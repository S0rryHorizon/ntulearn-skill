# Public release checklist

Current assessment (2026-09-10): **limited experimental retrieval candidate for a
private local daily trial; publication pending**.

This status applies to the documented browser-host-assisted collection and local
retrieval lane. It is not an unqualified public-release approval. No project code,
package, tag or announcement has been published, and no local Git remote has been
added. The maintainer's final software-publication decision remains open.

## Current evidence snapshot

The R1/R2 follow-up uses `ae40a3c` as its comparison baseline. Earlier PASS entries
below describe their recorded versions and scope, not a new full-suite or live run.

- [x] R2: GitHub Private vulnerability reporting is enabled for
  [S0rryHorizon/ntulearn-skill](https://github.com/S0rryHorizon/ntulearn-skill).
  The official API returned `enabled: true`; the public Advisories page showed
  **Report a vulnerability**. The actual entry and workflow are in `SECURITY.md`.
  This was a separately authorized empty-repository setup: `isEmpty: true`, no
  default branch commit, no uploaded code or artifacts, and no local remote.
  No test vulnerability was submitted. Software publication remains unauthorized.
- [ ] R3–R6: retained for a later scoped review; no implementation or completion
  claim is included in the R1/R2 follow-up.

| Area | Current evidence | State |
| --- | --- | --- |
| Local Core, CLI and Skills | Daily Chinese/English questions use the same typed local Core interfaces and source locators | **VERIFIED FOR BOUNDED TRIAL** |
| Fresh collection host | macOS Codex with connected Chrome; normal visible reads and downloads through the browser Skill | **VERIFIED FOR BOUNDED TRIAL** |
| Latest integrated build | 556 tests on Python 3.12, static checks and package builds | **PASS** |
| Original frozen event set | 8/8 mentions, 26/27 scored fields and 27/27 provenance checks | **BOUNDED PASS** |
| Independent event set | 13/16 mentions, zero extra event mentions and 0/16 fully correct mentions | **PARTIAL** |
| Library processing | 66 originals; 65 supported PDF/DOCX files parsed; one PPTX retained as unsupported | **PARTIAL COVERAGE** |
| Visual review | 58 chunks flagged; two supplemented; 56 not reviewed | **PARTIAL** |
| Candidate repository, history and distribution audit | Current local candidate checked; repeat after any later release changes | **PASS** |
| Human publication decision | Explicit maintainer review and authorization required | **PENDING** |

The event samples do not estimate recall across all courses. Event type, time, venue
and weak-identity errors remain, so event output is a preview and important answers
must be checked against exact sources or manual evidence. Full interpretation is in
[daily-library validation](daily-library-validation.md).

## Verified bounded lane

- [x] Keep credentials, captures, original files, private databases, indexes and
  evaluation labels outside Git.
- [x] Use the installed browser Skill for bounded fresh collection on the validated
  macOS Codex and connected Chrome host.
- [x] Use standalone CLI and daily-question Skill commands for local retrieval; these
  commands do not log in or control Chrome.
- [x] Preserve immutable originals, source hashes, physical-page locators, extraction
  history, conflicts and explicit uncertainty.
- [x] Report cached observation freshness separately from a current remote read.
- [x] Keep known-past dates, week-only statements, ambiguous dates and unresolved
  identities visible rather than inventing exact instants.
- [x] Run the latest integrated Python 3.12 suite, static checks and package builds.

The raw API adapter, automatic SSO and unsupported formats are not prerequisites for
this lane. They remain deferred and prevent broader product or compatibility claims.

## Known coverage limits

- [ ] Establish broader completeness for ordinary course-page bodies, folder
  descriptions, embedded attachments and external interactive modules.
- [ ] Add supported PPTX text extraction and validate representative legacy or office
  formats.
- [ ] Expand visual inspection beyond the two bounded supplements; flagged or
  unreviewed chunks cannot be treated as fully understood.
- [ ] Validate large-list pagination, remote delta/validator behavior, changed remote
  bytes and real authentication expiry across representative courses.
- [ ] Improve event type, time and venue extraction and resolve weak identities without
  merging unrelated events.
- [ ] Validate the separate raw API protocol and automatic session renewal if those
  capabilities are later offered.

These items may remain deferred for a clearly labelled experimental candidate. They
must remain explicit in user-facing documentation and release notes.

## Historical evidence that needs current revalidation

Phase 5 previously ran 463 tests on each of Python 3.11 through 3.14, a dependency
advisory audit, packaging checks and privacy review. That evidence applies to the
earlier Phase 5 state. It has not been rerun as a current 3.11–3.14 matrix or current
dependency audit after the daily-library follow-up.

Before claiming current multi-version or dependency-audit coverage:

- [ ] Re-run the full current suite on every claimed Python version.
- [ ] Re-run the dependency advisory audit against the intended release lock state.
- [ ] Record exact current commands, resolved dependencies and artifact hashes in
  [packaging validation](packaging-validation.md).

## Current candidate repository and artifact gates

The checks below apply to this local candidate. Repeat them after subsequent release
changes; passing them does not authorize publication.

- [x] Freeze the reviewed local candidate changes and ensure the working tree contains only
  reviewed changes.
- [x] Audit every reachable Git revision and unique historical blob for credentials,
  authenticated data, real course metadata/materials, private logs and unexpected
  binaries.
- [x] Audit the final staged diff and verify ignore rules for `.local/`, private runtime
  data, credentials, captures, databases, indexes, caches, logs and builds.
- [x] Build wheel and sdist from the candidate state, inspect their complete contents and
  metadata, and scan them independently from the working tree.
- [x] Install the candidate wheel in a clean environment and verify the console entry
  point, documented commands, empty-store behavior and stable exit codes.
- [x] Check all public documentation links, examples and claims against current CLI
  help and implementation behavior.
- [x] Confirm generated metadata and errors contain no private paths, usernames,
  signed URLs or source excerpts.
- [ ] Have the maintainer review the evidence, remaining limitations, security channel,
  package metadata and intended public destination.
- [ ] Receive explicit authorization before creating a public remote, pushing,
  publishing a package, tagging a release or announcing availability.

Until the human decision and any required publication-state rechecks are complete, describe the project as
a **limited experimental retrieval candidate** and say that publication is pending.
