---
name: ntulearn-browser
description: Synchronize a bounded NTULearn course through the connected Codex browser and retrieve its private local materials, sources, announcements, assessments and event evidence. Requires a supported browser host for fresh reads; local queries use the standalone ntulearn Core and CLI.
---

# NTULearn browser connection

Use this host workflow when the user requests NTULearn synchronization or retrieval.
For an ordinary question that should use only the existing private local library, use
the separate `ntulearn` skill first; this browser skill is the fresh-observation path.
Read [the browser guide](../../docs/usage-browser.md) and
[capture contract](../../docs/browser-capture-contract.md) for CLI installation and
accepted input. These links require the source checkout or source distribution; if
only this skill directory is installed, locate the user's installed project checkout
and read its corresponding docs. The host executes supported browser actions; the Python source adapter
validates and translates observations, and the Core owns storage, parsing, search,
reconciliation and freshness. Do not replace those operations with a temporary script.

## Local query first

Use the installed `ntulearn` CLI with the user's private runtime root. Ordinary queries
are cache-only. Show coverage, freshness, conflicts and exact source references with
answers. An empty partial result is not evidence that no course arrangement exists.
Use `source` and `resource` to resolve returned local keys; physical PDF page indices
are zero-based internally and should be presented as one-based page numbers to users.

## Fresh browser observation

Before creating a capture, run `ntulearn runtime-info --json` and confirm both
`browser_capture_helpers` are available. Obtain the matching interpreter with
`NTULEARN_PYTHON="$(ntulearn runtime-info --python)"` and execute all Python capture
helper calls with `"$NTULEARN_PYTHON"`. Keep this interpreter path as returned; do not
resolve its symlink to a base Python. An unknown diagnostic command or missing helper
means the CLI installation must be updated using the project installation guide before
collection. Do not substitute an arbitrary `python3` or another worktree's `.venv`.

1. Read the current connected browser tool's full usage rules and advertised capabilities.
   Use its supported Chrome host and a dedicated tab. Inspect current login state; any
   login or MFA is completed by the user in the normal interface. Never request cookies,
   tokens, browser-password exports or a hand-written connection object.
2. Choose only the authorized course/resource scope. Read normal course cards and content
   pages, preserving the observed course/content identifiers and source page path. Use
   `bundle_root = prepare_browser_capture_directory(PRIVATE_BUNDLE_ROOT)` from
   `ntulearn_skill.client` before saving anything, then use ordinary visible download
   controls to save original bytes into that new owner-only private capture bundle. Do
   not navigate directly to signed asset URLs. On macOS + Chrome, follow the
   [native Save workflow](references/host-boundaries.md#macos--chrome-downloads): click
   once, inspect Chrome's native windows, and verify the saved file. A missing DOM change
   or download-event timeout is not a permission denial; a delayed dialog is not a reason
   to click Download again. Recheck the current filename, directory and window before Save,
   especially when the user is also operating Chrome. An explicit denial stops that path.
3. Collect the original visible title/body/labels and explicitly observed identifiers in
   the source adapter's capture format. Preserve raw date wording and uncertainty; do not
   infer an unavailable identifier, timezone, due date, exhaustive coverage or event merge.
   Keep identity derivation and record translation in the source adapter. The agent, not
   the user, prepares this private bundle using the documented contract. Finish it with
   `write_browser_capture_manifest(bundle_root / "manifest.json", payload)` from
   `ntulearn_skill.client`. Do not create the manifest with `write_text` and tighten its
   mode afterward; the helper creates it as `0600` before any private JSON is written.
4. Invoke the normal CLI with `--browser-capture PRIVATE_MANIFEST` and the requested sync
   mode. Preserve the adapter's captured-time, stale and partial warnings. This is a
   browser-host-assisted observation import, not a direct HTTP connection or an assertion
   that replaying a capture performed a new network read.
5. Query the resulting private runtime through the CLI/Core. For verification, collect a
   new actual browser download; never relabel old bytes or an old capture as newly fetched.
   Cache-only queries must remain usable after the browser closes or the process restarts.

The full capture schema and current commands are described in the project guide; consult
[host boundaries](references/host-boundaries.md) when diagnosing a connection failure.

Real captures, manifests, downloads, labels, database state and validation reports belong
outside Git under `~/.ntulearn-skill/` (or the project's ignored `.local/`). Do not store
signed URLs, browser credentials, complete page dumps or unrelated personal information
in the bundle. Public fixtures must be invented independently of real course material.
Native Save handling depends on the current host's advertised tools. If native observation is
unavailable, report that specific gap without inferring a permission failure. Recheck the generated
bundle through the normal CLI import; report download, format validation and parsing separately.
