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

**Status:** Complete under synthetic implementation acceptance. M1–M9 accepted; real
transport compatibility remains a separate Phase 4 gate. See `implementation-status.md`.
The milestone order and acceptance criteria are defined in
`docs/architecture/testing-and-implementation.md`.

## Phase 4 — Validation

Test correctness, incremental behavior, provenance, failure recovery, privacy controls, and read-only safety using synthetic fixtures first and tightly controlled private data where necessary.

**Status:** Synthetic/runtime safety passed; current live transport validation is externally
blocked. The aggregate Phase 4 live gate remains open. Independent hardening work can
proceed without treating this as full release acceptance.

## Phase 5 — Open-source Hardening

Audit history and artifacts for sensitive data, finalize contributor and security guidance, validate packaging and documentation, and prepare a deliberate public release.

**Status:** Independent offline hardening passed. Locked dependencies, supported Python
validation, distribution checks, contributor/security documentation, and privacy review
are complete. The aggregate release gate remains blocked by current live validation;
remote GitHub CI and publication have not run. See `phase5-hardening.md`.
