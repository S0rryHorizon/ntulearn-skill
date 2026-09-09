# ADR 0002: SQLite/FTS5 metadata with filesystem binaries

- Status: Accepted
- Date: 2026-09-09

## Context

The system needs relational metadata, transactions, resource-version history, provenance, event
queries, full-text search, and migrations without operating a local service. It also needs to retain
potentially large original documents and browse them by course.

## Decision

Use SQLite as the canonical metadata and structured-text database, with FTS5 as the default derived
full-text index. Store original binaries and large derived artifacts in a private filesystem using
content-addressed immutable blobs plus a human-readable course view. Do not introduce an ORM or
vector database initially.

## Consequences

- Installation and backup remain simple and local.
- Foreign keys and bounded transactions preserve relationships; FTS can be rebuilt.
- Filesystem/database commit ordering and integrity recovery require deliberate implementation.
- One-writer SQLite constraints are acceptable for the expected desktop workload and can be
  measured later.

## Alternatives considered

- Store binaries as SQLite blobs: rejected because large files become awkward to browse, stream,
  and back up selectively.
- Run PostgreSQL plus a search/vector service: rejected as operationally heavy without a measured
  need.
- Use loose JSON metadata: rejected because transactions, typed relations, migrations, and event
  queries would be fragile.
