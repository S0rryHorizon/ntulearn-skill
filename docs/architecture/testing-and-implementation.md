# Testing strategy and Phase 3 implementation plan

## Test boundaries

### Unit tests

Unit tests are fully synthetic and exercise typed identifiers, filename safety, state machines,
metadata fingerprinting, hash/version decisions, time semantics, parser normalization, provenance
locators, event matching, claim supersession, freshness, and coverage-aware result wording. They do
not require a browser, credentials, network, or private runtime data.

Property-based or generated cases are especially useful for unsafe filenames, duplicate names,
content-tree depth, pagination state, temporal precision, and conflicting claims.

### Integration tests

Integration tests use invented courses such as `PH0000 — Example Physics Course` and sanitized/mock
provider responses. They cover:

- multi-page course and content traversal, including incomplete-page failures;
- folders, modules, direct files, document containers, attachments, and custom items;
- metadata-only observations followed by equal and changed binary hashes;
- SQLite migrations, transactions, FTS5 rebuilds, and local browse views;
- synthetic PDF/DOCX documents and later synthetic PPTX compatibility fixtures;
- announcements, assessments, schedule/due separation, event extraction, conflicts, and
  supersession;
- end-to-end local search and source resolution;
- session expiry and signed-route redaction using fake values.

Fixtures are built from invented content, not transformed copies of real course material. Snapshot
files receive a manual privacy review before commit.

### Private live validation

Real NTULearn validation runs only with explicit authorization in an ignored `.local/` environment
or the private runtime root. It never runs in public CI and never writes raw evidence into tracked
paths. The live suite is capability-oriented and read-only:

- compare adapter translations with normal accessible views;
- verify pagination completeness on available larger samples;
- test real binary revision/validator behavior only when a naturally revised resource exists;
- validate session expiry/renewal without extracting or weakening credentials;
- validate new parser formats only when an authorized original is naturally encountered;
- record de-identified `CONFIRMED`, `OBSERVED`, `HYPOTHESIS`, or `UNKNOWN` conclusions.

Synthetic compatibility success must never be reported as live platform validation.

## CI strategy

GitHub CI must be network-independent for core tests and must not define NTULearn secrets. The
baseline pipeline should:

1. install the package and locked development dependencies on supported Python versions;
2. check formatting and linting;
3. run static type checks once a type checker is selected;
4. run unit and synthetic integration tests;
5. build the distribution;
6. scan tracked/staged fixtures for prohibited private patterns and unexpected binary/database
   files;
7. render or inspect synthetic parser artifacts where regression risk warrants it.

No test may silently skip because real credentials are absent. Private live checks are separate,
manually invoked, and excluded from CI discovery by path and marker.

## Phase 3 vertical slices

Phase 3 should implement one reviewable slice at a time. Each milestone leaves the public test suite
green and does not depend on unfinished future layers.

### Milestone 1 — Domain, SQLite, and migration foundation

**Scope:** Typed identifier/value objects, time/coverage enums, private-root resolution, SQLite
connection policy, initial relational schema, migration runner, and repositories for synthetic
courses/content.

**Acceptance:** A fresh synthetic database migrates to the current version; a previous fixture
migrates forward transactionally; typed IDs cannot cross namespaces; no runtime file is created
inside Git by default.

**Tests:** Unit tests for value types and status rules; integration tests for migration checksums,
rollback, foreign keys, WAL readers, and private-root configuration.

**Dependencies:** Phase 2 schema and storage decisions only.

### Milestone 2 — Immutable resource storage and versioning

**Scope:** Safe filename policy, streaming hash ingestion, content-addressed blobs, browseable course
views, `Resource`, `ResourceObservation`, and `ResourceVersion` transactions.

**Acceptance:** Equal bytes reuse a version; changed bytes create a version; renamed metadata does
not lose history; duplicate filenames do not collide; failures leave neither current-pointer drift
nor exposed partial blobs.

**Tests:** Synthetic binaries, traversal/reserved-name cases, atomic-failure injection, hash and
hard-link/copy fallback checks, and resource lifecycle transitions.

**Dependencies:** Milestone 1.

### Milestone 3 — Read-only source boundary and discovery/content sync

**Scope:** `SessionProvider`, `ReadOnlyTransport`, `SourceProvider`, the initial NTULearn adapter
shape, course discovery, general content-tree traversal, typed pagination/coverage, and sync-run
observability. Public development uses mocks; live checks stay private.

**Acceptance:** A synthetic multi-shape course tree syncs without a week assumption; every page is
accounted for; a mid-pagination failure produces partial coverage; non-read operations are absent or
rejected; transport secrets are redacted.

