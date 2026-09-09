"""Focused synthetic regressions for M7 reconciliation review repairs."""

from __future__ import annotations

from itertools import permutations

import pytest
from test_reconciliation_adversarial import (
    _announce,
    _assessment_with_due_alternatives,
    _due_claims,
    _harness,
)

from ntulearn_skill.events import (
    CandidateFieldName,
    ClaimDecisionState,
    ConflictState,
    EventReconciler,
    EventResolutionState,
    EventSourceResolutionState,
    ManualFieldResolution,
)


def _source_payload_snapshot(harness) -> tuple[tuple[str, tuple[object, ...]], ...]:
    tables = (
        "source_observation",
        "extraction_record",
        "event_candidate",
        "event_candidate_field",
    )
    with harness.database.connect() as connection:
        return tuple(
            (table, tuple(row))
            for table in tables
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )


def _supersession_edges(harness) -> set[tuple[int, int]]:
    with harness.database.connect() as connection:
        return {
            (int(row["successor_claim_key"]), int(row["predecessor_claim_key"]))
            for row in connection.execute(
                """SELECT successor_claim_key, predecessor_claim_key
                FROM claim_supersession"""
            )
        }


def _assert_supersession_dag(harness) -> None:
    edges = _supersession_edges(harness)
    graph: dict[int, set[int]] = {}
    for successor, predecessor in edges:
        graph.setdefault(successor, set()).add(predecessor)

    def visit(node: int, path: set[int], complete: set[int]) -> None:
        if node in path:
            pytest.fail(f"claim supersession cycle contains synthetic claim {node}")
        if node in complete:
            return
        path.add(node)
        for predecessor in graph.get(node, set()):
            visit(predecessor, path, complete)
        path.remove(node)
        complete.add(node)

    complete: set[int] = set()
    for node in graph:
        visit(node, set(), complete)


@pytest.mark.parametrize(
    "observation_order",
    tuple(permutations(("date", "morning", "afternoon"))),
)
def test_date_only_with_two_incompatible_exact_times_never_hides_disagreement(
    tmp_path, observation_order: tuple[str, str, str]
) -> None:
    harness = _harness(tmp_path)
    wording = {
        "date": "Quiz 1 due 2030-02-10.",
        "morning": "Quiz 1 due 2030-02-10 10:00 UTC.",
        "afternoon": "Quiz 1 due 2030-02-10 14:00 UTC.",
    }
    for ordinal, item in enumerate(observation_order, start=1):
        _announce(
            harness,
            "three-way-temporal-conflict",
            wording[item],
            ordinal=ordinal,
        )

    result = harness.reconciler.reconcile_course(harness.first_course)

    with harness.database.connect() as connection:
        due_rows = connection.execute(
            """SELECT claim.decision_state, source.resolution_state
            FROM claim JOIN event_source source
              ON source.event_source_key = claim.event_source_key
            WHERE claim.field_name = 'due_time'
            ORDER BY claim.claim_key"""
        ).fetchall()
    assert len(due_rows) == 3
    assert not {
        ClaimDecisionState.REJECTED.value,
        ClaimDecisionState.SUPERSEDED.value,
    } & {str(row["decision_state"]) for row in due_rows}

    visible_conflict = any(
        conflict.field_name is CandidateFieldName.DUE_TIME and conflict.state is ConflictState.OPEN
        for event in result.events
        for conflict in event.conflicts
    )
    separate_unresolved = any(
        str(row["resolution_state"]) != EventSourceResolutionState.MATCHED.value for row in due_rows
    )
    assert visible_conflict or separate_unresolved
    assert not any(
        event.resolution_state is EventResolutionState.RESOLVED
        and len(_due_claims(harness, event.key)) > 1
        for event in result.events
    )


@pytest.mark.parametrize(
    "observation_order",
    (
        ("old", "move", "corroboration"),
        ("corroboration", "old", "move"),
    ),
)
def test_equal_timestamp_move_corroboration_keeps_new_due_date(
    tmp_path, observation_order: tuple[str, str, str]
) -> None:
    harness = _harness(tmp_path)
    evidence = {
        "old": ("Quiz 1 due 2030-02-10 10:00 UTC.", 1),
        "move": ("Quiz 1 deadline moved to 2030-02-12 10:00 UTC.", 2),
        "corroboration": ("Quiz 1 due 2030-02-12 10:00 UTC.", 2),
    }
    for ordinal, item in enumerate(observation_order, start=1):
        wording, published_day = evidence[item]
        _announce(
            harness,
            "corroborated-move",
            wording,
            ordinal=ordinal,
            published_day=published_day,
        )

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    event = result.events[0]
    selected = event.field(CandidateFieldName.DUE_TIME)
    assert selected is not None and not selected.uncertain
    assert selected.value["instant"].startswith("2030-02-12T10:00:00")
    due_claims = _due_claims(harness, event.key)
    assert len(due_claims) == 3
    assert sum(claim.decision_state is ClaimDecisionState.ACCEPTED for claim in due_claims) == 2
    assert sum(claim.decision_state is ClaimDecisionState.SUPERSEDED for claim in due_claims) == 1
    assert event.resolution_state is EventResolutionState.RESOLVED


