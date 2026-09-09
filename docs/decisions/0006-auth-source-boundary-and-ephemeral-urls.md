# ADR 0006: Isolate auth/source transport and keep signed URLs ephemeral

- Status: Accepted
- Date: 2026-09-09

## Context

Normal reads depend on an authenticated browser session, but minimum credential requirements,
renewal, SSO lifecycle, and signed download lifetime are not established. NTULearn-specific routes
may change independently from storage and retrieval.

## Decision

Place authentication behind `SessionProvider` and NTULearn translation behind
`NtulearnSourceAdapter`/`SourceProvider`. Domain code receives typed records and coverage, not raw
transport. Acquire signed download routes only for an active fetch and never persist them as
identity or durable metadata.

## Consequences

- Authentication providers can change without rewriting domain services.
- Endpoint churn remains inside one adapter.
- Expiry produces typed failures and preserves local state; business code does not manipulate
  cookies or invent renewal.
- Adapter and log-redaction tests are security-critical.

## Alternatives considered

- Let each feature make authenticated requests: rejected because it leaks session mechanics across
  the product and frustrates read-only review.
- Persist signed URLs for reuse: rejected because they are sensitive, ephemeral transport details
  and unsuitable identity.
- Assume a known automatic SSO renewal flow: rejected because it is currently unverified.
