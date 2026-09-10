# Selective visual evidence

The visual workflow is a private, local supplement for PDF pages that the native parser explicitly
flags. It does not scan every page and does not replace native text or the immutable original.

## Prepare flagged pages

Parse the verified resource through the normal sync pipeline. Obtain its current local parse key
through the normal resource command; no database access is needed:

```console
ntulearn --root /private/runtime resource 17 --json
```

The returned resource item includes `parse_key` when a reusable parse exists. Pass that key to
visual preparation:

```console
ntulearn --root /private/runtime visual prepare 42 --dpi 150 --include-local-path --json
```

The command renders only flagged PDF pages to the private cache. Its output includes each exact
source hash, zero-based page index, one-based page number, rendered-image hash, diagnostic reasons,
and local PNG path. It also creates a private bundle template under
`exports/visual-evidence/`. `NEEDS_REVIEW` means that rendering succeeded but no visual conclusion
has been accepted.

Fresh rendering requires the local Poppler `pdftoppm` executable on `PATH`. Cache-only resource,
search, event, and source queries do not require Poppler.

The host should open each returned PNG with its native image-view tool. It should transcribe only
event wording that is visible on that page, preserve `Week N` as week precision, and state every
unclear glyph, row association, date, clock, or timezone in `uncertainty`. It must not infer an exact
date from a week number or assume midnight or Singapore time.

## Import the host result

Keep the template's top-level source identity and `inspection_items`. Add one result per inspected
page to `results`:

```json
{
  "rendered_representation_key": 73,
  "text": "Invented report submission deadline: 15 April 2032.",
  "engine_version": "host-model-version",
  "settings": {"instruction_version": "event-transcription-1"},
  "confidence": 0.55,
  "review_status": "NEEDS_REVIEW",
  "uncertainty": ["The room label is partly obscured"]
}
```

Use `PARTIAL` when the page was inspected but the result knowingly covers only part of the relevant
visual content. Otherwise use `NEEDS_REVIEW`; imported host evidence never becomes complete or
confirmed through this command.

A later import for the same source page and inspection method becomes the current supplemental
description used by search and event extraction. Its `text` therefore needs to restate every
still-relevant visible region from earlier inspections. A region-only correction does not merge
with older descriptions; mark it `PARTIAL` when it knowingly omits other relevant regions. Older
representations remain in the immutable audit history.

```console
ntulearn --root /private/runtime visual import /private/runtime/exports/visual-evidence/parse-42-abc123-def456.json --json
```

Import verifies that the bundle, original source hash, page locator, rendered representation, and
rendered-image hash still agree. It stores a new `vision_description` representation with method,
engine version, settings hash, confidence, review status, uncertainty, source page, and hashes. A
repeated identical import reuses the same representation. Changed text or settings produce a new
auditable representation.

After import, visual text participates in deterministic search and event candidate extraction through the same
source locator as its native page. Candidate confidence is capped by the imported confidence, and
extraction remains `PARTIAL` with `visual_evidence_needs_review` or `visual_evidence_partial`.
Normal reconciliation can create source-linked claims and unresolved event projections; review
status remains available in visual-evidence metadata. If derived projection cannot finish, import
returns `visual_projection_pending`; rerunning the same import safely retries the projection.

Resolve a search result's `source_locator` through the ordinary source command to inspect the exact
native chunk and its current visual supplement together:

```console
ntulearn --root /private/runtime source 91 --kind source_locator --context-window 0 --json
```

The native parser result stays in `chunks`; `visual_evidence` separately reports the current
supplemental text, representation and chunk keys, method and provider, engine and method versions,
settings and settings hash, confidence, review status, uncertainty, source version and page,
original and rendered hashes, physical source locator, and `is_current`. When the cached render is
available, it also reports the renderer representation, method, engine and method versions,
settings, and settings hash. A source result containing
visual evidence remains `PARTIAL` and carries its review warning, so host transcription is not
presented as verified native text. The lookup reads the cache only: it does not render pages, open
the original, or make a network request, and it does not reveal the cached image path.

Add `--include-visual-history` to the same command for an explicit immutable audit view. Superseded
rows have `is_current: false`; without that flag, the command returns only the latest description
for each source page and inspection method.

Default search indexes native and derived chunks from only the latest usable (`COMPLETE` or
`PARTIAL`) parse of each immutable resource version. Older parse rows and locators remain available
for direct source audit, and a newer failed or unsupported parse does not hide the last usable one.

Current limits are PDF-only page rendering, at most 64 results per bundle, 32 KiB of text per
result, a 512 KiB bundle, 72–300 DPI, and a 32 MiB rendered PNG. A parse with more than 64 flagged
pages is rejected before rendering and needs a future split-bundle workflow. DOCX visual fallback, automatic
image understanding inside the core, external OCR, and external vision services are unsupported.