def test_source_publication_chronology_beats_observation_order_and_cache(tmp_path) -> None:
    outcomes = []
    for label, observation_order in (
        ("change-first", ("change", "original")),
        ("original-first", ("original", "change")),
    ):
        harness = _harness(tmp_path / label)
        evidence = {
            "original": ("Quiz 1 due 2030-02-10 10:00 UTC.", 1),
            "change": ("Quiz 1 deadline moved to 2030-02-12 10:00 UTC.", 2),
        }
        for ordinal, item in enumerate(observation_order, start=1):
            wording, published_day = evidence[item]
            _announce(
                harness,
                "publication-order",
                wording,
                ordinal=ordinal,
                published_day=published_day,
            )

        first = harness.reconciler.reconcile_course(harness.first_course)
        cached = harness.reconciler.reconcile_course(harness.first_course)

        assert not first.cache_hit
        assert cached.cache_hit and cached.run_key == first.run_key
        assert len(cached.events) == 1
        event = cached.events[0]
        selected = event.field(CandidateFieldName.DUE_TIME)
        assert selected is not None and not selected.uncertain
        assert all(source.source_timestamp_semantics == "published_at" for source in event.sources)
        if label == "change-first":
            change_source = next(
                source for source in event.sources if source.change_kind.value == "MOVE"
            )
            original_source = next(
                source for source in event.sources if source.change_kind.value == "NONE"
            )
            assert change_source.observed_at < original_source.observed_at
            assert change_source.source_timestamp > original_source.source_timestamp
        outcomes.append(selected.value)

    assert outcomes[0] == outcomes[1]
    assert outcomes[0]["instant"].startswith("2030-02-12T10:00:00")


def test_manual_a_b_a_history_is_append_only_and_keeps_supersession_acyclic(tmp_path) -> None:
    harness = _harness(tmp_path)
    _assessment_with_due_alternatives(harness)
    before = _source_payload_snapshot(harness)
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    claims = sorted(_due_claims(harness, event.key), key=lambda claim: claim.key)
    assert len(claims) == 2
    first_claim, second_claim = claims

    for claim, reason in (
        (first_claim, "Select synthetic due alternative A."),
        (second_claim, "Revise to synthetic due alternative B."),
        (first_claim, "Return to synthetic due alternative A."),
    ):
        resolved = harness.reconciler.resolve_field(
            ManualFieldResolution(
                event.key,
                CandidateFieldName.DUE_TIME,
                reason,
                selected_claim_key=claim.key,
            )
        )
        projection = resolved.field(CandidateFieldName.DUE_TIME)
        assert projection is not None and projection.selected_claim_key == claim.key
        _assert_supersession_dag(harness)

    with harness.database.connect() as connection:
        history = connection.execute(
            """SELECT selected_claim_key, active FROM manual_field_resolution
            WHERE event_key = ? AND field_name = 'due_time'
            ORDER BY manual_resolution_key""",
            (event.key,),
        ).fetchall()
    assert [(int(row["selected_claim_key"]), int(row["active"])) for row in history] == [
        (first_claim.key, 0),
        (second_claim.key, 0),
        (first_claim.key, 1),
    ]
    assert _source_payload_snapshot(harness) == before

    rerun = EventReconciler(harness.database, resolver_version="review-v2").reconcile_course(
        harness.first_course
    )
    persisted = rerun.events[0].field(CandidateFieldName.DUE_TIME)
    assert persisted is not None and persisted.selected_claim_key == first_claim.key
    assert _source_payload_snapshot(harness) == before
    _assert_supersession_dag(harness)


def test_manual_predecessor_can_override_automatic_winner_without_cycle(tmp_path) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "automatic-then-manual",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=1,
    )
    _announce(
        harness,
        "automatic-then-manual",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )
    before = _source_payload_snapshot(harness)
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    due_claims = _due_claims(harness, event.key)
    automatic_winner = next(
        claim for claim in due_claims if claim.decision_state is ClaimDecisionState.ACCEPTED
    )
    predecessor = next(
        claim for claim in due_claims if claim.decision_state is ClaimDecisionState.SUPERSEDED
    )
    assert (automatic_winner.key, predecessor.key) in _supersession_edges(harness)

    resolved = harness.reconciler.resolve_field(
        ManualFieldResolution(
            event.key,
            CandidateFieldName.DUE_TIME,
            "Restore the synthetic predecessor after review.",
            selected_claim_key=predecessor.key,
        )
    )
    projection = resolved.field(CandidateFieldName.DUE_TIME)
    assert projection is not None and projection.selected_claim_key == predecessor.key
    assert (automatic_winner.key, predecessor.key) in _supersession_edges(harness)
    assert (predecessor.key, automatic_winner.key) not in _supersession_edges(harness)
    _assert_supersession_dag(harness)
    assert _source_payload_snapshot(harness) == before

    rerun = EventReconciler(harness.database, resolver_version="review-v2").reconcile_course(
        harness.first_course
    )
    persisted = rerun.events[0].field(CandidateFieldName.DUE_TIME)
    assert persisted is not None and persisted.selected_claim_key == predecessor.key
    assert _source_payload_snapshot(harness) == before
    _assert_supersession_dag(harness)
