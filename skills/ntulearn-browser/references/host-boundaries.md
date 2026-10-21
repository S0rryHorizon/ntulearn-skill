# Host boundaries and failure diagnosis

The supported host is Codex with connected Chrome browser tools and normal NTULearn UI
access. Fresh collection requires supported UI/DOM observation and original-file download
capabilities. A standalone shell CLI cannot drive that host automatically; it can import
the host's validated private capture and perform all local retrieval independently.

Use only methods advertised by the current tools. Read-only DOM evaluation is not an
HTTP executor. Do not guess fetch/CDP/network APIs, inspect browser implementation code,
extract cookies, or switch channels around a known denial. Page assets and normal download
controls must be used according to their own documentation.

## macOS + Chrome downloads

The web page and Chrome's native Save dialog are separate observation surfaces. Inspect
the native window after a download click, even when the DOM is unchanged or a download
event has not arrived. This is normal UI handling within the same authorized download;
it is allowed only when the tools advertise native access and no explicit denial has
stopped that path. It does not require full CDP, a new transport or changed permissions.

1. Prepare the private capture directory with the existing helper. Record the selected
   resource and complete UI-visible filename, and check the intended destination for an
   existing file. Keep one download in flight so a later dialog can be attributed to it.
2. Click the visible original-file download control once. Use only documented download
   observers, with bounded waits; do not block native-window inspection on an event
   promise. On a CUA host, the native entry point is:

   ```javascript
   var chromeApp = await cua.getApp('com.google.Chrome');
   ```

   Follow the tool's initialization rules: that first call stands alone and returns
   native usage documentation and current UI state. On later calls, use
   `await chromeApp.getAXState()`; use its screenshot API when the current text leaves
   the dialog, filename or directory ambiguous. Do not invent native methods or search
   for the OS dialog with page DOM locators.
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
handling. For R
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

## Diagnose from evidence

| Observed signal | Conclusion and next action |
| --- | --- |
| DOM unchanged, event timeout, or no file yet | Cause **UNKNOWN**. Check native UI and current download/file state; do not label it a permission failure or immediately repeat the click. A dialog is only a hypothesis until seen. |
| Matching native Save sheet | **OBSERVED: awaiting save**. Recheck current filename/directory and complete the normal Save action above. This is not an access denial. |
| Explicit canceled/interrupted/failed download, or invalid/truncated bytes | **OBSERVED: cancellation, transfer failure or validation failure**, as applicable. Retain the exact stage/error and partial result. A retry needs an understood, resolved cause and a fresh UI check with no outstanding sheet/transfer; timeout alone does not justify one. |
| Explicit tool policy/permission refusal, or a visible access-denied page | **OBSERVED: denial by the named surface**. Stop that path. Do not try native input, another tab, shell HTTP, copied URLs or full CDP to accomplish the denied operation. Report the stated reason; distinguish a tool refusal from an observed site status. |
| `ERR_BLOCKED_BY_CLIENT` / `net::ERR_BLOCKED_BY_CLIENT` | **OBSERVED: client-blocked operation**. Stop that path and preserve this error separately from Save waiting. The code alone establishes neither an HTTP status nor which extension, administrator, tool or site caused it. Do not disable protections or switch transport. Unaffected authorized items may continue. |
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
