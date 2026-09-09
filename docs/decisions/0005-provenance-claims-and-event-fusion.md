# ADR 0005: Provenance-first claims and event fusion

- Status: Accepted
- Date: 2026-09-09

## Context

Important course events can appear in announcements, assessment metadata, calendar data, and exact
document pages or sections. Sources may complement, contradict, or supersede each other. Flattening
them into one row would erase evidence and make conflict handling opaque.

## Decision

Keep exact `SourceLocator`, source-specific `EventSource`, and atomic `Claim` records. Build the
canonical `Event` as a projection of accepted field-level claims. Resolve identity conservatively
using course, type, typed relations, time, title, and context. Preserve conflicts and explicit
supersession links with versioned decision audits.

## Consequences

- Answers can cite page/slide/section or structured source objects precisely.
- A moved date can replace one field without erasing the earlier claim or unrelated detail.
- Ambiguous candidates remain unresolved instead of being falsely merged.
- Extraction and reconciliation require more tables and explainable algorithms.

## Alternatives considered

- Use Calendar as the event source of truth: rejected because bounded evidence shows it can omit
  real arrangements.
- Apply one global source ranking: rejected because authority depends on field specificity,
  timestamp semantics, explicit update language, and source relation.
- Store only current event values: rejected because it loses auditability and superseded evidence.
