# ADR 0007: Deterministic retrieval first; semantic search optional

- Status: Accepted
- Date: 2026-09-09

## Context

Title, filename, course, event, and extracted-text queries need reliable local behavior. Semantic
search may improve paraphrase recall, but cloud embeddings introduce privacy/network dependencies
and a vector service would increase operational weight.

## Decision

Use relational filters plus SQLite FTS5 as the default search path. Hydrate exact local chunks and
small neighbor windows after ranking. Define an opt-in `SemanticIndexProvider` that augments
candidate recall but does not replace evidence hydration, provenance, or deterministic filters.

## Consequences

- Baseline search works offline with no API key or vector database.
- Semantic providers can be local or explicitly configured remote options with declared privacy
  behavior.
- FTS may miss some paraphrases until an optional provider is enabled.
- Semantic scores are treated as retrieval hints, never evidence.

## Alternatives considered

- Require cloud embeddings: rejected because it violates the local-first baseline.
- Run a dedicated vector database by default: rejected without scale or performance evidence.
- Omit a semantic boundary entirely: rejected because future paraphrase search is a valid optional
  capability and benefits from a stable interface.
