# Security policy

## Release and support status

`ntulearn-skill` has no supported public release yet. The current source tree is
pre-release and the public release gate is **NOT READY**. Security fixes are evaluated
against the current maintained source; no compatibility or response-time commitment is
made for unpublished versions.

## Reporting a vulnerability

GitHub **Private vulnerability reporting** is enabled for
[S0rryHorizon/ntulearn-skill](https://github.com/S0rryHorizon/ntulearn-skill).
The repository currently contains no project code or commits; enabling this reporting
channel does not constitute a software release.

For ordinary bugs, feature requests, and non-sensitive fixes, please open a public
issue or pull request.

If you discover a security vulnerability that has not yet been fixed, please do not
disclose the details in a public issue or pull request. Instead, use GitHub's
**Private vulnerability reporting** feature: open **Security and quality → Advisories
→ Report a vulnerability**, or use the
[private report form](https://github.com/S0rryHorizon/ntulearn-skill/security/advisories/new).
Sign in to GitHub when prompted. The
[Advisories page](https://github.com/S0rryHorizon/ntulearn-skill/security/advisories)
provides the same reporting entry.

Please avoid including real credentials, private data, or other unnecessary sensitive
information in the report. Never include credentials, private data, authenticated
URLs, private course materials, logs, database content or real NTULearn reproduction
artifacts in public issues or pull requests.

Include a concise impact description, the affected revision, synthetic reproduction
steps and any proposed mitigation. Replace all real identifiers and content with invented
values. If a private artifact is essential, first ask the maintainer how to transfer it;
do not attach it to an issue, commit or pull request.

## Security boundaries

The project is designed around these controls:

- Runtime data defaults to the owner-local `~/.ntulearn-skill/` tree. Repository paths
  require an explicit path below `.local/`, verified effective Git ignore protection
  and no tracked runtime content. An unverifiable Git boundary is rejected with a
  privacy-safe error; the directory name alone is not sufficient. These checks do
  not prevent later ignore-rule changes or forced staging, so staged privacy review
  remains necessary.
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
