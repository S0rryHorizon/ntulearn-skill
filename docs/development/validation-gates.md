# Implementation validation gates

These checks operationalize the accepted architecture; passing a mock check does
not establish live compatibility. Each milestone also requires its tests in
`../architecture/testing-and-implementation.md` and an independent diff review.

| Slice | Required adversarial evidence |
| --- | --- |
| M1 | Identical opaque values across identifier namespaces; migration checksum drift and injected rollback; foreign keys on every connection; private default paths |
| M2 | Interrupt before blob placement, before database commit, and before browse update; equal bytes after rename; changed bytes; duplicate filenames; verified copy fallback |
| M3 | GET-shaped unsafe operation refused before dispatch; redirect policy; repeated cursor, empty intermediate page, cap, tree cycle and session expiry; partial observations retained |
| M4 | Physical versus printed PDF pages; table-only DOCX; settings/version cache invalidation; HTML disguised as PDF; scan diagnostics; non-destructive derived text |
| M5 | Relational/index rollback; equivalent rebuild; Unicode and punctuation; cross-course filters; bounded chunk hydration without reading originals |
| M6 | Event in attachment with empty calendar; different publication/availability/close/due times; date/week precision; resolvable field provenance |
| M7 | Cross-course and numbered-event negatives; ambiguous matches; vague versus precise evidence; venue-only update; cancellation; retained manual decisions and audit |
| M8 | Partial omission, complete omission, repeated omission and reappearance; changed bytes with unchanged metadata; exact-window freshness; interrupted/repeated jobs |
| M9 | Empty complete versus incomplete results; typed errors and exit codes; no remote calls for cache-only; at most one targeted refresh; core without AI extras |

## Interpretation of existing invariants

An object directly observed readable can become `ACTIVE` even when another page
fails. Its enclosing scope remains `PARTIAL`. Omission-based lifecycle changes
require a complete comparable inventory. This follows the observation transitions
in the synchronization design and does not promote partial inventory coverage.

The content-addressed store and committed version metadata are authoritative.
Browse paths are a recoverable view. A post-commit view-update failure must be
reported and repaired from committed versions without deleting or reverting them.

## Phase 4

Run end-to-end synthetic workflows without credentials or network dependencies.
Include restart recovery, repeated sync, exact source resolution, conflicts,
staleness and authentication failures. No missing-credential skips in public tests.
Use invented secret canaries to check error/log/metadata redaction. Record private
live checks separately with bounded coverage and explicitly gated capabilities.

## Phase 5

Audit tracked content and every Git revision, wheel and sdist contents, fixture
provenance, dependency declarations, installation, CLI documentation, contributor
and security guidance, license and CI. Verify the final tree is clean and no remote
or publication occurred. Only then evaluate the human public-release gate.

Failed parser attempts remain immutable audit records and are not reusable cache entries.
The version/parser/engine/settings key identifies at most one reusable terminal result;
a transient failure can be retried without inventing new settings. Ordinary Python
diagnostics are discarded within serialized parser/fallback calls; this is not process isolation.

## Early history privacy regression

At M6 commit `c2fa638`, an independent read-only preflight examined all 8 reachable
commits (165 unique historical blobs), 98 tracked working-tree files, and 35 ignore
probes. No prohibited private source data or unexpected binary artifacts were
detected. Synthetic secret canaries were reviewed as such. This is not final
release acceptance: final history, wheel/sdist contents, dependencies and later
milestone changes still require inspection. Git author attribution is retained.
