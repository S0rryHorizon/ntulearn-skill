# Phase 5 hardening acceptance

Independent offline hardening: **PASS**. Public release: **NOT READY**.
The Phase 4 current-live transport gate remains **BLOCKED / NOT VALIDATED**.

## Executed acceptance

- CPython 3.11.15, 3.12.13, 3.13.14 and 3.14.6: 463 synthetic tests passed on
  each interpreter with sockets disabled. No browser, credentials or private source
  data were required. The supervisor independently ran the complete 3.12 suite.
- Ruff lint and formatting, strict mypy for 58 source files, compilation and
  `git diff --check` passed. Supported-version installation uses `uv.lock`.
- A fresh independent reviewer accepted the code, CI and public-interface documentation.
  Its 11 adversarial probes passed. A separate critical privacy reviewer passed
  27 adversarial probes and the public-file/dependency-source audit. All reported
  guard findings were repaired and independently retested.
- The locked dependency advisory query found no known vulnerabilities; the local
  editable project was explicitly excluded. Runtime and development license metadata
  were reviewed. See [packaging validation](packaging-validation.md) for exact scope.
- Wheel and sdist contents were inspected, including private paths, archive links,
  package metadata, license and source-file correspondence. A clean wheel install
  outside the checkout imported from `site-packages` and exposed the CLI.
- CLI examples, constructor signatures, relative documentation links, JSON errors
  and exit semantics were checked against the implemented interfaces and tests.

## Privacy and architecture

The independent baseline history audit at `25dba6f` covered all 12 reachable commits,
260 unique blobs and 137 historical paths. All historical blobs were UTF-8 text;
manual review identified only invented fixtures and redaction canaries. Final hardening
changes and distributions receive an additional audit, followed by a postcommit history
and clean-state check. Detailed local attestations retain final commit/artifact hashes
outside tracked content.

The final review preserves the accepted architecture: a generic content tree, typed
identities, immutable versions and provenance, complementary event sources, source-backed
Claims, retained conflicts, deterministic local FTS retrieval, scoped freshness,
read-only capability gates and thin AI integration. No source/storage contract or
accepted architecture decision was redesigned in Phase 5.

## Remaining gates

The authenticated UI was reachable, but the known read-only API route was blocked by
the browser client. No bypass or credential extraction was attempted. A usable authorized
read-only integration environment is still needed for current course/content,
announcement, assessment/due and resource retrieval validation. Offline tests and
historical replay do not satisfy that live gate.

GitHub-hosted CI has not run: no remote was added and nothing was pushed or published.
The workflow was reviewed and its local commands were exercised. No supported public
release, package-index publication or designated public security-report channel exists.
The maintainer's public-release decision remains separate and is not requested until
technical release gates can be evaluated with the missing live evidence.
