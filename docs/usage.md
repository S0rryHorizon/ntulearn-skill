# Usage and integration

This guide covers the implemented local interfaces. All example labels and search text
are synthetic. The commands do not configure live NTULearn access.

## Installation

The package declares Python 3.11 or newer; the current validation matrix covers
CPython 3.11 through 3.14. Install from a local checkout:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
ntulearn --help
```

For development:

```console
uv sync --frozen --extra dev
uv run --no-sync python -m pytest -q
```

To install a wheel built from this checkout, pass its path to pip:

```console
uv run --no-sync python -m build --no-isolation
python -m venv /tmp/ntulearn-wheel
/tmp/ntulearn-wheel/bin/python -m pip install dist/*.whl
(cd /tmp && /tmp/ntulearn-wheel/bin/ntulearn --help)
```

No package-index release currently exists. The commands above operate only on this
checkout and its locally built artifacts. See the executed
[packaging validation](development/packaging-validation.md) for the tested interpreter
matrix, artifact checks and clean-install evidence.

## Private runtime root

The CLI and `CoreService.from_runtime()` default to `~/.ntulearn-skill/`. The runtime
contains the SQLite database, content-addressed objects, browse views, indexes, caches,
logs and other private state. Directories are created with owner-only permissions where
the platform supports them.

Choose another private root either per command or through the environment:

```console
ntulearn --root /private/path/ntulearn-runtime courses
NTULEARN_DATA_DIR=/private/path/ntulearn-runtime ntulearn courses
```

Do not point the runtime at a tracked source directory. An explicit path below this
repository's ignored `.local/` directory is accepted for development. For an isolated
synthetic smoke test, create a temporary private root outside the checkout:

```console
synthetic_root="$(mktemp -d "${TMPDIR:-/tmp}/ntulearn-synthetic.XXXXXX")"
ntulearn --json --root "$synthetic_root" courses
```

A newly initialized store has no established source coverage, so `courses` returns
`UNKNOWN` completeness and exit `2`. Exit `3` is reserved for an empty result whose
relevant coverage is established as complete.

## CLI queries

Start by listing courses so later commands can use returned `local_key` values:

```console
ntulearn --json courses
ntulearn materials 1 --freshness cache-only
ntulearn search "synthetic interference" --course 1 --limit 20 --neighbors 1
ntulearn announcements 1
ntulearn assessments 1
ntulearn events --course 1 --show-conflicts
ntulearn events --next 2w
ntulearn upcoming --course 1 --days 14
ntulearn source 7 --kind source_locator --context-window 1
ntulearn resource 3 --version 5
```

The integer arguments are opaque keys in this private local database. Do not substitute
a course code, remote identifier or value from another runtime. A result might display
the invented code `PH0000`, but commands still use its returned integer `local_key`.

`resource --include-local-path` is the only CLI query that opts into returning a private
filesystem path. Avoid it in shared logs or agent output.

### Freshness

Query commands default to `--freshness cache-only`; this performs no source access.
Other accepted policies are `allow-stale`, `refresh-if-stale` and `require-current`.
Use `--max-age-seconds N` instead of `--freshness` when a bounded age is required.

A refresh-capable query permits one planned targeted refresh cycle through a configured
engine. That cycle may issue multiple allowlisted source read requests; the local query
is then retried once. Without an engine, freshness requirements that need a remote read
return a typed safe error or an unsatisfied result. The core does not silently create a
browser session.

### Result meaning and exit codes

`--json` emits schema version `1.0`. The envelope contains `operation`, `items`,
`completeness`, `as_of`, `freshness`, `coverage`, `conflicts`, `provenance`, `warnings`,
`errors`, `refresh_attempted` and `local_reads`, plus the CLI `command` field.

| Exit | Meaning |
| ---: | --- |
| `0` | Complete, non-empty successful result |
| `1` | Operational failure |
| `2` | Successful result with partial, stale or unknown completeness |
| `3` | Complete, conclusively empty result |
| `64` | Invalid command usage |

An empty exit-`2` result means only that nothing was found in the available local
coverage. It does not prove that the item does not exist remotely. Check `coverage`,
`freshness`, `warnings`, `errors` and `conflicts` before acting on a result.

Errors expose a stable category, code, safe message, operation, scope, retryability and
coverage impact. They intentionally omit supplied invalid values, raw exception text,
headers, payloads, signed URLs and private document excerpts.

### Local event decisions

Manual resolution changes only the private local event projection and retains its audit
record. Use local event, claim and source keys from query results:

```console
ntulearn manual-resolution field 4 title \
  --value-json '"Synthetic revised title"' \
  --reason "Synthetic local decision"
ntulearn manual-resolution identity 9 4 \
  --reason "Synthetic identity decision"
```

Field resolution accepts exactly one of `--claim-key` or `--value-json`. Local decisions
do not become source-backed evidence.

## Sync commands and their configuration boundary

The CLI grammar includes explicit read-only sync and fetch commands:

```console
ntulearn sync 1 --quick --no-fetch
ntulearn sync 1 --verify
ntulearn sync
ntulearn fetch 3 --verify
```

Without `--browser-capture`, the installed CLI creates a local `CoreService` without a
source engine. In that default
configuration these commands fail safely with exit `1` and the code
`sync_engine_unavailable`. Supply the host-prepared private bundle using
`--browser-capture PRIVATE_MANIFEST` to use the built-in browser capture provider and
existing engine. See [browser-assisted use](usage-browser.md). The package includes
no automatic SSO or browser credential extraction. The composition below is for a
custom or raw API integration, not a requirement for ordinary browser-assisted use.

A host application can compose the implemented core with its own authorized providers.
This example uses the actual constructor signatures while leaving authentication and
transport outside the repository:

```python
from pathlib import Path

from ntulearn_skill.client import SessionProvider, SourceProvider
from ntulearn_skill.core.api import CoreService
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync import SyncEngine


def configured_service(
    root: Path,
    sessions: SessionProvider,
    source: SourceProvider,
) -> CoreService:
    paths = RuntimePaths.discover(root).ensure()
    database = Database(paths.database)
    domain = DomainRepository(database)
    store = ResourceStore(paths, ResourceRepository(database))
    store.initialize()
    engine = SyncEngine(sessions, source, domain, store)
    return CoreService(database, runtime_paths=paths, sync_engine=engine)
```

The caller must implement `SessionProvider.acquire/status/invalidate` and the complete
typed `SourceProvider` protocol. An NTULearn-specific adapter and allowlisted
`ReadOnlyTransport` are available, but their executor, HTTPS source origin, authorized
session provider, redirect allowlist and any fresh resource-route factory must come from
a separately validated integration. Do not treat placeholder origins, cookies or tokens
as a working configuration.

## Core API

The main methods are:

```text
CoreService.from_runtime(root=None, *, sync_engine=None, browser_capture=None, initialize=True)
list_courses(filter=CourseFilter(), freshness=cache_only())
list_materials(course, filter=MaterialFilter(), freshness=cache_only())
search(query, freshness=cache_only())
search_course(course, query, freshness=cache_only())
get_announcements(course, filter=AnnouncementFilter(), freshness=cache_only())
get_assessments(course, filter=AssessmentFilter(), freshness=cache_only())
get_events(filter=EventFilter(), freshness=cache_only())
get_upcoming_events(window, course=None, freshness=cache_only())
get_resource(resource, version=None, include_local_path=False)
resolve_source(locator, context_window=1)
resolve_event_candidate(decision)
quick_sync(course, policy)
sync_course(course, policy)
sync_all(policy, selection=CourseSelectionPolicy())
fetch_resource(resource, verify=False)
```

Calls return `ResultEnvelope` rather than raising ordinary source or request failures to
the presentation layer. References accept positive local keys or the correct typed remote
identity; the CLI deliberately exposes only local keys.

## Codex dispatcher

`ntulearn_skill.integrations.codex.CodexToolDispatcher` validates a bounded mapping,
calls one allowlisted core method and returns the same schema-versioned, path-redacted
dictionary. It is useful when a host already knows how to register Python tools.

It is not an installed Codex plugin and does not provide tool registration, prompting,
an agent runtime or a ChatGPT adapter. Those integrations remain host responsibilities or
deferred work.
