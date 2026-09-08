# Architecture overview

This document records only the Phase 0 boundaries. Detailed component and data-model decisions belong to later phases.

## Principles

### Local-first

The user's local store is the working source for indexing, search, event retrieval, and resource retrieval. The remote NTULearn source is used primarily for authorized synchronization rather than as a required dependency for every query.

### Core independent from AI integrations

Core behavior must be available without Codex, ChatGPT, or any LLM. The intended interface includes commands such as:

```text
ntulearn sync
ntulearn courses
ntulearn events
ntulearn search "quiz"
```

AI-specific integrations live in thin adapters under `integrations/` and depend on core interfaces, never the reverse.

### Private data outside Git

The preferred runtime root is `~/.ntulearn-skill/`, with separate configuration, authentication, database, index, cache, log, and course areas. An ignored repository-local `.local/` workspace may be used during development. Neither location is public project content.

### Incremental synchronization

Future synchronization should detect changes and avoid unnecessary downloads or reprocessing. The exact remote identifiers, change signals, and reconciliation strategy remain undecided until reconnaissance.

### Provenance-aware retrieval

Indexed records and extracted facts should retain enough source information to trace a result back to the authorized local resource and synchronization observation that produced it.

## Initial package boundaries

- `core`: product-level orchestration and stable domain-facing interfaces.
- `client`: future read-only NTULearn transport boundary.
- `sync`: incremental synchronization workflow.
- `parsers` and `extractors`: document interpretation and structured fact extraction.
- `storage`, `index`, and `search`: local persistence and retrieval.
- `cli`: LLM-independent command-line adapter.
- `integrations`: optional, thin agent-product adapters.

These are boundaries, not claims of implemented behavior.
