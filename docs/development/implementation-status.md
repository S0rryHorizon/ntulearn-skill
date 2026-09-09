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
| M8 Incremental sync | PASS | `07a5074` | 362 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | Explicit scoped freshness; resource-only omission inference; queued contract changes fail safely and require current replanning; live transport remains unvalidated |
| M9 Core / CLI / integration | PASS | `da72f52` | 432 tests; lint/format; strict types; compile; wheel/sdist; independent + critical review PASS | Versioned Python/CLI/Codex envelopes; local keys in CLI; live sync requires an injected authorized engine; uncertain times and unlinked changes remain partial |

## Phase gates

- Phase 3: PASS (M1–M9 synthetic implementation acceptance).
- Phase 4: synthetic and runtime safety PASS (445 tests; independent safety review PASS). The historical direct API browser-navigation probe returned `BLOCKED_BY_CLIENT`; this was not a project transport response. The aggregate live-validation gate is not PASS; see the current follow-up below.
- Phase 5: independent offline open-source hardening PASS (463 tests on each of CPython 3.11–3.14; locked dependencies, packaging, documentation, CI configuration, and privacy review). The aggregate release gate remains blocked by private live validation; remote GitHub CI has not run.
- Public release readiness: NOT READY.

Acceptance requires executed tests, independent review, architecture regression
review, and a staged privacy check. Private live evidence stays outside tracked
files; only de-identified capability outcomes may be recorded here.

## Validation and hardening commits

- `25dba6f`: Phase 4 synthetic end-to-end validation and reviewed runtime-safety repairs.
- Phase 5: the separate `chore: harden packaging and public release checks` commit contains the final hardening changes; see [hardening acceptance](phase5-hardening.md) and [packaging validation](packaging-validation.md).

## Connection and extraction follow-up

M1–M9 are retained. The follow-up distinguishes missing executable connection and CLI
wiring from historical browser-tool errors; see [the diagnosis](connection-followup.md).

| Change | Status | Main commit | Evidence / remaining limits |
| --- | --- | --- | --- |
| Restarted raw API resource context | PASS | `c7104f1` | 467 implementation tests; supervisor regression 4 passed; independent critical review 116 focused tests passed; staged privacy audit 148 files passed. Bounded incomplete rediscovery fails before streaming and preserves cache. Raw API live protocol remains unvalidated. |
| Browser host provider and normal entry | IN PROGRESS | — | Fresh bounded CLI import and local provenance observed; independent review and final integrated acceptance pending. |
| Contextual native-text event extraction | IN PROGRESS | — | Frozen private baseline: 0/8 source-local event mentions found; repair and independent full-sample reevaluation pending. |

The currently blocked live operation is a second original-file download: the native
browser tool reports the Mac is locked. This is separate from the earlier API-navigation
`BLOCKED_BY_CLIENT` error. No alternate transport was attempted.
