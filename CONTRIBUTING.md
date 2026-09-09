# Contributing

Thank you for helping improve `ntulearn-skill`. The project is still pre-release,
and public release remains a deliberate human decision. Contributions must preserve
the local-first, read-only and privacy boundaries in `AGENTS.md` and the accepted
architecture.

## Development setup

The package declares Python 3.11 or newer, and the supported CI matrix currently
covers CPython 3.11 through 3.14. Install the repository and development dependencies
from the local checkout:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

For the locked development environment used by CI:

```console
uv sync --frozen --extra dev
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src
uv run --no-sync python -m pytest -q
uv run --no-sync python -m build --no-isolation
uv run --no-sync python scripts/check_public_artifacts.py --tracked --artifacts dist
```

The repository lockfile and CI workflow are the canonical dependency and supported
Python-version checks. See the executed [packaging validation](docs/development/packaging-validation.md)
for the current evidence and limitations.

## Before changing code

Read the [architecture overview](docs/architecture/overview.md),
[data boundaries](docs/privacy/data-boundaries.md),
[implementation plan](docs/architecture/testing-and-implementation.md) and
[capability ledger](docs/development/capability-ledger.md).

Keep the standalone core independent of Codex, ChatGPT and other LLM products. Put
product-specific behavior in a thin adapter over the stable core API. Do not promote
`UNKNOWN`, `HYPOTHESIS` or bounded `OBSERVED` source behavior to a supported contract.
Add a capability gate, safe fallback or explicit limitation until authorized evidence
validates the behavior.

## Test data and private state

Use invented data only. `PH0000 — Example Physics Course` is an acceptable synthetic
label. Never copy, lightly edit or anonymize a real course record into a fixture.

Do not commit any of the following:

- Credentials, cookies, tokens, session data, CSRF values or authentication headers.
- Real names, student identifiers, course identifiers, course metadata or materials.
- Authenticated URLs, request/response captures, private logs or error bodies.
- Runtime databases, indexes, caches, downloaded files or generated private exports.

Use a temporary directory outside the checkout for tests. If a development runtime
must sit beside the source, place it only below the ignored `.local/` directory and
pass that path explicitly. The narrow tracked-database exception under
`tests/fixtures/synthetic/` is for reviewed, demonstrably synthetic fixtures only.

## Change expectations

- Keep remote operations read-only and reject unknown request shapes before dispatch.
- Preserve typed source identities, provenance, explicit coverage and historical data.
- Do not infer deletion or complete absence from partial or failed traversal.
- Keep local paths out of machine output unless a caller explicitly requests them.
- Map failures to bounded, privacy-safe error categories; never echo source payloads,
  private query text, headers, URLs or exception details.
- Add a migration for persistent schema changes. Do not rewrite accepted migrations.
- Update public documentation when a stable interface, limitation or gate changes.

Tests should exercise meaningful boundaries and failure cases with no network access or
credentials. Run the commands above and any relevant focused tests. The complete
synthetic suite, lint, formatting, strict type checking, builds and the public-artifact
audit are the required baseline.

## Review checklist

Before handing a change to a maintainer:

- Inspect the full diff and every staged file.
- Confirm every identifier, URL, timestamp, filename and text sample is synthetic.
- Confirm tests made no network request and used no browser or account state.
- Verify new commands and Python examples against `--help` and actual signatures.
- Run the repository's release/privacy audit against tracked files and built artifacts.
- Record only checks that actually ran; keep blocked or unvalidated gates explicit.

Do not create a remote, push, publish a package or declare the project release-ready
without the maintainer's separate human approval.

By contributing, you agree that your contribution is licensed under the repository's
[MIT License](LICENSE).
