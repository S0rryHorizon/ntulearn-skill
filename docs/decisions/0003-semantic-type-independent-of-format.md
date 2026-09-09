# ADR 0003: Separate semantic material type from physical format

- Status: Accepted
- Date: 2026-09-09

## Context

An instructional role such as lecture slides can be delivered as PDF or PPTX, while one physical
format can contain syllabi, readings, tutorials, or assessment information. The bounded evidence
validates PDF as a current important format but does not establish that extension as course meaning.

## Decision

Model `MaterialClassification.semantic_type` independently from
`ResourceVersion.file_format`. Select parsers from detected bytes. Classify instructional purpose
from source context, titles, metadata, and extracted content, retaining method and confidence.

## Consequences

- Future PPTX compatibility does not require changing the course-material model.
- Classification can be unknown, revised, or user-confirmed without moving binary identity.
- Search can filter by both purpose and format.
- A separate classification pipeline and audit record are required.

## Alternatives considered

- Infer semantic type from extension: rejected because it confuses representation with purpose.
- Hard-code directory labels as source truth: rejected because course-authored structure varies and
  classifications may change.
