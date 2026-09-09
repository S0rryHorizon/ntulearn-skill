# Parsing and indexing

## Parser registry

Parsers implement a common versioned contract and are selected by detected physical format, never
by semantic material type:

```text
ParserRegistry
  PdfParser       # baseline, validated for text-bearing PDF samples
  DocxParser      # paragraphs and tables; no invented page numbers
  PptxParser      # future compatibility, synthetic tests first
  UnsupportedParser

parse(resource_version, options) -> ParsedDocument
```

Selection uses magic bytes and container inspection first, declared MIME type second, and filename
extension as a hint. A mismatch is retained as evidence and may quarantine the input instead of
silently choosing the remote extension.

Each parser publishes a name, implementation version, supported-format version range, and option
schema. `ParsedDocument` is uniquely keyed by resource-version hash, parser/version, and settings
hash. Repeating the same parse is idempotent; a parser upgrade creates a new derived result while
the previous result remains auditable until retention policy removes it.

## Uniform output

```text
ParsedDocument
  resource_version
  parser_name + parser_version + settings_hash
  status + coverage + warnings
  chunks[]
    ordinal
    kind: page | slide | section | paragraph_group | table | other
    native_text
    structured_elements[]
    locator
    representations[]
```

Locators are format-aware:

- PDF: physical page index is mandatory; printed/logical page label is optional and separate.
- PPTX: slide index is mandatory; title, shape, table, hyperlink, and speaker-note positions are
  optional capabilities, not current live guarantees.
- DOCX: section, paragraph, table, row, and cell positions are allowed. Viewer-dependent page
  numbers are not claimed unless produced and verified by a specific rendering pipeline.

Structured elements preserve reading order where the parser can justify it and mark uncertainty
where it cannot. Tables are never discarded merely because paragraph extraction succeeded.

## Non-destructive representations and visual fallback

Native extraction remains immutable evidence. OCR or vision output is an additional
`ChunkRepresentation`; it never overwrites native text.

### Stage A: native extraction

Run the format-native parser and collect text, structure, images, dimensions, and parser warnings.

### Stage B: page/slide diagnostics

Evaluate each chunk using explainable signals such as:

- unusually low text density relative to visible page area;
- a page dominated by images or vector drawings;
- font/text objects present but implausibly empty or scrambled;
- scan-like raster coverage;
- table/chart/diagram indicators with insufficient native text;
- extraction errors or suspicious reading order.

The thresholds are format-specific configuration with a recorded detector version. A low-density
title page alone should not trigger an expensive fallback without supporting signals.

### Stage C: selective fallback

Only flagged chunks are rendered. OCR may add text for scans; an optional vision provider may add
a bounded description for diagrams or spatial relationships. Each representation records method,
provider/model or tool version, settings hash, creation time, confidence, source page/slide, and
diagnostic reason. Cloud vision is opt-in and must follow the same privacy controls as semantic
providers.

Fallback output participates in search with a lower or method-aware confidence. Retrieval can show
native and derived evidence separately.

## Parse and derived-cache layout

Large derived artifacts live under private cache paths keyed by:

```text
resource SHA-256 / parser name / parser version / settings hash / chunk ordinal
```

The database stores the exact key, status, locator, searchable text, and integrity metadata. Cache
loss is recoverable from immutable originals; cache presence is never the sole record that a parse
succeeded.

## Material classification

Classification is a separate pipeline after basic metadata ingestion and, when needed, parsing.
Signals may include content-tree ancestry, title, filename, source handler, and extracted headings.
The result stores semantic type, confidence, rule/model version, evidence references, and whether it
was user-confirmed. A PDF can be `lecture_slides`; a PPTX can be the same semantic type. The
classifier may return `unknown` and can retain competing candidates rather than forcing a label.

## Deterministic full-text index

SQLite FTS5 is the default search layer. It indexes normalized views of:

- course code/title and content-tree titles;
- resource title, observed filename, and selected semantic type;
- document native text and clearly labeled derived representations;
- announcements and assessments;
- canonical events plus source-specific claims.

Relational filters for course, entity kind, semantic type, format, version, time range, lifecycle,
and confidence are applied alongside FTS rank. Search results point to `search_document`, which
points to a typed entity and exact `SourceLocator`. Neighbor expansion loads only selected local
chunks, such as a matching PDF page plus a small page window. This is local partial retrieval, not
a claim that remote range downloads are supported.

Index writes are transactional with their relational source updates. The index is disposable and
can be rebuilt; original text and evidence remain canonical.

## Optional semantic index

`SemanticIndexProvider` exposes build, remove, query, health, and manifest operations over
`DocumentChunk` identifiers. It must declare:

- local or remote execution;
- model and embedding dimensions/version;
- accepted data classes and privacy behavior;
- input-normalization version;
- incremental update and deletion support.

It is disabled by default. The core remains fully useful with FTS5 only. Provider output augments
candidate recall; final results still hydrate local evidence, enforce relational filters, and return
locators. A semantic score is never provenance or proof.

## Known compatibility limits

- PDF is the first parser priority because text-bearing PDF pages and page-level retrieval are
  confirmed in the bounded evidence.
- DOCX support must cover paragraphs and tables; complex tables and rendered pagination remain
  unvalidated.
- PPTX is a future compatibility capability. Synthetic tests can validate the contract, but must
  not be described as live NTULearn validation.
- Scanned PDFs, image-heavy slides, complex diagrams, legacy office formats, and spreadsheets are
  capability-gated. Unsupported input remains locally stored with parse state `UNSUPPORTED` or
  `PARTIAL`, not silently ignored.
