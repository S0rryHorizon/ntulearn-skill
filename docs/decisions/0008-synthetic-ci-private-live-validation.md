# ADR 0008: Synthetic public CI and isolated private live validation

- Status: Accepted
- Date: 2026-09-09

## Context

Core behavior must be testable without user credentials or course data. Some platform capabilities
can only be validated against an authorized live account, but those observations and artifacts are
private and unsuitable for GitHub CI.

## Decision

Use three layers: fully synthetic unit tests, integration tests with invented/mock provider data,
and explicitly invoked private live validation under ignored local storage. Public CI has no
NTULearn credentials, performs no live access, and treats absent private data as normal rather than
a reason to skip core coverage.

## Consequences

- Contributors can run deterministic tests safely.
- Public fixtures and logs require a privacy review before commit.
- Live platform assumptions remain in an evidence/capability ledger and may stay unknown.
- Synthetic parser or adapter success cannot be described as live integration validation.

## Alternatives considered

- Run credentialed live tests in GitHub CI: rejected because it expands secret exposure and makes
  tests account-dependent.
- Test only against live data locally: rejected because the core would be irreproducible for
  contributors.
- Convert real responses into fixtures: rejected because de-identification may be incomplete;
  fixtures must be invented from scratch.
