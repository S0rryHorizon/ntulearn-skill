# Development phases

## Phase 0 — Repository Bootstrap

Create the public project skeleton, privacy boundary, minimal packaging, documentation entry points, local-only workspace convention, and clean initial commit.

**Current scope:** Phase 0 only.

## Phase 1 — NTULearn Reconnaissance

With explicit authorization, observe only the minimum read-only behavior needed to understand supported synchronization. Label findings as `CONFIRMED`, `OBSERVED`, `HYPOTHESIS`, or `UNKNOWN`. Store raw requests, real metadata, internal identifiers, and download samples only in `.local/recon/`. Move only reviewed and de-identified conclusions into `docs/development/`.

## Phase 2 — Architecture Design

Turn confirmed reconnaissance into domain models, data flows, storage and provenance schemas, synchronization rules, security controls, and architectural decisions. Do not design around unverified platform assumptions.

## Phase 3 — Implementation

Implement the smallest validated vertical slices, preserving a standalone core and CLI with optional thin AI adapters.

## Phase 4 — Validation

Test correctness, incremental behavior, provenance, failure recovery, privacy controls, and read-only safety using synthetic fixtures first and tightly controlled private data where necessary.

## Phase 5 — Open-source Hardening

Audit history and artifacts for sensitive data, finalize contributor and security guidance, validate packaging and documentation, and prepare a deliberate public release.
