"""Phase 4 independent public journeys; invented data and injected transport only."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_m9_core_integration import NOW, WINDOW, _harness, _pdf, _policy

from ntulearn_skill.cli import run
from ntulearn_skill.client import (
    AuthorizedReadSession,
    NtulearnSourceAdapter,
    ReadOnlyTransport,
    ReadPurpose,
    ResolvedReadRequest,
    SessionStatus,
    WireResponse,
)
from ntulearn_skill.core import CourseId, Coverage
from ntulearn_skill.core.api import CoreService, CourseRef, FreshnessRequirement
from ntulearn_skill.integrations.codex.adapter import CodexToolDispatcher
from ntulearn_skill.search import SearchQuery
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync import SyncEngine
from ntulearn_skill.sync.event_sources import EventSourceScope


def _cli(service: CoreService, *args: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    code = run(["--json", *args], service=service, stdout=output)
    return code, json.loads(output.getvalue())


def test_restart_retains_old_and_new_exact_sources_across_public_interfaces(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.source.payload = _pdf("Invented old prismneedle explanation")
    assert h.service.sync_course(h.course, _policy()).ok
    first = h.service.search_course(h.course, SearchQuery("prismneedle", neighbor_count=0))
    assert first.items
    old = first.provenance[0]
    h.source.payload = _pdf("Invented new gratingneedle explanation")
    from ntulearn_skill.core.api import SyncPolicy

    assert h.service.sync_course(h.course, SyncPolicy(WINDOW, verify_resources=True)).ok
    assert len(ResourceRepository(h.database).list_versions(h.source.attachment)) == 2
    calls = tuple(h.source.calls)
    restarted = CoreService.from_runtime(h.store.paths.root)
    dispatcher = CodexToolDispatcher(restarted)
    for term in ("prismneedle", "gratingneedle"):
        code, result = _cli(restarted, "search", term)
        assert code in {0, 2} and result["items"]
        tool = dispatcher.call("search", {"query": term, "neighbor_count": 0})
        assert tool["items"]
        assert all(len(hit["neighbors"]) <= 1 for hit in tool["items"])
    resolved = dispatcher.call(
        "resolve_source", {"kind": old.source_kind, "key": old.source_key, "context_window": 0}
    )
    assert not resolved["errors"]
    assert "prismneedle" in json.dumps(resolved)
    assert "gratingneedle" not in json.dumps(resolved)
    assert str(h.store.paths.root) not in json.dumps(resolved)
    assert tuple(h.source.calls) == calls


def test_bounded_sync_restart_resume_then_equal_sync_is_idempotent(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.source.include_event_sources = False
    h.source.payload = _pdf("Assignment 7 is due 2034-02-10 10:00 UTC.")
    initial = h.service.sync_course(h.course, _policy(max_jobs=1))
    assert initial.completeness is not Coverage.COMPLETE
    # Leave a real claimed job unfinished, then expire its lease to model a stopped worker.
    claimed = h.engine.runner.queue._claim(now=datetime.now(UTC), lease=timedelta(minutes=5))
    assert claimed is not None
    interrupted_key = claimed[0].key
    with h.database.transaction() as connection:
        connection.execute(
            "UPDATE local_job SET lease_expires_at = ? WHERE job_key = ?",
            ("2000-01-01T00:00:00+00:00", interrupted_key),
        )
    paths = h.store.paths
    database = Database(paths.database)
    store = ResourceStore(paths, ResourceRepository(database))
    engine = SyncEngine(h.sessions, h.source, DomainRepository(database), store)
    restarted = CoreService(database, runtime_paths=paths, sync_engine=engine, now=lambda: NOW)
    assert restarted.sync_course(h.course, _policy()).ok
    events = restarted.get_upcoming_events(WINDOW, h.course)
    assert events.items
    before = json.dumps(
        CodexToolDispatcher(restarted).call("get_events", {"course_key": h.course.local_key})[
            "items"
        ],
        sort_keys=True,
    )
    assert restarted.sync_course(h.course, _policy()).ok
    after = json.dumps(
        CodexToolDispatcher(restarted).call("get_events", {"course_key": h.course.local_key})[
            "items"
        ],
        sort_keys=True,
    )
    assert before == after
    assert len(ResourceRepository(database).list_versions(h.source.attachment)) == 1
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM local_job WHERE status != 'SUCCEEDED'"
            ).fetchone()[0]
            == 0
        )


def test_real_adapter_to_sqlite_cli_and_auth_failure_recovery(tmp_path: Path) -> None:
    """No network: real adapter translates invented WireResponses through all layers."""
    calls: list[str] = []
    failed = False
    secret = "SYNTHETIC_PHASE4_SECRET_CANARY"
    course = CourseId("ntulearn", "invented-course")
    marker = object()

    class Sessions:
        def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
            return AuthorizedReadSession("ntulearn", frozenset({purpose}), marker)

        def status(self) -> SessionStatus:
            return SessionStatus.READY

        def invalidate(self, reason: str) -> None:
            assert secret not in reason

    def executor(
        request: ResolvedReadRequest, session: AuthorizedReadSession, forward_credentials: bool
    ) -> WireResponse:
        calls.append(request.target)
        if request.target.endswith("/memberships"):
            records = [
                {
                    "isAvailable": True,
                    "course": {
                        "id": course.value,
                        "courseId": "PH0000",
                        "displayName": "Invented course",
                    },
                }
            ]
        elif request.target.endswith("/announcements"):
            if failed:
                return WireResponse(401, json_body={"message": secret})
            records = [
                {
                    "id": "invented-notice",
                    "title": "Assignment 1",
                    "body": {"rawText": "Assignment 1 is due 2034-02-10 10:00 UTC."},
                    "createdDate": "2034-01-02T09:00:00+0000",
                    "visibility": "VISIBLE",
                }
            ]
        else:
            raise AssertionError(f"unexpected mock endpoint: {request.target}")
        return WireResponse(
            200,
            json_body={
                "paging": {"nextPage": "", "limit": 100, "count": len(records), "offset": 0},
                "results": records,
            },
        )

    paths = RuntimePaths(tmp_path / "invented-runtime")
    database = Database(paths.database)
    domain = DomainRepository(database)
    store = ResourceStore(paths, ResourceRepository(database))
    store.initialize()
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor), source_origin="https://learn.example.invalid"
    )
    engine = SyncEngine(Sessions(), adapter, domain, store)
    assert (
        engine.quick_sync(
            course,
            window=WINDOW,
            event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
            include_content=False,
        ).status.value
        == "SUCCEEDED"
    )
    record = domain.get_course(course)
    assert record is not None
    service = CoreService(
        database,
        runtime_paths=paths,
        sync_engine=engine,
        now=lambda: datetime.now(UTC) + timedelta(days=1),
    )
    reference = CourseRef(local_key=record.key)
    initial_calls = len(calls)
    code, cached = _cli(service, "announcements", str(record.key))
    assert code in {0, 2} and cached["items"]
    assert len(calls) == initial_calls
    failed = True
    expired = service.get_announcements(reference, freshness=FreshnessRequirement.require_current())
    assert expired.items and expired.errors and expired.refresh_attempted
    assert len(calls) == initial_calls + 1
    assert secret not in json.dumps(expired.to_dict())
    for persisted in paths.root.rglob("*"):
        if persisted.is_file():
            assert secret.encode() not in persisted.read_bytes()
    failed = False
    recovered = service.get_announcements(
        reference, freshness=FreshnessRequirement.require_current()
    )
    assert recovered.items
    assert not any(
        error.category.value in {"session_expired", "authentication_required"}
        for error in recovered.errors
    )
    offline = CoreService.from_runtime(paths.root)
    assert CodexToolDispatcher(offline).call("get_events", {"course_key": record.key})["items"]


def test_conflicting_source_fields_survive_restart_and_resolve_through_dispatcher(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from test_m9_core_integration import _time

    h = _harness(tmp_path)
    original = h.source.get_assessment
    h.source.get_assessment = lambda *args: replace(
        original(*args), grading_due_at=_time(NOW + timedelta(days=12))
    )
    assert h.service.sync_course(h.course, _policy()).ok
    service = CoreService.from_runtime(h.store.paths.root)
    dispatcher = CodexToolDispatcher(service)
    result = dispatcher.call("get_events", {"course_key": h.course.local_key})
    assert result["conflicts"]
    code, cli_events = _cli(service, "events", "--course", str(h.course.local_key))
    assert code == 2 and cli_events["conflicts"]
    assert result["provenance"]
    for provenance in result["provenance"]:
        resolved = dispatcher.call(
            "resolve_source", {"kind": provenance["source_kind"], "key": provenance["source_key"]}
        )
        assert resolved["items"] and not resolved["errors"]
    assert h.service.sync_course(h.course, _policy()).ok
    again = dispatcher.call("get_events", {"course_key": h.course.local_key})
    assert again["conflicts"] == result["conflicts"]


def test_multi_page_partial_retrieval_never_opens_original_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    from reportlab.pdfgen import canvas

    h = _harness(tmp_path)
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    for physical_page in range(1, 10):
        document.drawString(72, 720, f"Invented page {physical_page}")
        if physical_page == 5:
            document.drawString(72, 680, "uniquespectralneedle")
        document.showPage()
    document.save()
    h.source.payload = output.getvalue()
    assert h.service.sync_course(h.course, _policy()).ok
    service = CoreService.from_runtime(h.store.paths.root)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert not any(
            path.is_relative_to(h.store.paths.root / directory)
            for directory in ("objects", "courses")
        ), "retrieval attempted to reopen original bytes or a browse copy"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = CodexToolDispatcher(service).call(
        "search", {"query": "uniquespectralneedle", "neighbor_count": 1}
    )
    assert len(result["items"]) == 1
    hit = result["items"][0]
    assert len(hit["neighbors"]) == 3
    assert hit["locator"]["physical_page_index"] == 4
    resolved = CodexToolDispatcher(service).call(
        "resolve_source",
        {
            "kind": result["provenance"][0]["source_kind"],
            "key": result["provenance"][0]["source_key"],
            "context_window": 0,
        },
    )
    assert resolved["items"] and not resolved["errors"]
