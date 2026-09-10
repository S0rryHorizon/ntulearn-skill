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
- Phase 5: independent offline open-source hardening PASS (463 tests on each of CPython 3.11–3.14; locked dependencies, packaging, documentation, CI configuration, and privacy review). This is historical offline acceptance; the current browser-assisted lane is assessed below. Remote GitHub CI has not run.
- Current scoped assessment: local daily retrieval trial and limited experimental retrieval candidate; event extraction is a preview. Publication remains a human gate.

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
| Browser host provider and normal entry | PASS (implementation) | `e3ffd60` | 482 implementation tests; independent provider review 37 focused tests and integrated review 33 tests passed. Installed-wheel CLI imported the bounded real capture; second fresh download returned `REUSED_VERIFIED` with unchanged immutable version and derived counts. |
| Contextual native-text event extraction | PASS (implementation and bounded evaluation) | `f67db02` | 474 implementation tests; final independent critical review 39 focused tests passed. Frozen real evaluation: 5/8 mentions, 0 false positives, 17/18 matched fields correct, 18/18 complete provenance paths; three image-only dates and generic-title limitation remain. |
| Resource-backed event/Claim search preservation | PASS | `c6a15c0` | Independent critical review and two focused regressions passed. Installed CLI repaired the existing private index; repeat/replay/simulated-expiry checks preserved four logical search hits and all canonical counts. |

The first repeatable real connection chain is **COMPLETE**. The locked-screen blocker is resolved. A second normal browser download and installed-CLI
targeted verification passed: `REUSED_VERIFIED`, equal SHA-256, unchanged immutable version,
parse and event/Claim counts, stable physical-page provenance, and zero observed socket connection calls in
a subsequent cache-only check. Only the selected resource was refreshed; course-wide
coverage, real SSO expiry, changed remote bytes and raw API transport remain unvalidated.

Integrated follow-up verification: **498 tests passed** on Python 3.12; Ruff check and
format, strict mypy (59 source files), compile, wheel/sdist build and public artifact
audits passed. The installed local CLI and browser Skill are available; host-assisted
fresh collection requires the documented connected Chrome host. No remote or publication
was performed. Bounded second-download acceptance passed; this does not promote aggregate live coverage
or public release readiness to PASS.

## Daily-library follow-up

M1–M9 and the accepted repeatable connection chain remain complete. Current work
uses the documented browser-host-assisted lane; raw API execution is a separate
unvalidated capability, not an automatic blocker for a limited local trial.

| Change | Status | Main commit | Acceptance / limits |
| --- | --- | --- | --- |
| Local daily-question Skill and library/change queries | PASS | `a2b85f1` | Independent implementation review; normal installed Skill used for bounded Chinese questions; lexical and source-backed, not semantic completeness |
| Private runtime-root discovery | PASS | `f4346ec` | Normal CLI discovers private configuration; bounded path/permission checks; no user-written provider needed |
| Selective visual evidence | PASS | `5c9f15d` | Independent review; immutable native/original evidence, mixed/vector-page diagnostics and normal CLI import; reviewed pages stay PARTIAL |
| Current extraction reconciliation | PASS | `ec32016` | Independent persistent-DB cache/retirement review; obsolete Claims remain auditable; weak identities stay unresolved |
| Uncertain material observations | PASS | `c149b28` | Seven focused tests and independent review; UNKNOWN is not a material update; history retained |
| Visual source resolution and active parse index | PASS | `1eb2bd0` | Independent 549-test branch review; normal source provenance, historical locators, failed-parse fallback, index contract replan |
| Bounded extraction context and field repairs | PASS (implementation) | `aaea8c4` | Independent 552-test branch review; integrated 556 tests; original 8/8 mentions, supplemental 13/16; incomplete fields and event types remain explicit |

Collection and evaluation details are recorded in [daily-library validation](daily-library-validation.md).
Private per-course inventories, source identities, timestamps and human labels remain
outside Git. Final integrated follow-up: **556 tests passed on Python 3.12**, lint/format,
strict types (61 source files), compile, wheel/sdist and independent reviews passed.
The five-question installed Skill session passed with explicit coverage and source-conflict
limitations. This supports a limited experimental retrieval candidate, not authoritative
event automation or an unrestricted public-release declaration. Earlier phase gates
above describe their historical acceptance scope.

## Bounded daily-answer maintenance

The local question guide now distinguishes scientific uses of `test` from student
assessments while retaining source-backed lab arrangements. Upcoming answers check
both window bounds, overlapping sessions, date-only uncertainty and conflicting dates.
This is a Skill instruction change; it does not repair stored event classifications
or increase content coverage. The installed Skill links to the checkout, so the guide
is available without reinstalling the unchanged Python package.

The takeover check at `f268e8e` verified installed/source Python file equality, CLI
help and cache-only library status (no errors, no refresh; incomplete coverage remains
explicit). No full suite, live acceptance or remote download was repeated. Skill
entrypoint/reference checks and diff whitespace validation passed. The bundled Skill
validator passed after adding PyYAML to the locked development dependencies and
installing them in the isolated project environment with `uv sync --frozen --extra dev`.
Validation used `uv run --no-sync python /path/to/skill-creator/scripts/quick_validate.py
skills/ntulearn`; the validator is supplied by the host's skill-creator installation.
Independent read-only review passed the guide diff and seven synthetic evidence
scenarios covering classification, time boundaries, overlap and conflicts. This
instruction-level check is not a new real-course extraction evaluation.

## R1/R2 release-blocker follow-up

Comparison baseline: `ae40a3c`. R1 checks effective Git ignore and the index for
in-checkout private paths, retains linked-path and escape protections, and rejects
unverifiable boundaries with bounded errors. Direct database paths also validate
their containing directory to protect SQLite sidecars. No ignore rules or existing
private data are modified by boundary validation.

The maintainer selected GitHub Private vulnerability reporting for R2. Its workflow
is documented, but R2 remains a publication blocker until the repository exists,
the feature is enabled and its reporting entry is verified. No GitHub private
reporting feature has been enabled. Ordinary non-sensitive issues and pull
requests remain appropriate; private vulnerability details need a private channel.
R3–R6 remain deferred and are not implemented by this follow-up.

Focused Python 3.12 validation: 58 runtime-boundary/storage/browser-capture tests
passed, followed by 23 CLI/public-artifact tests. Changed Python files passed Ruff
lint/format and the two changed source modules passed mypy. These are synthetic
checks; the full suite, live acceptance and existing private database were not run
or changed for this follow-up.
Independent review accepted the R1 diff and R2's pending status; its separate
41-test boundary/storage run and additional synthetic probes passed. Wheel and
sdist rebuilds and public-content audits passed. These outcomes do not close R2
or authorize publication.
