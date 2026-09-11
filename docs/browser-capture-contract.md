# Host browser capture contract

`ntulearn-browser-capture` is a snapshot adapter. A host opens NTULearn through the supported
browser UI, reads ordinary visible DOM fields, and saves normal browser downloads into one private
bundle. The adapter validates that bundle and sends typed records and byte streams through the
existing `SyncEngine`. It does not log in, call browser APIs, copy session material, or make network
requests.

The bundle is PRIVATE runtime data. Keep it outside the repository or under the ignored `.local/`
tree. The complete synthetic construction example is
[`tests/browser_capture_fixture.py`](../tests/browser_capture_fixture.py).

## Top-level object

The manifest is UTF-8 JSON, at most 2 MiB, with duplicate keys rejected.

The capture directory must be a real owner-only directory (mode `0700`) and the manifest must be a
regular owner-only file (mode `0600`). Symbolic links and group/other permission bits are rejected.
Host integrations use `prepare_browser_capture_directory` before saving capture content and
`write_browser_capture_manifest` for the final JSON. The writer opens a new file as `0600` before
writing; creating a default `write_text` file as `0644` and changing its mode afterward is not a
safe substitute. The importer opens the directory and manifest without following final symlinks,
then validates and reads the manifest through the same bounded file descriptor. Permission failures
produce a fixed remediation hint without echoing the capture path or manifest content.

```python
from ntulearn_skill.client import (
    prepare_browser_capture_directory,
    write_browser_capture_manifest,
)

bundle_root = prepare_browser_capture_directory(PRIVATE_BUNDLE_ROOT)
# Save ordinary browser downloads below bundle_root, then write the final bounded payload.
manifest_path = write_browser_capture_manifest(bundle_root / "manifest.json", payload)
```

The top-level object accepts exactly these keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer `1` | Contract version. |
| `source_kind` | string `host_browser_ui` | Declares the supported observation source. |
| `capture_id` | ASCII identifier | Unique batch ID, 1–128 characters: letters, digits, `.`, `_`, `-`. |
| `capture_start_at` | aware ISO 8601 timestamp | Start of this browser observation batch. |
| `captured_at` | aware ISO 8601 timestamp | End and authoritative observation time of the batch. |
| `expires_at` | aware ISO 8601 timestamp | Resource-stream deadline, after `captured_at` and no more than 24 hours later. |
| `authentication_status` | `READY`, `EXPIRED`, or `UNAVAILABLE` | Authentication state observed by the host during capture. |
| `course` | single-item scope | The observed course. |
| `content` | array scope | Bounded content-tree observations. |
| `announcements` | array scope | Bounded announcement observations. |
| `assessments` | array scope | Bounded assessment observations. |

All timestamps must contain a UTC offset. `capture_start_at <= captured_at < expires_at`.
`captured_at` may be at most five minutes ahead of the importing host clock.
An expired deadline does not change `captured_at`: metadata remains importable as old evidence,
while resource streaming fails with `session_expired`. An `authentication_status` of `EXPIRED` or
`UNAVAILABLE` fails source acquisition and preserves existing cache state.

## Scope objects

The single-item `course` scope has exactly:

```json
{
  "coverage": "PARTIAL",
  "source_page_path": "/ultra/courses/_synthetic_course_1/outline",
  "item": {}
}
```

The other scopes replace `item` with `items`, an array. `coverage` accepts only `PARTIAL` or
`UNKNOWN`. A bounded UI observation cannot claim a complete remote inventory. An omitted or unread
category uses `UNKNOWN` with an empty array. `source_page_path` must be an absolute path with no
scheme, query, or fragment. The capture ID and all four scope paths are retained in the private
`sync_run.requested_scope_json` receipt.

## Course item

Required keys are `remote_id`, `code`, `title`, and `availability`. Optional key `term` accepts a
string or null. `availability` uses the core `Availability` values. `remote_id` is the opaque course
identity visibly observed by the host. Every input identity accepts only ASCII letters, digits,
`.`, `_`, and `-` (at most 512 characters). URLs, paths, query strings, fragments, and percent
escapes are rejected rather than persisted as durable identity.

