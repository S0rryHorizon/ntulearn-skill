from __future__ import annotations

from pathlib import Path

import pytest
from test_reconciliation_adversarial import _announce, _harness

from ntulearn_skill.events import (
    CandidateFieldName,
    ClaimDecisionState,
    ConflictState,
    EventReconciler,
    EventSourceResolutionState,
    ManualFieldResolution,
)


def _due_claims(event):
    return tuple(claim for claim in event.claims if claim.field_name is CandidateFieldName.DUE_TIME)


def test_date_only_claim_does_not_bridge_incompatible_exact_times(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "same-source",
        "Quiz 1 due 2030-02-10.",
        ordinal=1,
        published_day=1,
    )
    _announce(
        harness,
        "same-source",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )
    _announce(
        harness,
        "same-source",
        "Quiz 1 due 2030-02-10 12:00 UTC.",
        ordinal=3,
        published_day=3,
    )

    event = harness.reconciler.reconcile_course(harness.first_course).events[0]

    assert event.field(CandidateFieldName.DUE_TIME) is None
    assert {claim.decision_state for claim in _due_claims(event)} == {
        ClaimDecisionState.CONFLICTING
    }
    assert any(
        conflict.field_name is CandidateFieldName.DUE_TIME and conflict.state is ConflictState.OPEN
        for conflict in event.conflicts
    )


def test_explicit_new_value_can_have_equivalent_corroborating_claims(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "base-source",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=1,
    )
    _announce(
        harness,
        "move-source",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )
    _announce(
        harness,
        "peer-source",
        "Quiz #1 due 2030-02-12 10:00 UTC.",
        ordinal=3,
        published_day=2,
    )

    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    projection = event.field(CandidateFieldName.DUE_TIME)
    due_claims = _due_claims(event)

    assert projection is not None
    assert projection.value["instant"].startswith("2030-02-12T10:00:00")
    assert sum(claim.decision_state is ClaimDecisionState.ACCEPTED for claim in due_claims) == 2
    assert sum(claim.decision_state is ClaimDecisionState.SUPERSEDED for claim in due_claims) == 1


def test_change_seen_first_binds_after_base_arrives_and_audit_is_idempotent(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "move-first",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=1,
        published_day=2,
    )
    first = harness.reconciler.reconcile_course(harness.first_course)
    assert not first.events
    assert first.unresolved_sources[0].resolution_state is EventSourceResolutionState.RELATED_CHANGE

    _announce(
        harness,
        "base-later",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=2,
        published_day=1,
    )
    second = harness.reconciler.reconcile_course(harness.first_course)
    repeated = harness.reconciler.reconcile_course(harness.first_course)

    assert len(second.events) == 1
    due = second.events[0].field(CandidateFieldName.DUE_TIME)
    assert due is not None and due.value["instant"].startswith("2030-02-12T10:00:00")
    assert not second.unresolved_sources
    assert len(second.decisions) == 2
    assert repeated.cache_hit and repeated.run_key == second.run_key
    assert len(repeated.decisions) == 2


def test_manual_a_b_a_selection_does_not_create_reverse_supersession_edge(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "manual-base",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=1,
    )
    _announce(
        harness,
        "manual-move",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    claims = sorted(_due_claims(event), key=lambda claim: claim.key)
    older, newer = claims

    for claim in (older, newer, older):
        event = harness.reconciler.resolve_field(
            ManualFieldResolution(
                event.key,
                CandidateFieldName.DUE_TIME,
                "Synthetic A-B-A manual selection.",
                selected_claim_key=claim.key,
            )
        )

    selected = event.field(CandidateFieldName.DUE_TIME)
    assert selected is not None and selected.selected_claim_key == older.key
    with harness.database.connect() as connection:
        edges = connection.execute(
            """SELECT successor_claim_key, predecessor_claim_key
            FROM claim_supersession ORDER BY 1, 2"""
        ).fetchall()
    assert [tuple(row) for row in edges] == [(newer.key, older.key)]


@pytest.mark.parametrize(
    ("original_day", "change_day", "has_winner"),
    (
        (1, 2, True),
        (1, None, False),
        (None, 2, False),
        (None, None, False),
    ),
    ids=(
        "known-later-change",
        "unknown-change-time",
        "unknown-original-time",
        "both-source-times-unknown",
    ),
)
def test_change_and_base_order_is_stable_without_inventing_source_chronology(
    tmp_path: Path,
    original_day: int | None,
    change_day: int | None,
    has_winner: bool,
) -> None:
    results: list[tuple[object | None, tuple[ClaimDecisionState, ...], int, bool]] = []
    for label, ordered in (
        ("change-first", ("change", "base")),
        ("base-first", ("base", "change")),
    ):
        harness = _harness(tmp_path / label)
        for ordinal, kind in enumerate(ordered, start=1):
            if kind == "change":
                _announce(
                    harness,
                    f"{label}-move",
                    "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
                    ordinal=ordinal,
                    published_day=change_day,
                )
            else:
                _announce(
                    harness,
                    f"{label}-base",
                    "Quiz 1 due 2030-02-10 10:00 UTC.",
                    ordinal=ordinal,
                    published_day=original_day,
                )
        reconciler = EventReconciler(harness.database, "order-v1")
        first = reconciler.reconcile_course(harness.first_course)
        repeated = reconciler.reconcile_course(harness.first_course)
        event = first.events[0]
        projection = event.field(CandidateFieldName.DUE_TIME)
        results.append(
            (
                None if projection is None else projection.value,
                tuple(
                    sorted(
                        (claim.decision_state for claim in _due_claims(event)),
                        key=lambda state: state.value,
                    )
                ),
                event.source_count,
                repeated.cache_hit and repeated.run_key == first.run_key,
            )
        )

    assert results[0] == results[1]
    value, states, source_count, cache_hit = results[0]
    assert source_count == 2
    assert cache_hit
    if has_winner:
        assert isinstance(value, dict)
        instant = value.get("instant")
        assert isinstance(instant, str) and instant.startswith("2030-02-12T10:00:00")
        assert set(states) == {
            ClaimDecisionState.ACCEPTED,
            ClaimDecisionState.SUPERSEDED,
        }
    else:
        assert value is None
        assert set(states) == {ClaimDecisionState.CONFLICTING}
