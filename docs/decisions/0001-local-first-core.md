# ADR 0001: Local-first core with thin integrations

- Status: Accepted
- Date: 2026-09-09

## Context

Course content should remain usable when the remote service or an AI product is unavailable. Remote
access is comparatively expensive, freshness-sensitive, and authentication-bound. Codex, ChatGPT,
and other clients may change independently from course synchronization and retrieval.

## Decision

Make the private local store the working source for parsing, indexing, event retrieval, and content
retrieval. Remote access occurs through explicit synchronization. Put stable use cases in an
AI-independent core, expose them through a Python API and CLI, and keep AI integrations as thin
translation/presentation adapters.

## Consequences

- Ordinary historical/content queries can run offline and use exact local versions.
- Freshness and coverage must be modeled explicitly rather than hidden behind every query.
- Local storage, migration, and recovery become core product responsibilities.
- AI clients cannot bypass the core to scrape or parse remote data.

## Alternatives considered

- Query NTULearn for every question: rejected because it couples retrieval to authentication,
  increases remote reads, and weakens reproducibility.
- Put the workflow inside one AI skill: rejected because core behavior would depend on one product
  and be difficult to test independently.
