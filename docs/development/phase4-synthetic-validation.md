# Phase 4 synthetic validation

Validation baseline: accepted M1–M9 commit `da72f52`, Python 3.12, 2026-09-10.
All added fixtures are invented in test code. No credentials, authenticated data,
network requests, browser access, or live reconnaissance were used.

`tests/test_phase4_e2e.py` adds five independent journeys across public interfaces:

| Journey | Observed evidence |
| --- | --- |
| Revision and restart | A changed PDF produces two immutable versions; restarted Core, CLI and Codex dispatcher find both texts. An old source reference resolves the old text without exposing runtime paths. |
| Bounded work and interrupted worker | A one-job sync reports incomplete coverage. A claimed job is left unfinished and its lease expired as fault injection. A new engine resumes persisted work; subsequent equal sync preserves event output and one resource version, with all local jobs succeeded. |
| Real adapter and authentication recovery | Injected, invented `WireResponse` objects pass through `NtulearnSourceAdapter`, read-only transport, SyncEngine and SQLite. Cache-only CLI reads make no additional transport calls. A 401 refresh retains cached announcements, reports errors and attempts one endpoint read; successful retry restores access. An invented error-body canary is absent from the result and persisted runtime files. |
| Conflicts and source provenance | Conflicting assessment due fields pass through sync and extraction, survive restart and repeated sync, remain visible in CLI/dispatcher output, and every returned provenance reference resolves locally. CLI returns partial-result exit code 2. |
| Bounded document retrieval | A nine-page PDF yields the matching physical page at zero-based index 4 and exactly three chunks for a one-neighbor search. Restarted search and source resolution succeed with original object and browse-copy file opens forbidden. |

Validation commands use `PYTHONPATH=src` and the existing development environment:

- `python -m pytest tests/test_phase4_e2e.py -q`: **5 passed**.
- `python -m pytest -q`: **437 passed** (432 existing plus 5 added).
- `ruff check tests/test_phase4_e2e.py`: **PASS**.
- `ruff format --check tests/test_phase4_e2e.py`: **PASS**.

Result: **PASS for this bounded synthetic validation**. No production defect was
confirmed and no production code was changed. The new tests reuse the invented
M9 source helper for resource journeys, while the announcement/authentication
journey exercises the actual NTULearn adapter over an injected transport.

This does not establish live NTULearn compatibility, real binary-download route
support, authentication renewal, real revision behavior, or private-live gate
completion. The interrupted-worker case is deterministic lease fault injection,
not an operating-system process kill. Packaging/history/release audits remain
Phase 5 work. Private live evidence must remain separate.

## Runtime safety repair and combined acceptance

The separate independent security review found and reproduced three defects:
initial discovery authentication failures could leave a run unfinished; descriptive
metadata could retain signed URL query data; and untrusted exception categories
could enter durable diagnostics and public errors. The repairs finish failed runs,
normalize categories to a fixed vocabulary, and sanitize descriptive metadata at
persistence boundaries. Binary originals remain unchanged.

A repair regression in resource fingerprints was also fixed. An internal immutable
canonical metadata representation now carries the same once-normalized values and
fingerprint through comparison and persistence, including local-ID fallback. Repeated
signed-URL, whitespace and encoded-title observations respect the verification interval.

Combined supervisor validation: **445 tests passed**, Ruff lint/format, strict mypy
for 58 source files, compile and wheel/sdist builds passed. Independent final safety
review: **39 external synthetic probes passed**, plus **60 focused tests passed**.
The external probes cover the original defects, read-only policy, redirects, expiry,
paths, partial downloads, permissions and canonical fingerprint regressions.

Result: **PASS for synthetic and runtime-safety validation**. This is not an aggregate
Phase 4 live PASS. A new bounded private check reached the authenticated course UI,
but the known read-only course API was again blocked by the browser client. No
credentials were extracted, no bypass was attempted, and the temporary tab was closed.
End-to-end course/content/announcement/assessment/due/resource live checks remain
BLOCKED pending a usable authorized read-only session/transport environment. Private
evidence is ignored and outside Git. Independent hardening may proceed while this
external gate remains open; public release readiness remains NOT READY.
