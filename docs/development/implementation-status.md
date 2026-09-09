# Implementation status

Phase 2 architecture is the accepted implementation baseline. Public release is a
human gate: no remote creation, push, or package publication is authorized.

| Milestone | Status | Commit | Acceptance / tests | Validated limitations |
| --- | --- | --- | --- | --- |
| M1 Domain and SQLite | PASS | `d9f2997` | 37 tests; lint/format; strict types; compile; wheel/sdist; independent + security review PASS | No live source functionality in M1 |
| M2 Immutable resources | PASS | `7b96d78` | 63 tests; lint/format; strict types; compile; independent + critical review PASS | Complete orphan blobs retained; browse failures explicitly repairable |
| M3 Read-only source | PASS | `8f8d79b` | 123 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | Injected session/transport and fresh resource route; end-to-end live adapter NOT VALIDATED |
| M4 Parsing | PASS | `4f17594` | 82 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | PDF/DOCX only; visual fallback requires an optional provider; no decoder process isolation |
| M5 FTS retrieval | PASS | `562face` | 144 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | Lexical FTS; durable source locators survive rebuild; full freshness policy follows in M8 |
| M6 Event candidates | PASS | `c2fa638` | 180 tests; lint/format; strict types; compile; wheel/sdist; independent + source critical review PASS | Bounded English extraction; schedule adapter UNKNOWN; due-calendar source times not assumed to be due times |
| M7 Reconciliation | PASS | `6d643e5` | 277 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | Bounded affirmative English changes; manual reassignment of an already-bound source is rejected; local decisions are not indexed as source evidence |
| M8 Incremental sync | PASS | this commit | 362 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | Explicit scoped freshness; resource-only omission inference; queued contract changes fail safely and require current replanning; live transport remains unvalidated |
| M9 Core / CLI / integration | PENDING | — | Depends on M1–M8 | — |

## Phase gates

- Phase 3: IN PROGRESS.
- Phase 4 validation: PENDING.
- Phase 5 open-source hardening: PENDING.
- Public release readiness: NOT READY.

Acceptance requires executed tests, independent review, architecture regression
review, and a staged privacy check. Private live evidence stays outside tracked
files; only de-identified capability outcomes may be recorded here.
