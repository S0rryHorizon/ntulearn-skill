# ntulearn-skill

`ntulearn-skill` is a local-first Python library and command-line interface for a
private NTULearn course mirror. It stores course metadata, downloaded resources,
parsed PDF/DOCX text, deterministic FTS5 search data, announcements, assessments,
event evidence, conflicts and synchronization state under a private runtime root.

> **Release status: NOT READY FOR PUBLIC RELEASE.** Phase 3 implementation passed
> its synthetic acceptance gates. Phase 4 synthetic and runtime-safety validation
> passed, but current end-to-end live NTULearn transport validation is blocked and
> remains **NOT VALIDATED**. Phase 5 offline hardening passed; the live gate and final human release decision
> remain open. See the [release checklist](docs/development/public-release-checklist.md).

## What is implemented

- A standalone, LLM-independent `CoreService` and `ntulearn` CLI.
- Private SQLite metadata and FTS5 indexes plus content-addressed resource storage.
- PDF and DOCX parsing with source locators; unsupported formats remain explicit.
- Deterministic lexical search with bounded neighboring chunks.
- Source-backed announcements, assessments and event candidates.
- Provenance-first event reconciliation that preserves unresolved conflicts and
  manual local decisions.
- Bounded incremental synchronization through injected `SessionProvider` and
  `SourceProvider` implementations.
- Versioned JSON result envelopes that report freshness, coverage, conflicts,
  provenance, warnings and privacy-safe errors.
- A thin Python `CodexToolDispatcher` over the same core API.

The Codex dispatcher is a Python integration surface. This repository does not
install a Codex plugin, run an LLM agent or provide a ChatGPT adapter.

## Current limits

Synthetic and offline acceptance does not establish live NTULearn compatibility.
The repository does not include automatic SSO, browser credential extraction,
cookie copying or session renewal. A live integration must inject a currently
authorized session provider and a guarded read-only source/transport. Real resource
download routing is also unavailable until that integration supplies a validated
fresh-route factory.

Search is lexical rather than semantic. The built-in parsers cover PDF and DOCX;
real PPTX, scanned-document fallback quality, complex DOCX tables, ordinary schedule
coverage, large-list pagination and several remote freshness mechanisms remain
unvalidated or deferred. See the [capability ledger](docs/development/capability-ledger.md)
for the complete evidence boundary.

## Install from a local checkout

The package declares Python 3.11 or newer; the current validation matrix covers
CPython 3.11 through 3.14. From this repository:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
ntulearn --help
```

This project has not been published to a package index. A locally built wheel can
be installed by path; the development workflow and exact validation commands are in
[the usage guide](docs/usage.md) and [contributor guide](CONTRIBUTING.md).

## Query the private local store

The default runtime root is `~/.ntulearn-skill/`. Set `NTULEARN_DATA_DIR` or pass
`--root PRIVATE_ROOT` to use another private path. A path inside a Git checkout is
rejected unless it is explicitly below that checkout's ignored `.local/` directory.

```console
ntulearn --json courses
ntulearn materials 1 --freshness cache-only
ntulearn search "synthetic optics" --course 1 --neighbors 1
ntulearn events --course 1 --show-conflicts
ntulearn source 7 --kind source_locator --context-window 1
ntulearn resource 3
```

The integers are local opaque keys discovered from earlier results. They are not
NTULearn IDs or course codes. For example, an invented course may display the code
`PH0000`, while its CLI argument is still its returned `local_key`.

Local paths are omitted from results by default. Only `resource --include-local-path`
opts into displaying a private local path. Query commands default to `cache-only` and
never access a remote source under that policy.

CLI exit codes have stable meanings: `0` complete result, `1` operational failure,
`2` partial/stale/unknown result, `3` complete empty result and `64` invalid usage.
Automation should inspect both the exit code and the versioned JSON envelope.

## Python API and source integration

The public Python facade is `ntulearn_skill.core.api.CoreService`. Local-only use can
start with `CoreService.from_runtime()`. Sync and fetch calls require a `SyncEngine`
constructed with caller-supplied implementations of the typed `SessionProvider` and
`SourceProvider` contracts. The repository's NTULearn adapter accepts a guarded
`ReadOnlyTransport`, but it does not create or recover an authenticated session.

See [usage](docs/usage.md) for verified method signatures and a minimal composition
example. Source integration must remain within the read-only policy described in
[authentication and source boundaries](docs/architecture/authentication-and-source-boundaries.md).

## Privacy and safety

This repository may contain source code, schemas, documentation and fully synthetic
examples only. Credentials, cookies, tokens, authenticated captures, real course
identifiers, real course metadata, downloaded materials, private databases, indexes,
caches and logs must remain outside Git. The preferred runtime root is
`~/.ntulearn-skill/`; `.local/` is an ignored development-only alternative.

Read [data boundaries](docs/privacy/data-boundaries.md) before creating fixtures or
sharing diagnostics. Security reports should follow [SECURITY.md](SECURITY.md).

## Project documentation

- [Usage and integration](docs/usage.md)
- [Architecture overview](docs/architecture/overview.md)
- [Implementation status](docs/development/implementation-status.md)
- [Packaging validation](docs/development/packaging-validation.md)
- [Development phases](docs/development/phases.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

The source is licensed under the [MIT License](LICENSE).
