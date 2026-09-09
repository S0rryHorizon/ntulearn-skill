# Connection follow-up

Baseline: `3c31f76`. The M1–M9 synthetic acceptance history is preserved. This follow-up
examines executable source code and current host behavior rather than interpreting a
status label as proof of live compatibility.

## Baseline implementation diagnosis

| Category | Finding | Code/evidence |
| --- | --- | --- |
| A: implemented connection unavailable | Does not explain the default CLI failure: no request was dispatched by that CLI | `cli/_main.py:run`, `core/api.py:_engine` |
| B: missing concrete connection | The API translator exists, but concrete authorization-session acquisition, wire execution and fresh resource routing were absent | `client/contracts.py:SessionProvider`, `client/transport.py:ReadOnlyTransport.__init__`, `client/ntulearn.py:NtulearnSourceAdapter.__init__` |
| C: missing normal entry wiring | The CLI constructed `CoreService.from_runtime(root)` without an engine; the Codex dispatcher required a caller-supplied CoreService | `cli/_main.py:run`, `integrations/codex/adapter.py:CodexToolDispatcher` |
| D: contract/context defects | Session-marker stability was implicit; a restarted API adapter lacked the resource authorization context required for standalone fetch | `client/ntulearn.py:_activate_session`, `_require_active_session`, `open_resource_stream`; `sync/resources.py:ResourceFetchService` |
| E: insufficient live evidence | Current raw API response translation, pagination and signed resource routing remain unvalidated | Synthetic tests and old capture replay cannot establish these facts |

The concrete API adapter contains endpoint request construction and response translation;
it is not merely a mock. Its missing executable connection components must nevertheless
be reported separately from environmental access failures.

## Error origin and host comparison

The historical error was returned by the CUA browser tool during direct tab navigation to
a known membership API path. The wrapper said the browser reported
`net::ERR_BLOCKED_BY_CLIENT`. It was not a terminal network error or a project exception.
No HTTP response/status was recorded, and that string alone does not identify a site,
administrator or extension policy. That denied path was not retried through another
channel during this follow-up.

Phase 1 successful observations used normal browser UI navigation, original-file download
controls and UI-exported observation records. They did not prove that direct API tab
navigation, a Python transport, or old session/download links remained usable.

The currently connected Chrome host advertised normal UI navigation, read-only DOM
inspection, download controls and page assets, but no general authenticated HTTP executor.
An initial accessibility/DOM inspection timeout was traced to a visible renderer crash
(code 11) in the new diagnostic tab. One ordinary Reload restored the page. The normal
course UI, a representative course tree, a related announcement and assessment metadata
were then readable; a fresh nine-page original PDF was saved through the normal Download
and Save dialog into a newly created private directory.

No credential extraction, hidden tool, alternate HTTP channel, security-setting change,
quiz attempt, submission, roster access or account modification was used. No login or
permission prompt required user action during these observations. The precise historical
client-blocking component remains unknown and is not needed to use the supported UI path.

## Product connection boundary

The supported fresh-read path is browser-host-assisted: normal UI observations and
original downloads enter a typed private capture, which the source adapter validates and
the existing Core processes. The host does not implement domain reconciliation, parsing
or persistence. Source translation and identity validation belong in Python; the host
passes raw visible labels and exact observed identities.

A capture is an observation at its original timestamp. Replaying it is not a new network
read. Partial UI scope does not prove completeness, remote deletion or access to a hidden
assessment body. Independent shell operation supports local retrieval and capture
processing; browser collection requires the documented Codex host.

Detailed live scope, raw observations, file hashes, extraction labels and acceptance
outputs are private. Only reviewed, de-identified outcomes may be added to this record.

## Current validation observations

The normal CLI imported a newly collected private browser capture into a new empty
runtime: one course, four selected content nodes, one nine-page PDF, one announcement
and one assessment metadata record. Eight local processing jobs succeeded. Original
bytes matched the browser download and the stored SHA-256; a local search resolved an
answer to the original resource version and physical PDF page 5. Subsequent commands
ran in new processes against the persisted store.

Repeating the same capture created no additional resource version, parsed document or
event. Its resource receipt was `NOT_NEEDED` / `within_verification_interval`, with no
advance to the original verification timestamp. A cache-only invocation of the installed
CLI target produced results with zero observed socket connection calls, no browser action
and no attempt to open a deliberately nonexistent capture path.

A **simulated** expired capture returned `session_expired`; original bytes and the counts
of versions, parses and events were unchanged. This does not validate real SSO expiry.
Cached search remained usable afterward, with failed-coverage warnings retained.

The current native browser tool reports that the Mac is locked and requires manual
unlock. Therefore a second fresh browser download and cross-capture hash verification
remain pending. No alternate download channel was attempted. Implementation review and
final integrated acceptance are still in progress; these observations are not an aggregate
PASS or evidence of complete course coverage.
