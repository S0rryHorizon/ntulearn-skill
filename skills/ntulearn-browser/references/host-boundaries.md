# Host boundaries and failure diagnosis

Bounded trials cover Codex with connected Chrome and the in-app browser, using normal
NTULearn UI access. Fresh collection requires supported UI/DOM observation and original-file download
capabilities. A standalone shell CLI cannot drive that host automatically; it can import
the host's validated private capture and perform all local retrieval independently.

Use only methods advertised by the current tools. Read-only DOM evaluation is not an
HTTP executor. Do not guess fetch/CDP/network APIs, inspect browser implementation code,
extract cookies, or switch channels around a known denial. Page assets and normal download
controls must be used according to their own documentation.

## In-app browser downloads

Prefer this host for quiet collection unless the user chooses another browser. Create a
dedicated tab with `cua.createBrowserTab("iab", NTULEARN_URL, { visible: false })`; check
login normally. Do not change the user's Chrome preferences or silently switch hosts.

1. Prepare the owner-only private bundle. Establish the browser's download directory:
   [official browser documentation](https://learn.chatgpt.com/docs/browser) identifies
   the system Downloads folder as the default, with a separate in-app setting for a custom
   location and Save prompts. Do not assume a default if a custom location is known.
   Record the UI-visible original filename and a baseline of only that filename and its
   browser-generated collision variants in the known directory before clicking.
2. Register the bounded download observer and click **Download original file** once.
   An observer path is useful when returned, but an event timeout does not mean the file
   was not saved. Inspect the known destination for a new matching file even after a
   timeout. Keep one download in flight and do not click again merely because the event
   is missing. A `_blank` window request without a new tab also does not prove failure.
3. Attribute the file using the baseline, filename, time and current attempt. Never use
   a pre-existing unchanged file as fresh evidence or select the newest unrelated file.
   If multiple candidates or concurrent same-name downloads make attribution ambiguous,
   retain UNKNOWN. Reject symlinks, incomplete transfers and format/integrity failures;
   use the shared validation requirements in the Chrome section below.
4. Copy the validated new bytes into the prepared bundle as an exclusively created
   owner-only file (`0600`), preserving the original filename in metadata. Verify the
   copy matches the source; leave the source download untouched. This makes the final
   capture location explicit without changing browser-wide preferences. Use the normal
   manifest/Core import when synchronizing; file acquisition alone is not library import.

**CONFIRMED (bounded trial, 2026-11-13):** two earlier original-menu downloads were found
in the default Downloads directory despite observer timeouts. A controlled follow-up
recorded the filename baseline, clicked once, found exactly one newly created collision
variant, validated its PDF structure/pages and copied it into a private bundle. All three
files had identical SHA-256 hashes. The follow-up download observer still timed out.
**OBSERVED (user report):** the second original-menu trial produced no Save dialog or
foreground interruption. Browser settings were not changed.

**UNKNOWN:** why the host does not report the completion event/path for this route;
event forwarding is a hypothesis, not an inspected implementation finding. The preview's
Download button also timed out, but its file result was not established. This trial
validates the original-menu acquisition and destination-copy path, not a full-course sync,
all file types, custom-directory settings, or a guarantee of focus behavior on every host.

## macOS + Chrome downloads

### Quiet downloads (default)

**OBSERVED (user report):** native Save dialogs bring Chrome to the foreground and
interrupt other work. The interactive procedure below handles a dialog after it opens;
it does not prevent focus stealing. Prefer a download route that does not open that
dialog. Do not bring Chrome forward, send global keys, open the downloaded file, or
silently switch to native Save to finish a background collection.

The required destination remains the prepared private capture bundle. Do not solve focus
stealing by changing the user's everyday Chrome download preferences or default directory.
Before the first download, check whether the supported host offers a dialog-free download
with a task-scoped destination, or a task-owned temporary file that can be validated and
copied into the bundle. Confirm the documented scope; a browser-wide setting is not a
task-scoped setting. If neither route is established, report the missing capability rather
than claiming quiet downloads are implemented. Do not trigger a known dialog-producing
download merely to test whether it steals focus. A denied operation remains stopped;
another surface or preference-file edit is not a workaround.

When dialog-free saving is ready, prepare the private bundle, register a bounded download
wait, then click the observed original-file control once. Use the documented download
result's local path and the file checks below. If the file lands outside the bundle,
copy only that verified new file into the bundle before import; preserve the original
and do not scan unrelated downloads. An event timeout alone does not authorize another
click or an interactive fallback. If a dialog appears unexpectedly, stop issuing further
downloads, report the pending dialog and ask whether to continue interactively; do not
assume that ending the tool call canceled it. Metadata/local-query work can continue with
explicit partial coverage, without claiming that the originals were downloaded.

**UNKNOWN:** a task-scoped, dialog-free original download has not yet been established on
this host. The advertised download event and its `path()` result observe a saved file;
they do not themselves specify a destination or suppress Save. Verify one authorized
original through any suitable supported route before a batch and report
file validation separately from observed or user-confirmed focus behavior. Do not claim
that a dedicated tab alone isolates native dialogs or guarantees background operation.

### Interactive Save (opt-in)

Use this procedure only when the user has accepted foreground Save interaction for the
current collection. File validation below applies to both quiet and interactive downloads.

The web page and Chrome's native Save dialog are separate observation surfaces. Inspect
the native window after a download click, even when the DOM is unchanged or a download
event has not arrived. This is normal UI handling within the same authorized download;
it is allowed only when the tools advertise native access and no explicit denial has
stopped that path. It does not require full CDP, a new transport or changed permissions.
An application restart is not a routine prerequisite either.

1. Prepare the private capture directory with the existing helper. Record the selected
   resource and complete UI-visible filename, and check the intended destination for an
   existing file. Keep one download in flight so a later dialog can be attributed to it.
   Prepare native observation before triggering the download. On a CUA host, the entry
   point is:

   ```javascript
   var chromeApp = await cua.getApp('com.google.Chrome');
   ```

   Follow the tool's initialization rules: if this initializes CUA, the call stands alone
   and returns native usage documentation and current UI state. On later calls, use
   `await chromeApp.getAXState()`; use its screenshot API when the current text leaves
   the dialog, filename or directory ambiguous. Do not invent native methods or search
   for the OS dialog with page DOM locators.
2. Use the filename's visible **More options → Download original file** menu when offered.
   Before clicking the download item once, register the documented
   `tab.playwright.waitForEvent('download', { timeoutMs: ... })`. The current API waits
   for download completion, which may depend on native Save. Do not await that promise
   (including through `Promise.all`) before inspecting and handling the native window.
   For example, with `tab` and `originalDownload` bound from the current tab/DOM and
   `chromeApp` from step 1:

   ```javascript
   var downloadWait = tab.playwright.waitForEvent('download', { timeoutMs: 5000 })
     .then(download => ({ download }), error => ({ error }));
   await originalDownload.click({ timeoutMs: 5000 });
   await chromeApp.getAXState();
   ```

   **UNKNOWN:** this specific scheduling example is API-checked, not live-validated;
   pending-promise/native-UI coordination still needs host validation. The 5-second caps
   are illustrative observer/click limits, not a download deadline. Keep each call within
   the current `cua_repl.js` budget (30 seconds by default), with room for native inspection.
   Attach the rejection handler immediately as above; inspect the error rather than
   treating it as success or retry authorization. Do not copy long event waits or increase
   the tool timeout to wait past a Save dialog. Inspect native UI even if the event is
   pending or has timed out; read the observer outcome after Save handling or the no-dialog
   checks in step 4, without starting another event wait.
3. Identify the Save sheet belonging to this download from the current window and
   filename. Check both the filename (including extension) and destination; use the
   visible dialog controls to select the prepared private bundle when needed. Refresh
   native state after edits and immediately before Save, deriving element indices from
   that state. Click the observed Save button once, then inspect the resulting state.
   Do not reuse old coordinates/indices, send a blind Return, or accept Replace for an
   existing file. Use a fresh destination for a confirmed task-owned dialog.
4. If no dialog is visible yet, inspect the current download status and intended local
   destination, then make a bounded re-observation using the tool's documented waiting
   behavior. Do not add fixed sleeps, poll an unchanged tree indefinitely, issue another
   download click, or queue the next resource while this attempt is unresolved. If neither
   completion nor a terminal failure is observed, retain **pending / UNKNOWN**, including
   the last observed stage; elapsed time alone proves neither failure nor denial.
   A tool-call timeout or REPL reset does not cancel Chrome's Save dialog. After a reset,
   initialize the supported handles again and check native UI, download status and the
   known target file before considering another click.
5. If the user changes tabs, window focus, filename or directory, discard stale targets
   and re-observe. Do not take over an unrelated or ambiguously owned dialog. If the user
   already saved, check the actual observed destination within the authorized scope; if
   they canceled, record cancellation rather than automatically restarting. Ask for the
   smallest clarification only when ownership, destination or resuming remains unclear.
6. Verify the actual file before claiming success. A closed sheet, completed click or
   download event alone is insufficient. The expected final path must contain a new,
   nonempty regular file, with no symlink or remaining partial-download file for this
   attempt (for example, `.crdownload`). Check completion/stable size and a suitable
   format/integrity validator; stable size or a familiar extension alone is insufficient.
   A pre-existing file is not evidence of this attempt. If Chrome or the user saved outside the
   bundle, use only the verified, authorized file and place a copy inside the private
   bundle before import; do not search unrelated folders or change the user's original.

For PDF/Office pre-import checks, reuse `ntulearn_skill.storage.detect_file_format` with
the installed capture-helper interpreter; it checks byte structure without trusting the
filename. Require the detected type to match the expected PDF/Office type; `unknown`
does not pass that check. Then use the normal Core import for SHA-256, storage and version
handling. Compare the new file's hash with an existing original when checking whether its
version changed; an extra manual hash comparison is not required for every save. For R
source and text/tabular data, inspect readable content and expected structure without
executing the downloaded code, and reject login/error HTML masquerading as a file. Do not
invent expected row counts, byte sizes or source hashes. An unsupported parser or `unknown`
format is separate from a successful save; report exactly what was checked and what
remains unverified. A local hash identifies bytes, but cannot prove they match a remote
original without an independent comparison.

Only attach a `file` entry after the local save and format checks above pass; then inspect
the normal import's resource receipt before claiming it is stored/verified in the library. Keep
the observed `original_filename` even if a safe local filename differs. For unresolved
or denied downloads, retain observed metadata without `file`, and retain partial/unknown
scope coverage. Do not infer remote unavailability from a local save problem or invent
capture fields for host diagnostics. Keep per-item diagnostics private and concise:
stage, observed error, save/file result, checks performed, and remaining uncertainty;
omit signed URLs and raw browser dumps.

**OBSERVED (bounded user-reported trial, not rerun for this documentation update):**
Registering the download wait before the original-file menu click and handling native Save
saved two PDFs whose SHA-256 matched existing local copies. Earlier automatic attempts
were client-blocked despite observed HTTP 200, `application/pdf` and the expected attachment
filename; manual use of the same menu succeeded. Event waiting timed out with Save open;
a longer wait also timed out the tool and reset its session while the dialog remained.
Response headers alone do not establish a successful local save.

**UNKNOWN:** waiting order has not been isolated as the sole cause. Session state, an
application restart and earlier Full CDP enablement may have influenced the outcome;
no specific extension or internal rule has been identified. This supports the workflow,
not an exception to the denial boundary below.

## Diagnose from evidence

| Observed signal | Conclusion and next action |
| --- | --- |
| DOM unchanged, event timeout, or no file yet | Cause **UNKNOWN**. Check native UI and current download/file state; do not label it a permission failure or immediately repeat the click. A dialog is only a hypothesis until seen. |
| Matching native Save sheet | **OBSERVED: awaiting save**. In quiet mode, stop further downloads and request interactive continuation. If already authorized, recheck current filename/directory and complete the normal Save action above. This is not an access denial. |
| Explicit canceled/interrupted/failed download, or invalid/truncated bytes | **OBSERVED: cancellation, transfer failure or validation failure**, as applicable. Retain the exact stage/error and partial result. A retry needs an understood, resolved cause and a fresh UI check with no outstanding sheet/transfer; timeout alone does not justify one. |
| Explicit tool policy/permission refusal, or a visible access-denied page | **OBSERVED: denial by the named surface**. Stop that path. Do not try native input, another tab, shell HTTP, copied URLs or full CDP to accomplish the denied operation. Report the stated reason; distinguish a tool refusal from an observed site status. |
| `ERR_BLOCKED_BY_CLIENT` / `net::ERR_BLOCKED_BY_CLIENT` | **OBSERVED: client-blocked operation**. Stop that path; do not automatically retry. Preserve this error separately from Save waiting. The code alone establishes neither an HTTP status nor which extension, administrator, tool or site caused it. Do not disable protections or switch transport. Later diagnostic success does not authorize bypassing a refusal. Unaffected authorized items may continue. |
| Explicit filesystem write/permission error | **OBSERVED: local save/storage failure**. Identify the failing local stage; it does not establish NTULearn access denial. Do not change system/browser permissions based on a timeout. |

Other host failures also need distinct evidence:

- A CLI configuration error means no source was configured, not a failed HTTP request.
- A tool-reported browser navigation error does not establish an HTTP status or identify
  an administrator policy. Record the operation, wrapper/source and observed response.
- A renderer crash may explain an accessibility/DOM timeout. Inspect the visible crash;
  one ordinary reload can test that specific hypothesis. Do not loop on an unchanged error.
- A login/MFA screen needs the user to complete that screen. Do not send credentials to
  another destination or ask the user to paste them into chat.
- An explicit permission or policy denial stops that path. Explain the exact supported
  permission/approval required; do not disable protections or improvise another transport.

If blocked, finish independent local work and save its state. Ask for only the smallest
currently necessary action, giving where, what, why and the visible completion signal.
Do not claim background continuation after ending the turn.

Observe only course content, related announcements and assessment descriptions/due labels.
Do not start attempts, submit, grade, post, message, change completion controls, visit
rosters or enumerate unrelated identities. Preserve unavailable items as unavailable;
normal UI metadata visibility does not authorize opening restricted bodies.
