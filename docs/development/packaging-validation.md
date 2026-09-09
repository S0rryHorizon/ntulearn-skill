# Packaging and CI validation

Validation baseline: Phase 5 candidate based on commit `25dba6f`, 2026-09-10.

This record covers public packaging, supported Python runtimes, deterministic development setup,
distribution privacy, and dependency review. It does not validate live NTULearn transport. Phase 4
synthetic/runtime validation remains passed, live transport remains `BLOCKED_BY_CLIENT` and not
validated, and public release remains a separate human gate.

## Reproducible development environment

Python 3.11 or newer and `uv` 0.12.1 are required for the locked development workflow. The lock
contains SHA-256 hashes and public PyPI/Files.pythonhosted URLs only. The audit rejects alternate
indexes, credentials in URLs, Git/file/path dependency sources, malformed URLs, and unhashed
archives. The local editable source `.` is the only non-registry package source allowed.

```console
uv sync --frozen --extra dev
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src
uv run --no-sync python -m pytest -q
```

`pytest-socket` disables sockets for the public suite through the repository's pytest settings.
Dependency installation and the separate advisory query require public network access; core tests
do not.

## Build and artifact checks

```console
uv run --no-sync python -m build --no-isolation
uv run --no-sync python scripts/check_public_artifacts.py --tracked --artifacts dist
```

The artifact audit checks both wheel and source distribution paths and extracted bytes. It rejects
private/runtime/config paths and file types, likely high-confidence credential forms, archive links,
oversized members, binary content, unexpected wheel/sdist roots, non-public dependency sources,
missing hashes, a wrong `Requires-Python`, or a missing `ntulearn` console entry point.

The clean-install check uses a new virtual environment outside the checkout, installs only the
built wheel and its declared runtime dependencies, changes the working directory to `/tmp`, runs
`ntulearn --help`, and verifies that `ntulearn_skill` imports from `site-packages`:

```console
python -m venv /tmp/ntulearn-wheel
/tmp/ntulearn-wheel/bin/python -m pip install dist/*.whl
(cd /tmp && /tmp/ntulearn-wheel/bin/ntulearn --help)
```

No upload, repository remote creation, or publication is part of this validation.

## CI contract

`.github/workflows/ci.yml` has read-only repository permissions and does not define NTULearn
secrets. It runs the socket-disabled synthetic suite from the locked environment on CPython 3.11,
3.12, 3.13, and 3.14. A Python 3.12 job performs strict Ruff lint/format checks, strict mypy checks,
lock and privacy checks, wheel/sdist builds, distribution inspection, and the clean wheel install.
A separate Python 3.12 job performs the network-backed advisory query and prints license metadata.
External GitHub actions are pinned to full commit hashes verified against their official release
tags: `actions/checkout` 5.1.0 and `actions/setup-python` 6.2.0.

## Executed evidence

The following commands were run locally against the locked Phase 5 candidate:

| Check | Result |
| --- | --- |
| CPython 3.11.15 synthetic suite | PASS — 463 tests |
| CPython 3.12.13 synthetic suite | PASS — 463 tests |
| CPython 3.13.14 synthetic suite | PASS — 463 tests |
| CPython 3.14.6 synthetic suite | PASS — 463 tests |
| Ruff lint / format | PASS — 130 Python files formatted |
| Strict mypy | PASS — 58 source files |
| Bytecode compilation | PASS |
| Locked public-source audit | PASS |
| Wheel/sdist privacy and membership audit | PASS |
| Clean wheel import and `ntulearn --help` outside checkout | PASS |
| Independent privacy-guard adversarial probes | PASS — 27/27 |
| `pip-audit` 2.10.1, installed locked environment | No known vulnerabilities found |

The GitHub-hosted workflow has not run because this checkout has no configured remote. The table
records local execution of the same commands and version matrix; hosted CI remains an external gate
when the project is placed in a public repository.

The advisory result is a dated query of known public vulnerability records, not proof that the
dependencies or their transitive native libraries are vulnerability-free. `pip-audit` skipped the
editable local `ntulearn-skill` distribution and reported that skip explicitly. Its own security
model says that it audits dependency records rather than source code or malicious packages. The CI
query uses the [Python Packaging Advisory Database](https://github.com/pypa/advisory-database)
through the PyPI service documented by [PyPA pip-audit](https://github.com/pypa/pip-audit).

## Runtime dependency licenses

The locked runtime graph was reviewed from installed package metadata and current PyPI release
records:

| Package | Locked version | Declared license |
| --- | --- | --- |
| `pypdf` | 6.18.0 | BSD-3-Clause |
| `python-docx` | 1.2.0 | MIT |
| `lxml` | 6.1.3 | BSD-3-Clause |
| `typing-extensions` | 4.16.0 | PSF-2.0 |

These are permissive licenses compatible with this project's MIT distribution. The source records
are the [pypdf 6.18.0](https://pypi.org/project/pypdf/6.18.0/),
[python-docx 1.2.0](https://pypi.org/project/python-docx/1.2.0/),
[lxml 6.1.3](https://pypi.org/project/lxml/6.1.3/), and
[typing-extensions 4.16.0](https://pypi.org/project/typing-extensions/4.16.0/) PyPI pages. The full
development environment also reports permissive licenses or MPL-2.0 for its locked packages; it has
no GPL/AGPL dependency. License metadata is package-supplied and does not replace legal review.
