# Architecture review checklist

This checklist records Phase 2 invariants and should be rerun when the schema or provider behavior
changes.

- [x] The content model is a generic tree and does not require every course to have weeks.
- [x] Calendar is one source among announcements, assessments, content metadata, and document bodies.
- [x] Remote identifier namespaces are represented by distinct types.
- [x] Semantic material type is separate from physical file format.
- [x] Publication, modification, availability, due, start, end, observation, and sync times are distinct.
- [x] File-backed provenance reaches an immutable version and page/slide/section/element locator.
- [x] Search can index lightweight chunks and hydrate only nearby local chunks.
- [x] Update detection does not rely on an unverified ETag or redirect metadata.
- [x] Signed URLs are ephemeral and excluded from durable storage and identity.
- [x] A missing observation never deletes local evidence or proves remote deletion.
- [x] Coverage and freshness accompany empty or partial query results.
- [x] Authentication and NTULearn transport details are isolated behind adapters.
- [x] CLI and AI integrations call the core rather than contain source logic.
- [x] Semantic search is optional; SQLite FTS5 is the default.
- [x] CI uses only synthetic/mock data and never needs real credentials or network access.
- [x] Future source support is limited to a small provider/evidence boundary, not a speculative framework.
- [x] Public examples use invented courses and identifiers only.
- [x] Phase 2 contains documentation and decisions, not product implementation.
