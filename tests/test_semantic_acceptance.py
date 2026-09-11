"""Synthetic acceptance regressions for freshness and current event semantics."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import cast

from test_m8_resources_jobs import _harness as _job_harness
from test_m8_resources_jobs import _run as _job_sync_run
from test_m9_core import _database, _record_complete
from test_reconciliation_adversarial import _announce
from test_reconciliation_adversarial import _harness as _reconciliation_harness

from ntulearn_skill.cli._presentation import json_envelope, render_human
from ntulearn_skill.client.contracts import AnnouncementSourceRecord
from ntulearn_skill.core import AnnouncementId, Availability, CourseId, Coverage
from ntulearn_skill.core.api import CoreService, CourseFilter, CourseRef, EventFilter
from ntulearn_skill.core.results import FreshnessView, ResultEnvelope
from ntulearn_skill.events import (
    CandidateFieldName,
    EventConfirmationBasisKind,
    EventReconciler,
    EventRepository,
    ManualFieldResolution,
)
from ntulearn_skill.events import extraction as extraction_module
from ntulearn_skill.integrations.codex import CodexToolDispatcher
from ntulearn_skill.sync.freshness import (
    FreshnessRequirement,
    FreshnessStatus,
    FreshnessWarning,
)
from ntulearn_skill.sync.jobs import LocalJobStatus
from ntulearn_skill.sync.state import ScopeKey


def test_cache_only_satisfaction_is_independent_from_staleness(tmp_path) -> None:
    database, paths, domain = _database(tmp_path)
    domain.put_course(
        CourseId("synthetic", "semantic-course"),
        code="PH0000",
        title="Synthetic semantic course",
    )
    scope = ScopeKey("synthetic", None, "courses")
    observed_at = datetime(2031, 1, 1, tzinfo=UTC)
    _record_complete(database, scope, at=observed_at, max_age=timedelta(hours=1))
    result = CoreService(
        database,
        runtime_paths=paths,
        now=lambda: observed_at + timedelta(hours=2),
    ).list_courses(CourseFilter(), FreshnessRequirement.cache_only())

    assert result.source_completeness is Coverage.COMPLETE
    assert result.freshness_satisfied is True
    assert result.freshness[0].status == "STALE"
    assert result.freshness[0].ttl_configured


def test_cache_only_unknown_scope_has_no_unstated_age_guarantee(tmp_path) -> None:
    database, paths, domain = _database(tmp_path)
    domain.put_course(
        CourseId("synthetic", "unknown-freshness-course"),
        code="PH0000",
        title="Synthetic unknown freshness course",
    )

    result = CoreService(database, runtime_paths=paths).list_courses(
        CourseFilter(), FreshnessRequirement.cache_only()
    )

    assert result.freshness_satisfied is True
    assert result.freshness[0].status == FreshnessStatus.UNKNOWN.value
    assert not result.freshness[0].ttl_configured
    assert FreshnessWarning.NO_MAX_AGE_GUARANTEE.value in result.freshness[0].warning_codes


def test_truncated_local_page_does_not_fabricate_a_freshness_error(tmp_path) -> None:
    database, paths, domain = _database(tmp_path)
    for ordinal in (1, 2):
        domain.put_course(
            CourseId("synthetic", f"semantic-course-{ordinal}"),
            code=f"PH000{ordinal}",
            title=f"Synthetic semantic course {ordinal}",
        )
    scope = ScopeKey("synthetic", None, "courses")
    observed_at = datetime(2031, 1, 1, tzinfo=UTC)
    _record_complete(database, scope, at=observed_at)
    result = CoreService(
        database,
        runtime_paths=paths,
        now=lambda: observed_at,
    ).list_courses(
        CourseFilter(limit=1),
        FreshnessRequirement.with_max_age(timedelta(minutes=1)),
    )

    assert result.truncated
    assert result.completeness is Coverage.PARTIAL
    assert result.source_completeness is Coverage.COMPLETE
    assert result.freshness_satisfied is True
    assert result.errors == ()


def test_human_output_separates_policy_coverage_and_pagination_dimensions() -> None:
    result = ResultEnvelope[object](
        "synthetic.query",
        items=({"title": "Synthetic item"},),
        freshness=(
            FreshnessView(
                "synthetic/global/courses/all",
                "STALE",
                datetime(2031, 1, 1, tzinfo=UTC),
                7_200,
                True,
                ("no_max_age_guarantee",),
                ttl_configured=False,
            ),
        ),
        completeness=Coverage.PARTIAL,
        source_completeness=Coverage.COMPLETE,
        freshness_satisfied=True,
        truncated=True,
        next_cursor="synthetic-cursor",
    )
    payload = json_envelope(result, command="courses")

    assert payload["source_completeness"] == "COMPLETE"
    assert payload["freshness_satisfied"] is True
    assert payload["truncated"] is True
    assert payload["next_cursor"] == "synthetic-cursor"
    rendered = render_human(payload)
    assert "Source completeness: COMPLETE" in rendered
    assert "Freshness policy satisfied: yes" in rendered
    assert "Truncated: yes" in rendered
    assert "Next cursor: synthetic-cursor" in rendered
    assert "ttl_configured=false" in rendered
    assert 'status="STALE"' in rendered


def test_codex_adapter_preserves_freshness_and_pagination_dimensions() -> None:
    result = ResultEnvelope[object](
        "list_courses",
        items=({"title": "Synthetic item"},),
        freshness=(FreshnessView("synthetic/global/courses/all", "STALE", None, None, True),),
        completeness=Coverage.PARTIAL,
        source_completeness=Coverage.COMPLETE,
        freshness_satisfied=True,
        truncated=True,
        next_cursor="synthetic-cursor",
    )

    class _Core:
        def list_courses(self, *args, **kwargs):
            del args, kwargs
            return result

    payload = CodexToolDispatcher(cast(CoreService, _Core())).call("list_courses", {})

    assert payload["completeness"] == "PARTIAL"
    assert payload["source_completeness"] == "COMPLETE"
    assert payload["freshness_satisfied"] is True
    assert payload["truncated"] is True
    assert payload["next_cursor"] == "synthetic-cursor"


def test_new_incompatible_source_reopens_old_manual_field_decision(tmp_path) -> None:
    harness = _reconciliation_harness(tmp_path)
    _announce(
        harness,
        "manual-then-source-change",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=1,
    )
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    old_due = event.field(CandidateFieldName.DUE_TIME)
    assert old_due is not None
    harness.reconciler.resolve_field(
        ManualFieldResolution(
            event.key,
            CandidateFieldName.DUE_TIME,
            "Confirm the synthetic initial date.",
            selected_claim_key=old_due.selected_claim_key,
        )
    )

    _announce(
        harness,
        "manual-then-source-change",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )
    updated = EventReconciler(harness.database).reconcile_course(harness.first_course).events[0]
    due = updated.field(CandidateFieldName.DUE_TIME)
    assert due is not None
    assert str(due.value["instant"]).startswith("2030-02-12T10:00:00")  # type: ignore[index]

    preview = (
        CoreService(harness.database)
        .get_events(EventFilter(course=CourseRef(remote_id=harness.first_course)))
        .items[0]
    )
    assert preview.review is not None
    basis = next(
        item
        for item in preview.review.confirmation_basis
        if item.field_name is CandidateFieldName.DUE_TIME
    )
    assert basis.kind is EventConfirmationBasisKind.DETERMINISTIC_RULE
    assert not basis.manually_confirmed
    with harness.database.connect() as connection:
        decisions = connection.execute(
            """SELECT active FROM manual_field_resolution
            WHERE event_key = ? AND field_name = 'due_time'
            ORDER BY manual_resolution_key""",
            (event.key,),
        ).fetchall()
    assert [int(row["active"]) for row in decisions] == [0]


def test_new_agreeing_source_keeps_a_still_valid_manual_confirmation(tmp_path) -> None:
    harness = _reconciliation_harness(tmp_path)
    _announce(
        harness,
        "manual-then-corroboration",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=1,
    )
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    due = event.field(CandidateFieldName.DUE_TIME)
    assert due is not None
    harness.reconciler.resolve_field(
        ManualFieldResolution(
            event.key,
            CandidateFieldName.DUE_TIME,
            "Confirm the synthetic date.",
            selected_claim_key=due.selected_claim_key,
        )
    )
    _announce(
        harness,
        "manual-then-corroboration",
        "Quiz 1 remains due 2030-02-10 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )

    EventReconciler(harness.database).reconcile_course(harness.first_course)
    preview = (
        CoreService(harness.database)
        .get_events(EventFilter(course=CourseRef(remote_id=harness.first_course)))
        .items[0]
    )
    assert preview.review is not None
    basis = next(
        item
        for item in preview.review.confirmation_basis
        if item.field_name is CandidateFieldName.DUE_TIME
    )
    assert basis.kind is EventConfirmationBasisKind.MANUAL_RESOLUTION
    assert basis.manually_confirmed
    with harness.database.connect() as connection:
        active = connection.execute(
            """SELECT active FROM manual_field_resolution
            WHERE event_key = ? ORDER BY manual_resolution_key""",
            (event.key,),
        ).fetchall()
    assert [int(row["active"]) for row in active] == [1]


def test_same_source_due_change_keeps_unchanged_title_confirmation(tmp_path) -> None:
    harness = _reconciliation_harness(tmp_path)
    _announce(
        harness,
        "same-source-field-change",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=1,
    )
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    title = event.field(CandidateFieldName.TITLE)
    assert title is not None
    harness.reconciler.resolve_field(
        ManualFieldResolution(
            event.key,
            CandidateFieldName.TITLE,
            "Confirm the unchanged synthetic title.",
            selected_claim_key=title.selected_claim_key,
        )
    )
    _announce(
        harness,
        "same-source-field-change",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )

    EventReconciler(harness.database).reconcile_course(harness.first_course)
    preview = (
        CoreService(harness.database)
        .get_events(EventFilter(course=CourseRef(remote_id=harness.first_course)))
        .items[0]
    )
    assert preview.review is not None
    title_basis = next(
        item
        for item in preview.review.confirmation_basis
        if item.field_name is CandidateFieldName.TITLE
    )
    assert title_basis.kind is EventConfirmationBasisKind.MANUAL_RESOLUTION
    assert title_basis.manually_confirmed
    due = preview.field(CandidateFieldName.DUE_TIME)
    assert due is not None
    assert str(due.value["instant"]).startswith("2030-02-12T10:00:00")  # type: ignore[index]


def test_legacy_extraction_jobs_remain_readable_and_pending_work_fails_closed(
    tmp_path,
) -> None:
    harness = _job_harness(tmp_path, [])
    timestamp = datetime(2030, 1, 1, tzinfo=UTC).isoformat(timespec="microseconds")
    legacy_payload = json.dumps(
        {
            "observation_key": 1,
            "extractor_name": harness.runner.extractor.name,
            "extractor_version": harness.runner.extractor.version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with harness.database.transaction() as connection:
        connection.execute(
            """INSERT INTO local_job(
                job_kind, input_identity, payload_json, result_json, status,
                max_attempts, available_at, created_at, updated_at, completed_at
            ) VALUES ('extract', ?, ?, '{"extraction_record_key":1}', 'SUCCEEDED',
                      3, ?, ?, ?, ?)""",
            ("a" * 64, legacy_payload, timestamp, timestamp, timestamp, timestamp),
        )
        pending = connection.execute(
            """INSERT INTO local_job(
                job_kind, input_identity, payload_json, status, max_attempts,
                available_at, created_at, updated_at
            ) VALUES ('extract', ?, ?, 'PENDING', 3, ?, ?, ?)
            RETURNING job_key""",
            ("b" * 64, legacy_payload, timestamp, timestamp, timestamp),
        ).fetchone()
    assert pending is not None
    assert len(harness.queue.list()) == 2

    outcome = harness.runner.run(
        max_jobs=1,
        job_keys=(int(pending["job_key"]),),
        now=datetime(2030, 1, 2, tzinfo=UTC),
    )
    assert outcome.failed == 1
    failed = harness.queue.get(int(pending["job_key"]))
    assert failed is not None
    assert failed.status is LocalJobStatus.FAILED
    assert failed.last_error_category == "local_job_contract_mismatch"


def test_rule_six_to_seven_reextracts_and_retires_manually_selected_false_positive(
    tmp_path, monkeypatch
) -> None:
    harness = _job_harness(tmp_path, [])
    course_id = CourseId("synthetic", "course-one")
    observed = EventRepository(harness.database).observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "rule-upgrade-scientific-test"),
            course_id,
            "Synthetic experiment note",
            "A scientific test examines the sample on Monday, 6th May 2030.",
            Availability.ACTIVE,
        ),
        sync_run_key=_job_sync_run(harness.database, datetime(2030, 1, 1, tzinfo=UTC)),
    )
    revision_six = dict(extraction_module._SETTINGS)
    revision_six["rule_revision"] = 6
    revision_six_hash = hashlib.sha256(
        json.dumps(revision_six, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    never_matches = re.compile(r"(?!x)x")
    with monkeypatch.context() as old_rules:
        old_rules.setattr(extraction_module, "_SETTINGS_HASH", revision_six_hash)
        old_rules.setattr(extraction_module, "_NON_ASSESSMENT_TEST_PREFIX", never_matches)
        old_rules.setattr(extraction_module, "_NON_ASSESSMENT_TEST_SUFFIX", never_matches)
        old_plan = harness.planner.plan_observation(observed.observation.key)
        old_run = harness.runner.run(
            max_jobs=2,
            job_keys=old_plan.keys,
            now=datetime(2030, 1, 2, tzinfo=UTC),
        )
        assert old_run.succeeded == 2

    reconciler = EventReconciler(harness.database)
    event = reconciler.list_events(course_id)[0]
    event_type = event.field(CandidateFieldName.EVENT_TYPE)
    assert event_type is not None
    reconciler.resolve_field(
        ManualFieldResolution(
            event.key,
            CandidateFieldName.EVENT_TYPE,
            "Synthetic review under the old rule.",
            selected_claim_key=event_type.selected_claim_key,
        )
    )

    current_plan = harness.planner.plan_observation(observed.observation.key)
    assert current_plan.keys != old_plan.keys
    current_run = harness.runner.run(
        max_jobs=2,
        job_keys=current_plan.keys,
        now=datetime(2030, 1, 3, tzinfo=UTC),
    )
    assert current_run.succeeded == 2
    current = harness.runner.extractor.extract_observation(observed.observation.key)
    assert current.cache_hit
    assert current.candidates == ()
    reconciled = EventReconciler(harness.database).reconcile_course(course_id)

    assert reconciled.events == ()
    assert EventReconciler(harness.database).get_event(event.key) is None
    with harness.database.connect() as connection:
        decisions = connection.execute(
            """SELECT active FROM manual_field_resolution
            WHERE event_key = ? ORDER BY manual_resolution_key""",
            (event.key,),
        ).fetchall()
    assert [int(row["active"]) for row in decisions] == [0]


def test_numbered_student_unit_test_is_not_removed_by_scientific_wording_filter(
    tmp_path,
) -> None:
    harness = _job_harness(tmp_path, [])
    course_id = CourseId("synthetic", "course-one")
    observed = EventRepository(harness.database).observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "numbered-unit-test"),
            course_id,
            "Synthetic assessment notice",
            "Unit Test 1 is on Monday, 6th May 2030.",
            Availability.ACTIVE,
        ),
        sync_run_key=_job_sync_run(harness.database, datetime(2030, 1, 1, tzinfo=UTC)),
    )

    result = harness.runner.extractor.extract_observation(observed.observation.key)

    assert len(result.candidates) == 1
    event_type = result.candidates[0].field(CandidateFieldName.EVENT_TYPE)
    assert event_type is not None
    assert event_type.value == "test"
