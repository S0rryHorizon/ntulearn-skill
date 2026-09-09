# Security policy

## Release and support status

`ntulearn-skill` has no supported public release yet. The current source tree is
pre-release and the public release gate is **NOT READY**. Security fixes are evaluated
against the current maintained source; no compatibility or response-time commitment is
made for unpublished versions.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the maintainer through a private channel
the maintainer has established with you. No public security-report address or repository
issue tracker has been designated yet, so this document intentionally does not invent
one. Until a human release establishes a reporting channel, do not open a public report
that includes possible credentials, authenticated URLs, private course information,
logs, database content or reproduction artifacts derived from real NTULearn access.

Include a concise impact description, the affected revision, synthetic reproduction
steps and any proposed mitigation. Replace all real identifiers and content with invented
values. If a private artifact is essential, first ask the maintainer how to transfer it;
do not attach it to an issue, commit or pull request.

## Security boundaries

The project is designed around these controls:

- Runtime data defaults to the owner-local `~/.ntulearn-skill/` tree. Repository paths
  are rejected unless an explicit path is below the ignored `.local/` directory.
- Remote access is mediated by typed session/source contracts and an allowlisted
  `ReadOnlyTransport`; unsupported or malformed operations fail before dispatch.
- Signed and preview URLs are ephemeral transport details and must not be persisted as
  identity, metadata or logs.
- Public result envelopes use bounded error categories and omit local filesystem paths
  by default.
- Local originals, versions and provenance are retained when a remote read is partial,
  fails or omits an item.

The package does not extract browser credentials, copy cookies, automate SSO or renew
sessions. It does not encrypt the runtime database or downloaded resources at rest, and
PDF/DOCX decoders are not process-isolated. Protect the host account and private runtime
directory accordingly. A successful synthetic test run is not evidence that a live
NTULearn transport or every document format is safe and compatible.

## Accidental disclosure

If private material enters a working tree, built artifact or Git history, stop sharing
the repository and notify the maintainer privately. Preserve enough local evidence to
identify the affected paths and revisions, but do not copy the sensitive values into a
new report. Removing a file from the latest tree does not remove it from Git history;
history and generated distributions must both be audited before any release resumes.
