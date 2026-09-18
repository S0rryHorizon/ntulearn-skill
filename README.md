# ntulearn-skill

**English** | [简体中文](README.zh-CN.md)

`ntulearn-skill` is a local-first Python library and command-line interface for a
private NTULearn course mirror. It stores course metadata, downloaded resources,
parsed PDF/DOCX text, deterministic FTS5 search data, announcements, assessments,
event evidence, conflicts and synchronization state under a private runtime root.

> **Status: limited experimental retrieval candidate.** The current build supports a
> private local daily trial using the installed Skill and a bounded, host-assisted
> fresh-collection path. Experimental source code is available on GitHub; no package
> release has been published. Event extraction remains a preview:
> answers must retain source links and uncertain fields, and important dates,
> requirements and venues still need source or manual checks. Candidate repository
> and artifact checks describe their recorded validation scope; package publication
> remains a separate decision. See
> [daily-library validation](docs/development/daily-library-validation.md) and the
> [release checklist](docs/development/public-release-checklist.md).

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
- A host browser capture provider wired into the normal Core/CLI, with a thin
  [Codex browser skill](skills/ntulearn-browser/SKILL.md) for authorized collection.
- A separate [local question skill](skills/ntulearn/SKILL.md) for bounded Chinese or
  English daily queries over the same cache-only Core interfaces.

The Codex dispatcher is a Python integration surface. The browser skill requires a
supported connected host; this repository does not install a browser plugin, run an
LLM agent or provide a ChatGPT adapter.

## Current limits

The validated fresh path uses Codex on macOS with a connected Chrome browser, normal
visible UI reads and downloads, and the installed browser Skill. Standalone CLI and
daily-question Skill queries read the private local library; they do not log in or
drive a browser. The repository does not include automatic SSO, browser credential
extraction, cookie copying or session renewal. See the
[browser usage guide](docs/usage-browser.md).

The separate raw API integration remains unvalidated, but it is not a prerequisite
for this bounded browser-host-assisted trial. The same is true of automatic SSO and
unsupported formats. These deferred capabilities prevent broader compatibility
claims rather than blocking evaluation of the documented local retrieval lane.

Search is lexical rather than semantic. The built-in parsers cover PDF and DOCX;
PPTX text extraction remains unsupported, and ordinary page bodies, embedded
attachments, external modules, scanned-document fallback quality, complex DOCX
tables, large-list pagination and several remote freshness mechanisms remain partial,
unvalidated or deferred. The current private trial stored 66 originals and parsed 65
supported PDF/DOCX files. Fifty-eight chunks were flagged for possible visual review;
two have bounded supplements and 56 remain unreviewed. These counts describe processing
coverage, not course completeness.

Two frozen event evaluations show why extraction remains a preview. One original
source-local set recovered 8/8 mentions, 26/27 scored fields and 27/27 provenance
checks. A separately selected set recovered 13/16 mentions with zero extra event mentions,
but none of its 16 mentions was fully correct. Event type, time, venue and weak-identity
limitations remain. See [daily-library validation](docs/development/daily-library-validation.md)
for interpretation and the [capability ledger](docs/development/capability-ledger.md)
for the broader evidence boundary.

## Install from a local checkout

