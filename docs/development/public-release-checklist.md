# Public release checklist

Current assessment after Phase 5 offline hardening (2026-09-10):
**NOT READY FOR PUBLIC RELEASE**.

## Gate snapshot

| Gate | Current evidence | State |
| --- | --- | --- |
| Phase 3 implementation | M1–M9 accepted under synthetic tests and independent reviews | **PASS** |
| Phase 4 offline validation | 445 synthetic tests, lint/format, strict type checking, compile and wheel/sdist builds; independent runtime-safety review passed | **PASS** |
| Phase 4 live validation | Authenticated UI reachable, but the known read-only API route was blocked by the browser client; no bypass or credential extraction attempted | **BLOCKED / NOT VALIDATED** |
| Phase 5 independent hardening | Locked dependencies; 463 tests on each supported Python version; independent code/privacy review; final wheel/sdist and documentation checks | **PASS (offline)** |
| Human public-release decision | No remote creation, push, package publication or release is authorized | **PENDING** |

Offline PASS does not promote the aggregate live-validation gate. Publication remains
blocked until the live gate has bounded evidence and a maintainer explicitly approves
release after reviewing all Phase 5 artifacts.

## Privacy and history

- [x] Audit every reachable Git revision and unique historical blob for prohibited
  credentials, authenticated data, real course identifiers/metadata, real materials,
  private logs and unexpected binary artifacts.
- [x] Review all tracked fixtures and examples manually; confirm they are invented and
  use only synthetic labels such as `PH0000`.
- [x] Audit the final staged diff and verify ignore rules for `.local/`, the default
  private runtime, credentials, captures, databases, indexes, caches, logs and builds.
- [x] Scan the wheel and sdist contents independently from the working tree.
- [x] Confirm generated metadata, error text and documentation contain no private paths,
  usernames, host-specific secrets, signed URLs or source payload excerpts.

## Packaging and compatibility

- [x] Create clean environments from the locked dependency set for every declared
  Python version. Limit installation traffic to the configured public package registry
  and record the resolved lock state.
- [x] Run strict lint, format and type checks using the repository's canonical commands.
- [x] Run the complete synthetic suite with sockets disabled and no credentials,
  browser access or private source state.
- [x] Run the dependency advisory audit using only its public vulnerability database;
  do not contact NTULearn or any private source.
- [x] Build both wheel and sdist; inspect their file lists and metadata.
- [x] Install the wheel into a clean environment and verify the `ntulearn` console entry
  point, `ntulearn --help`, local empty-store behavior and stable exit codes.
- [x] Install from a local checkout and validate documented development commands.
- [x] Confirm the MIT license is present in source and distributions and matches package
  metadata. The current `LICENSE` is the standard MIT text for 2026 contributors.

Record exact commands, interpreter versions, artifact contents and results in
[packaging validation](packaging-validation.md). A prior milestone build does not
satisfy this final audit.

## Public interface and documentation

- [x] Compare every documented CLI command and option with actual `ntulearn --help` and
  subcommand help from the clean wheel install.
- [x] Execute representative JSON commands against a temporary synthetic private root;
  verify schema version `1.0` and exit codes `0`, `1`, `2`, `3` and `64`.
- [x] Verify human and JSON output make freshness, scoped coverage, conflicts, provenance
  and inconclusive absence visible.
- [x] Verify safe errors do not reflect invalid arguments, exception text, credentials,
  source payloads, authenticated URLs or private paths.
- [x] Compare the Python API examples with actual type/constructor signatures.
- [x] Confirm the source configuration guide requires injected authorized session and
  transport providers and makes the absence of automatic SSO/browser credential
  extraction explicit.
- [x] Confirm the Codex dispatcher is described only as a thin Python surface, not an
  installed plugin, agent or ChatGPT adapter.
- [x] Check all relative Markdown links and packaged README content.

## Live validation gate

- [ ] Obtain a usable, explicitly authorized, read-only session/transport environment
  without extracting browser credentials or bypassing client/security controls.
- [ ] Run bounded current-live checks for course discovery, content traversal,
  announcements, assessment/due data and resource retrieval.
- [ ] Record detailed evidence only in the private runtime; publish only reviewed,
  de-identified capability outcomes using `CONFIRMED`, `OBSERVED`, `HYPOTHESIS` and
  `UNKNOWN` accurately.
- [ ] Update the capability ledger without claiming global completeness from a bounded
  sample.
- [ ] Obtain an independent review of the live conclusions and failure handling.

If the environment remains blocked, retain **BLOCKED / NOT VALIDATED**. Do not convert
the synthetic/runtime-safety PASS into a live PASS.

## Final human gate

- [ ] Before publication, re-run history, staged-file and distribution privacy audits
  after any remaining live-validation changes.
- [x] Resolve or explicitly defer every independent hardening finding.
- [ ] Before publication, recheck that the worktree is clean and the intended release
  commit is the reviewed commit. The hardening postcommit check is recorded separately.
- [ ] Have the maintainer review the final evidence, remaining limitations, security
  reporting channel, package metadata and intended public destination.
- [ ] Receive explicit human authorization before creating a public remote, pushing,
  publishing a package, tagging a release or announcing availability.

Until every required gate above is satisfied or consciously waived by the maintainer,
the release decision remains **NOT READY FOR PUBLIC RELEASE**.
