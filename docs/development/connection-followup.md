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

The installed wheel's normal CLI imported the newly collected private browser capture into a new empty
runtime: one course, four selected content nodes, one nine-page PDF, one announcement
and one assessment metadata record. Eight local processing jobs succeeded. Original
bytes matched the browser download and the stored SHA-256; a local search resolved an
answer to the original resource version and physical PDF page 5. Subsequent commands
ran in new processes against the persisted store. The final extractor produced six
source-local candidates and five canonical events in this bounded live sample.

Repeating the same capture created no additional resource version, parsed document or
event. Its resource receipt was `NOT_NEEDED` / `within_verification_interval`, with no
advance to the original verification timestamp. A cache-only invocation of the installed
CLI target, using the same valid capture configuration, produced results with zero
observed socket connection calls and no browser action. A separate negative configuration
check confirmed that cache-only queries do not open a nonexistent capture path either.
Forced verification of the old capture reports `capture_replay_assumed_not_reverified`;
it does not claim a second download or advance the verification timestamp.

A **simulated** expired capture returned `session_expired`; original bytes and the counts
of versions, parses and events were unchanged. This does not validate real SSO expiry.
Cached search remained usable afterward, with failed-coverage warnings retained.

After manual Mac unlock, the normal Chrome course UI loaded without a login prompt.
A second original PDF was downloaded through the content item's normal download menu
and native Save dialog into a new private bundle. The installed CLI's targeted
`fetch --verify` returned `REUSED_VERIFIED`, `binary_changed=false`: both actual
browser downloads and the stored original have the same SHA-256. Verification time
advanced to the new capture. The immutable version record was unchanged; counts of
parses, candidates, events, Claims and jobs were unchanged. The same four logical search hits and
physical page 5 source survived in new CLI processes. Replaying this second capture
returned `NOT_NEEDED` with the explicit not-reverified warning. A fresh cache-only
check with this same configuration recorded zero socket calls and no browser action.

Only the selected course/resource labels and file were re-observed; announcement and
assessment scopes in the second bundle are UNKNOWN. The existing parent-folder identity
was retained from the earlier mapping, not claimed as a newly read opaque ID. Targeted
fetch did not reimport the content tree. The resource fetch returned COMPLETE, but
cached search still reports FAILED coverage inherited from the earlier simulated
source failure and incomplete source scopes, while retaining its results. It must not
be described as a complete course refresh. The locked-screen download blocker is resolved.

During this continuation a browser-tab inspection returned `Debugger unattached`;
normal native Chrome UI inspection and download remained available. The supported
native interface was used; no alternate network executor was introduced.

Source, host-entry and
extraction and incremental index changes passed independent critical review. The installed
entry exposed a derived-index bug: refreshing a resource dropped its event/Claim search
rows while canonical data remained intact. The repair refreshes all projections for each
affected course. On the same private runtime, a normal sync restored all four original
logical search hits; repeat sync, old-capture verification and simulated expiration
preserved them, with unchanged original bytes, versions, parses, candidates, events,
Claims and jobs. Each command ran as a new installed-CLI process. These observations
are not aggregate live acceptance or evidence of complete course coverage.

## Real attachment extraction evaluation

The frozen private sample contains two previously authorized attachments and one
announcement. The original manual review covered all eleven rendered attachment pages
and the complete selected announcement, not only pages hit by the extractor. Later
evaluations reuse that frozen review and gold labels; they do not claim new visual review.

| Measure | Original extractor | Reviewed contextual extractor |
| --- | --- | --- |
| Source-local event mentions found | 0 / 8 | 5 / 8 |
| Missed mentions | 8 | 3, all image-only dates |
| False positives in this sample | 0 | 0 |
| Correct fields among matched mentions | Not applicable | 17 / 18 |
| Gold fields not reached | 27 | 9 / 27 |
| Correct page/observation and full component paths | Not applicable | 18 / 18 |
| Literal contiguous field quotations | Not applicable | 17 / 18, including all seven temporal fields |

The recovered selection deadline still has a generic heading instead of a sufficiently
specific title. Constraints such as permitted materials, weights and deliverable rules
remain retrievable source text and private evaluation context; the current event schema
does not model them as scored event fields. Unknown timezones are not inferred.

Embedded visual content now makes extraction explicitly PARTIAL even when native-text
parsing completed. Original files and native text remain unchanged. No OCR, external LLM,
external vision service or new data upload was introduced. Limited deterministic rules
and an empty result do not prove that course arrangements are absent. These sample metrics
do not establish recall or precision for all courses, document formats or writing styles.