## Content item

Required keys are:

```text
remote_id, parent_remote_id, handler_kind, title, position,
availability, is_container, resources
```

`parent_remote_id` is null for a root node. Parents must exist in the same bundle, and cycles and
duplicate IDs are rejected. `position` is a non-negative integer. `resources` contains at most 100
items. Optional `sanitized_metadata` may contain only `content_type`, `display_style`, and
`module_label` with JSON scalar values.

## Resource item

Required keys are `display_title` and the complete UI-visible `original_filename`. Optional keys are
`declared_mime`, `candidate_modified_at`, and `file`. The manifest does not invent an attachment ID:
the adapter derives `ui-file:<content remote_id>` in its own provider namespace. Consequently, the
current schema supports at most one attachment per content node.

`file`, when present, has exactly:

```json
{
  "relative_path": "files/synthetic-course-overview.pdf",
  "downloaded_at": "2036-09-04T15:57:00+00:00",
  "fresh_for_capture": true
}
```

The relative path must remain inside the manifest directory. Absolute paths, `..`, backslashes,
symlinks, non-regular files, empty files, and files above 100 MiB are rejected. The total declared
resource size is limited to 256 MiB. `downloaded_at` must fall between `capture_start_at` and five
minutes after `captured_at`. The adapter opens every path component without following symlinks and
rechecks file identity and size before streaming. Storage then validates format, hashes the bytes,
creates or reuses `ResourceVersion`, and runs the normal parsing/indexing pipeline.

Replaying one capture uses its original `captured_at`; it cannot advance SyncState or the verified
time. `--verify` requires an unexpired file explicitly marked fresh for that capture. A new remote
verification therefore requires a new browser capture and normal download.

## Announcement item

Required keys are `remote_id`, `title`, `body`, and `availability`. Optional visible-time keys are
`created_at`, `modified_at`, `published_at`, `available_from`, and `available_until`.

## Assessment item

Required keys are `content_remote_id`, `title`, and `availability`. The adapter derives
`ui-assessment:<content_remote_id>` in its own namespace. The content ID must name an item in the
same bundle. Optional keys are `grading_column_remote_id`, `kind_text`, `instructions_text`, and the
visible-time keys `created_at`, `modified_at`, `available_from`, `available_until`, `open_at`,
`close_at`, `due_at`, `grading_due_at`, and `generic_due_at`. Missing instructions become an empty
observed string. `kind_text` maps recognized UI words to the core assessment subtype; an absent or
unrecognized label maps to `other`.

The provider uses the explicit `assessments` rows to identify assessment content. It projects a
matching visible content handler such as `Assignment` to `assessment:Assignment`, retaining the UI
label while giving the core a deterministic assessment category. The host does not infer this
membership.

## Visible time

A visible-time value keeps host-observed wording instead of requiring the host or an AI layer to
interpret it:

```json
{
  "text": "36/9/4 23:59 (UTC+8)",
  "source_timezone": "UTC+8"
}
```

`text` is required and `source_timezone` is optional. The adapter recognizes bounded
`YY/M/D HH:MM[:SS]` and `YYYY/M/D HH:MM[:SS]` forms, with optional `AM`/`PM`, when `UTC` or a valid
numeric UTC offset is supplied. A parenthesized timezone suffix must exactly match
`source_timezone`; unsupported trailing wording is not ignored. Invalid offsets, IANA wall times,
and unsupported suffixes remain `UNKNOWN` without an instant. A successful parse becomes
`EXACT_TIME`, while date-only wording becomes `DATE_ONLY`. The original text is retained in every
case.

## CLI

Use the private bundle through the ordinary CLI:

```text
ntulearn --root PRIVATE_ROOT --browser-capture PRIVATE_MANIFEST sync
ntulearn --root PRIVATE_ROOT --browser-capture PRIVATE_MANIFEST sync --verify
```

Local `cache-only` and `allow-stale` queries do not require or open a capture manifest. A query with
`refresh-if-stale`, `require-current`, or `--max-age-seconds` can use the same option to configure
the source-backed refresh path.
