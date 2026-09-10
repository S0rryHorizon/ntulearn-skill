# Browser-assisted NTULearn use

The supported experimental path separates fresh collection from local retrieval.
Fresh collection is host-assisted through the installed browser Skill. Standalone
`ntulearn` CLI commands and the daily-question Skill read the private local library;
they do not log in to NTULearn or control a browser.

## Validated host

The bounded current trial used Codex on macOS with a connected Chrome browser. Log in
and complete MFA through Chrome's normal interface. The agent checks the current login
state before collection; a saved login from an earlier run is not evidence that the
current session works.

Install the Python project using the repository's
[installation instructions](../README.md). Install both `skills/ntulearn-browser` and
`skills/ntulearn` in the host's Codex skills location (normally
`~/.codex/skills/ntulearn-browser` and `~/.codex/skills/ntulearn` respectively),
then reload skills or start a
new task as required by the host. The source distribution includes those folders, but
the Python wheel alone does not install a Codex skill or a browser connection.

## Normal daily flow

1. Configure the private runtime root as described in the README.
2. In Chrome, open NTULearn and complete normal login and MFA.
3. Ask the `ntulearn-browser` Skill to refresh a small, named course scope. The host
   uses visible pages and ordinary download controls, then passes the bounded private
   capture into the existing Core synchronization path.
4. Ask the `ntulearn` Skill a daily question, or use the local CLI commands below.
5. For important dates, requirements, exam scope or venues, inspect the returned exact
   source. Treat event extraction as a preview and retain uncertainty or conflicts.

The normal Skill flow does not require the user to write a connection object, copy a
credential or prepare a capture manifest. If Chrome is unavailable or logged out, the
agent reports that host condition and requests normal interactive login without asking
for browser secrets.

After collection, representative local commands are:

```text
ntulearn courses
ntulearn library-status --course COURSE_KEY --freshness cache-only
ntulearn materials COURSE_KEY --freshness cache-only
ntulearn recent-materials COURSE_KEY --days 14 --freshness cache-only
ntulearn search "search words" --course COURSE_KEY --freshness cache-only
ntulearn announcements COURSE_KEY --freshness cache-only
ntulearn assessments COURSE_KEY --freshness cache-only
ntulearn events --course COURSE_KEY --show-conflicts --freshness cache-only
ntulearn source LOCATOR_KEY
ntulearn resource RESOURCE_KEY
```

`COURSE_KEY`, `RESOURCE_KEY` and `LOCATOR_KEY` are opaque local keys returned by
earlier commands. `source` and `resource` are inherently local and do not accept a
freshness option. Local queries work across process restarts without a connected
browser. If local processing exceeds the default queue bound, the browser-assisted
workflow can resume it with `sync --max-jobs`; inspect `library-status` rather than
assuming the first bounded run drained every job.

A partial or stale query uses exit code 2 and may still contain useful items. Failed
coverage can use exit code 1 while retaining items. Inspect the result's coverage,
freshness, conflicts and warnings instead of treating every nonzero code as an empty
answer.

## Evidence and coverage boundary

The current private trial stored 66 originals and parsed 65 supported PDF/DOCX files.
One PPTX remains stored but unsupported. Fifty-eight chunks were flagged for possible
visual review; two have bounded supplemental descriptions and 56 have not been
reviewed. Ordinary page bodies, folder descriptions, embedded attachments and external
interactive modules remain incomplete or unsupported. These figures describe one
bounded library state, not general NTULearn coverage.

Event extraction has two frozen evaluations. The original source-local set recovered
8/8 mentions, 26/27 scored fields and 27/27 provenance checks. The independently
selected set recovered 13/16 mentions with zero extra event mentions, but 0/16 mentions
were fully correct. Event type, time, venue and weak-identity errors remain. A retrieved
event must not be presented as a confirmed arrangement unless its source fields support
that claim. See [daily-library validation](development/daily-library-validation.md).

## Capture contract and freshness

The [typed capture contract](browser-capture-contract.md) is the private integration
boundary between host collection and the normal Core. It carries bounded observations,
source paths, capture times and downloaded files. It is mainly relevant to maintainers
and host integration code; everyday users should use the browser Skill.

A capture represents its original observation time. Replaying it does not prove a new
network read. Limited traversal remains PARTIAL, and omitted resources are not evidence
of deletion. Reuse based on unchanged metadata is only an unchanged assumption; hash
verification requires an actual download. Never rename an old capture or relabel old
bytes to manufacture fresh evidence.

If a capture expires, its earlier metadata may remain importable while resource
streaming fails explicitly. Cached material remains available through local queries.
If a visible page or tool denies a path, stop that path and report the gap; do not switch
to terminal requests, copy credentials or bypass browser controls.

The raw API adapter and automatic SSO renewal remain separate, unvalidated
capabilities. They are not required for this bounded host-assisted trial, and the trial
does not validate them. Read-only access excludes quiz attempts, assignment submission,
completion changes, rosters, messaging and all site edits. Captures, downloads and
validation evidence remain private under the configured runtime root.
