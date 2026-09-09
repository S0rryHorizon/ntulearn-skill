# Browser-assisted NTULearn use

Fresh NTULearn reads currently require a connected Codex browser host. The standalone
Python CLI handles private storage, parsing, search and provenance; it does not log
in to NTULearn or open an authenticated browser by itself. The raw API adapter remains
an integration interface whose live protocol has not been validated end to end.

## Supported host and installation

Use Codex with the supported Chrome browser connection enabled and visible in its
browser tools. Complete NTULearn login and MFA in Chrome's normal interface. The
agent checks the current tool documentation and login state before collecting data.
A saved login from an earlier run is not evidence that the current session works.

Install the Python project using the repository's [installation instructions](../README.md).
Install the repository's `skills/ntulearn-browser` folder into your Codex skills
location (normally `~/.codex/skills/ntulearn-browser`), then reload skills or start a
new task as required by the host. The skill is supplied in the source distribution;
the Python wheel alone does not install a Codex skill or a browser connection.

Ask the agent to synchronize a small, named course scope and then search it. The
agent uses normal visible course pages and download controls, prepares the private
observation bundle, and calls the existing CLI. You do not need to write a connection
object, copy a credential, or prepare a JSON file. If Chrome is not connected, the
agent identifies the missing host connection rather than asking for browser secrets.

## Product path

The browser observes authorized pages and saves original attachments. The
[typed capture contract](browser-capture-contract.md) carries only the selected raw
observations, source paths, capture times and downloaded files. The source provider
validates that input and supplies the normal Core synchronization engine. The Core
creates immutable versions, parses documents, indexes native text and reconciles
source-backed event candidates.

These commands show the normal entry points. `PRIVATE_MANIFEST` is prepared by the
host agent, and `COURSE_KEY`, `RESOURCE_KEY` and `LOCATOR_KEY` are local keys returned
by previous commands, not remote NTULearn identifiers.

```text
ntulearn --root PRIVATE_ROOT --browser-capture PRIVATE_MANIFEST sync
ntulearn --root PRIVATE_ROOT courses
ntulearn --root PRIVATE_ROOT materials COURSE_KEY
ntulearn --root PRIVATE_ROOT search "search words" --course COURSE_KEY --freshness cache-only
ntulearn --root PRIVATE_ROOT source LOCATOR_KEY
ntulearn --root PRIVATE_ROOT resource RESOURCE_KEY --include-local-path
ntulearn --root PRIVATE_ROOT announcements COURSE_KEY
ntulearn --root PRIVATE_ROOT assessments COURSE_KEY
ntulearn --root PRIVATE_ROOT events --course COURSE_KEY --show-conflicts
```

Local queries work across process restarts and do not require a browser capture.
A partial/stale result uses exit code 2 and still contains useful results. Failed
coverage can also make a cached query return exit code 1 while preserving its items;
inspect coverage and warnings instead of treating every nonzero code as an empty result.

## Freshness and failures

A capture represents its original observation time. Replaying it does not prove a
new network read. A limited course view remains PARTIAL, and omitted resources are
not evidence of deletion. Reuse of existing bytes based on unchanged metadata is
assumed unchanged; hash verification requires a new actual download and a new capture.
Never rename an old capture or relabel old bytes to manufacture fresh evidence.

An expired capture deadline still permits import of its old metadata, but resource
streaming fails explicitly. An observed authentication status of `EXPIRED` or
`UNAVAILABLE` prevents source acquisition. Cached materials remain available through
cache-only queries. If the normal browser UI
requires login, the only needed user action is to complete that login in the supported
browser. If a tool or site explicitly denies a path, stop that path and identify the
required formal permission; do not switch to a terminal request or copy credentials.

See [connection diagnosis](development/connection-followup.md) for the distinction
between the historical browser navigation error and the original CLI wiring gap.
Read-only access excludes quiz attempts, assignment submission, completion changes,
rosters, messaging and all site edits. Downloads, manifests and validation evidence
remain private under `~/.ntulearn-skill/`.