The package declares Python 3.11 or newer. The recorded integrated follow-up ran 556
tests plus static checks and package builds on Python 3.12. The earlier Phase 5 run of
463 tests on each of Python 3.11 through 3.14 is historical evidence and is not a
current follow-up compatibility matrix. These historical local results are not a
claim that the current [GitHub CI](https://github.com/S0rryHorizon/ntulearn-skill/actions)
passes. From this repository:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
ntulearn --help
```

This project has not been published to a package index. A locally built wheel can
be installed by path; the development workflow and exact validation commands are in
[the usage guide](docs/usage.md) and [contributor guide](CONTRIBUTING.md).

## Run the offline synthetic demo

With the local virtual environment active, install the optional development dependencies
(including `reportlab`) and run the standalone example:

```console
python -m pip install '.[dev]'
python examples/offline_demo.py
```

Each run creates and removes a new temporary private root. The example invents a course,
builds a three-page PDF and a local capture manifest, then uses the normal source provider,
Core sync/search and Codex dispatcher to show a match at physical page 2, its resource
version and original-text source locator. Repeating the sync keeps one version. A missing
term yields no local match, but the example reports `PARTIAL` source coverage and `UNKNOWN`
overall completeness, so absence from the remote course is unproven. Once installed, the
example needs no account, network access, real course data or existing
`~/.ntulearn-skill/` store. This synthetic path demonstrates local retrieval behavior;
real fresh collection still requires an
authorized supported host flow described in the [browser usage guide](docs/usage-browser.md).

## Query the private local store

The default runtime root is `~/.ntulearn-skill/`. Override it with `--root PRIVATE_ROOT`, then `NTULEARN_DATA_DIR`, then an absolute
`runtime_root` in the private host file `~/.ntulearn-skill/config.json`, in that
precedence order. A path inside
a Git checkout is rejected unless it is explicitly below that checkout's `.local/`
directory and Git confirms the private boundary is effectively ignored and contains
no tracked runtime content. The directory name alone is insufficient. If the Git
boundary cannot be verified, the operation fails with a privacy-safe error. See
[data boundaries](docs/privacy/data-boundaries.md) for the scope of this check.

```console
ntulearn --json courses
ntulearn library-status --course 1 --freshness cache-only
ntulearn materials 1 --freshness cache-only
ntulearn recent-materials 1 --days 14 --freshness cache-only
ntulearn search "synthetic optics" --course 1 --neighbors 1
ntulearn search "synthetic optics" --course 1 --current-only --freshness cache-only
ntulearn events --course 1 --show-conflicts
ntulearn source 7 --kind source_locator --context-window 1
ntulearn resource 3
```

`search --current-only` matches only the current resource version in the local store;
default search still includes historical versions. This flag does not refresh NTULearn
or prove the local version is the latest remote version. The Codex dispatcher accepts
the same opt-in as `{"query": "synthetic optics", "current_only": true}`.

For a selected course whose local processing exceeds the default 64-job run bound,
the configured browser-Skill workflow can repeat synchronization with a larger
`--max-jobs` value to resume the idempotent queue. A bare standalone CLI has no fresh
source engine. Use `library-status --course 1` to inspect remaining and failed local
work.

The integers are local opaque keys discovered from earlier results. They are not
NTULearn IDs or course codes. For example, an invented course may display the code
`PH0000`, while its CLI argument is still its returned `local_key`.

Local paths are omitted from results by default. `resource --include-local-path` and
`visual prepare --include-local-path` are the explicit local-path opt-ins. Query
commands default to `cache-only` and never access a remote source under that policy.

CLI exit codes have stable meanings: `0` complete result, `1` operational failure,
`2` partial/stale/unknown result, `3` complete empty result and `64` invalid usage.
Automation should inspect both the exit code and the versioned JSON envelope.

## Python API and source integration

The public Python facade is `ntulearn_skill.core.api.CoreService`. Local-only use can
start with `CoreService.from_runtime()`. Normal fresh collection is orchestrated by
the browser Skill on the supported host and then enters the same Core/CLI path. The
private browser-capture manifest is an integration boundary rather than something a
daily user needs to prepare. Raw API or custom integrations can instead supply a
`SyncEngine` composed from the typed `SessionProvider` and `SourceProvider` contracts.
The separate NTULearn API adapter accepts a guarded `ReadOnlyTransport`, but does not
create or recover an authenticated session.

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

Chinese readers can start with the [Chinese README](README.zh-CN.md),
[browser usage guide](docs/usage-browser.zh-CN.md),
[contributor guide](CONTRIBUTING.zh-CN.md) and [security policy](SECURITY.zh-CN.md).
Detailed architecture and API references below are currently in English.

- [Usage and integration](docs/usage.md)
- [Architecture overview](docs/architecture/overview.md)
- [Implementation status](docs/development/implementation-status.md)
- [Packaging validation](docs/development/packaging-validation.md)
- [Development phases](docs/development/phases.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

The source is licensed under the [MIT License](LICENSE).
