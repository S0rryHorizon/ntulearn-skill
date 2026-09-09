# ADR 0004: Separate resource, observation, version, and lifecycle

- Status: Accepted
- Date: 2026-09-09

## Context

A logical attachment may keep its source identity while its filename, metadata, or bytes change.
Remote modification fields are candidate signals, original validators are unverified, and one
missing observation may reflect incomplete pagination or temporary access rather than deletion.

## Decision

Represent a logical `Resource`, per-sync `ResourceObservation`, and immutable hash-verified
`ResourceVersion` separately. Use metadata-first fetch decisions plus configurable hash
verification. Track `ACTIVE`, `MISSING`, `UNAVAILABLE`, `REMOVED_CONFIRMED`, and `UNKNOWN` without
deleting local versions automatically.

## Consequences

- Metadata-only changes do not create false binary versions; changed hashes preserve history.
- Current-version promotion requires a completed, verified local write.
- Some unchanged decisions remain explicitly assumed until periodic verification.
- Storage grows over time and needs a future explicit retention workflow.

## Alternatives considered

- Overwrite one file per resource: rejected because it destroys historical evidence.
- Treat modification metadata or redirect headers as definitive versions: rejected because their
  semantics are not validated.
- Delete after one absent sync: rejected because absence does not prove removal.
