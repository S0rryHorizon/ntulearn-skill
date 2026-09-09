# ntulearn-skill

Local-first NTULearn course synchronization, indexing and retrieval for AI agents.

> **Status: Phase 3 in progress — M1–M4 accepted.** Typed identities, private runtime
> paths, SQLite migrations, immutable resources, PDF/DOCX parsing, classification, and local
> chunk retrieval are tested with synthetic data. Read-only course/content sync works through
> injected session and transport providers; a concrete live NTULearn connection remains unvalidated.

## Motivation

Course information and learning resources are often scattered across pages, attachments, and deadlines. This project aims to create a private local mirror with traceable retrieval interfaces that work for people, command-line tools, and AI agents.

## Planned capabilities

- Synchronize authorized course information and attachments from NTULearn.
- Parse common course documents and extract important academic events.
- Maintain Course, Resource, and Event indexes with incremental updates.
- Support full-text and semantic retrieval with source provenance.
- Expose stable core and command-line interfaces plus thin Codex, ChatGPT, and other agent adapters.

These capabilities are plans, not current functionality.

## Architecture principles

- Local-first storage and retrieval.
- Read-only interaction with NTULearn by default.
- A core and CLI that do not depend on any LLM product.
- Thin, optional AI integration layers.
- Incremental synchronization and provenance-aware results.
- Privacy by default, with private data kept outside Git.

See [the architecture overview](docs/architecture/overview.md) for the system design, evidence
boundary, and detailed architecture documents.

## Privacy

This public repository is for source code, documentation, schemas, and fully synthetic examples only. Real credentials, authenticated traffic, course materials, personal course records, databases, indexes, caches, and logs must never be committed. Private runtime data should live under `~/.ntulearn-skill/`; the ignored `.local/` directory is available only as a development workspace.

See [data boundaries](docs/privacy/data-boundaries.md) before adding fixtures, logs, or captured data.

## Development roadmap

1. Phase 0 — Repository Bootstrap (complete)
2. Phase 1 — NTULearn Reconnaissance (complete with documented limitations; private evidence)
3. Phase 2 — Architecture Design (complete)
4. Phase 3 — Implementation (M1–M4 complete; M5 in progress; M6–M9 pending)
5. Phase 4 — Validation
6. Phase 5 — Open-source Hardening

The phase definitions and gates are documented in [development phases](docs/development/phases.md).

See [implementation status](docs/development/implementation-status.md) for acceptance gates
and [capability limits](docs/development/capability-ledger.md) for deferred live validation.
