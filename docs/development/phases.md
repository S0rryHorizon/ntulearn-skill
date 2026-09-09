# Development phases

## Phase 0 — Repository Bootstrap

Create the public project skeleton, privacy boundary, minimal packaging, documentation entry points, local-only workspace convention, and clean initial commit.

**Status:** Complete.

## Phase 1 — NTULearn Reconnaissance

With explicit authorization, observe only the minimum read-only behavior needed to understand supported synchronization. Label findings as `CONFIRMED`, `OBSERVED`, `HYPOTHESIS`, or `UNKNOWN`. Store raw requests, real metadata, internal identifiers, and download samples only in `.local/recon/`. Move only reviewed and de-identified conclusions into `docs/development/`.

**Status:** Complete with documented limitations. Raw evidence remains private and ignored.

## Phase 2 — Architecture Design

Turn confirmed reconnaissance into domain models, data flows, storage and provenance schemas, synchronization rules, security controls, and architectural decisions. Do not design around unverified platform assumptions.

**Status:** Complete. See `docs/architecture/` and `docs/decisions/`. No product functionality was
implemented in this phase.

## Phase 3 — Implementation

Implement the smallest validated vertical slices, preserving a standalone core and CLI with optional thin AI adapters.

**Status:** In progress. M1–M5 accepted; M6–M9 pending. See `implementation-status.md`.
The milestone order and acceptance criteria are defined in
`docs/architecture/testing-and-implementation.md`.

## Phase 4 — Validation

Test correctness, incremental behavior, provenance, failure recovery, privacy controls, and read-only safety using synthetic fixtures first and tightly controlled private data where necessary.

## Phase 5 — Open-source Hardening

Audit history and artifacts for sensitive data, finalize contributor and security guidance, validate packaging and documentation, and prepare a deliberate public release.