**Tests:** Mock response integration tests, pagination caps/cursors, nested trees, unavailable items,
session-expiry behavior, and log-redaction assertions.

**Dependencies:** Milestones 1–2; a private live gate may validate the adapter after mock acceptance.

### Milestone 4 — PDF/DOCX parsing, classification, and partial retrieval

**Scope:** Parser registry, PDF page chunks, DOCX paragraph/table chunks, locators, parse caches,
classification interface, Stage B diagnostics, and selective fallback interfaces without requiring
a vision provider.

**Acceptance:** Synthetic PDF results cite physical pages; DOCX tables remain searchable; identical
version/parser/settings reuse a parse; a query loads only matching local chunks and configured
neighbors; native text is not overwritten by derived text.

**Tests:** Synthetic documents, parser failures, table and page locators, cache invalidation by
parser version/settings, and low-text-page diagnostic cases.

**Dependencies:** Milestone 2. PPTX contract tests may follow here, but live compatibility remains a
separate future gate.

### Milestone 5 — Deterministic full-text search

**Scope:** `search_document`, FTS5 schema, transactional indexing for courses/materials/chunks, typed
filters, ranking, source resolution, and coverage-bearing results.

**Acceptance:** Search by title, filename, semantic type, and body returns exact locators; rebuilding
FTS from relational data is lossless; missing coverage changes result wording/status; no semantic or
network service is required.

**Tests:** Ranking/filter fixtures, Unicode queries, stale/partial scopes, index rollback/rebuild, and
page-neighbor hydration.

**Dependencies:** Milestones 1 and 4.

### Milestone 6 — Announcements, assessments, and event candidates

**Scope:** Separate announcement, assessment, schedule, and due-item ingestion; distinct temporal
fields; event-source extraction with reproducible provenance.

**Acceptance:** A synthetic event may be found in an attachment when the calendar is empty; due,
availability, publication, and start times remain distinct; every candidate field resolves to a
source object or document locator.

**Tests:** Complementary-source fixtures, missing-calendar cases, assignment/test subtype cases,
date-only precision, and extractor-version idempotency.

**Dependencies:** Milestones 3–5.

### Milestone 7 — Event identity, claims, conflicts, and supersession

**Scope:** Conservative candidate matching, `Claim`, `EventSource`, canonical event projections,
conflict state, supersession graph, decision audit, and manual resolution command in the core.

**Acceptance:** “Quiz 1” variants can merge when course/type/time/context agree; title similarity
alone cannot merge; an explicit later move supersedes only affected fields; unresolved conflicts are
returned without deleting evidence.

**Tests:** Positive/negative match matrices, ambiguous candidates, cancellations, venue changes,
field-specific precedence, rerun reproducibility, and manual-decision persistence.

**Dependencies:** Milestone 6.

### Milestone 8 — Incremental sync, quick sync, and freshness

**Scope:** Metadata fingerprints, verification intervals, per-scope `SyncState`, quick/course/all
modes, conservative missing states, targeted refresh, and idempotent downstream job planning.

**Acceptance:** Unchanged assumed versus hash-verified states are distinct; one omission never
deletes; partial traversal cannot mark removals; current-state queries can target relevant scopes;
historical local queries do not access the source.

**Tests:** Sync sequence/state-machine fixtures, equal/changed bytes, interrupted runs, stale scopes,
unsupported capabilities, reappearance, and retry-once retrieval behavior.

**Dependencies:** Milestones 2–7. Validated remote validators remain an optional later optimization.

### Milestone 9 — Stable core API, CLI, and thin AI adapter

**Scope:** Finalize result/error schemas, implement CLI commands and JSON mode, then add one thin AI
integration that calls the core. An optional semantic provider may be added only after deterministic
retrieval is measured.

**Acceptance:** CLI workflows expose freshness, coverage, conflicts, and provenance; AI integration
contains no source or parsing logic; disabling AI/semantic extras leaves all core capabilities
working; integration changes do not alter source/storage APIs.

**Tests:** CLI contract/golden tests with synthetic data, exit codes, JSON schema compatibility,
adapter boundary tests, and privacy-safe error presentation.

**Dependencies:** Milestones 1–8.

## Deferred validation ledger

Phase 3 should keep an explicit capability ledger for assumptions that remain open: long-term remote
identity, large-list pagination, real original-file validators and revisions, authentication renewal,
ordinary calendar identifier coverage, real PPTX, scanned PDFs/visual fallback quality, complex
DOCX tables, and legacy/office formats. Each entry records current evidence level, fallback,
validation trigger, and whether it blocks any milestone. None is silently promoted by implementation.
