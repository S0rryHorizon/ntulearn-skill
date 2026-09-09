# Local storage

## Decision

The baseline store is SQLite with FTS5 plus a private filesystem. It is fully local, requires no
service process, supports transactions and migrations, and is sufficient for the expected
relational metadata and deterministic search workload. A separate vector database is not a base
dependency.

SQLite and the filesystem have distinct responsibilities:

| Store | Owns |
| --- | --- |
| SQLite | Typed relations, source observations, resource/version metadata, lifecycle state, timestamps, provenance locators, parsed text, structured event claims, sync/coverage state, migration history, and FTS5 indexes. |
| Filesystem | Original binary blobs, human-browseable material views, large rendered pages/slides, parser working caches, optional semantic index files, redacted logs, backups, and private exports. |
| Authentication provider | Credential and session material. It is neither an ordinary SQLite business table nor a debug artifact. Prefer an OS credential store when supported. |

Large original files, rendered images, raw document packages, credentials, and temporary download
URLs do not belong in SQLite. Small sanitized provider metadata snapshots may be stored in SQLite
when needed for update comparison; ephemeral URL fields and sensitive headers are stripped before
persistence.

## Private runtime layout

```text
~/.ntulearn-skill/
├── config/
│   └── settings.toml
├── auth/
│   └── provider-state/          # owner-only opaque state; secrets prefer OS keychain
├── db/
│   ├── metadata.sqlite3
│   └── backups/
├── objects/
│   └── sha256/
│       └── 12/ab/<full-hash>    # immutable canonical binary
├── courses/
│   └── PH0000--c_8f31ad2e/
│       └── materials/
│           └── lecture_slides/
│               └── week-3-slides--r_2c91d880/
│                   ├── current
│                   └── versions/
│                       ├── v000001--sha256-a1b2c3d4e5f6--week-3-slides.pdf
│                       └── v000002--sha256-12ab34cd56ef--week-3-slides.pdf
├── cache/
│   ├── parsed/
│   ├── rendered/
│   └── transport/
├── indexes/
│   └── semantic/                # absent unless explicitly enabled
├── exports/                     # private by default
├── logs/
│   └── operations.jsonl         # redacted and rotated
└── tmp/
```

`objects/` is the authoritative content-addressed binary store. `courses/` is a replaceable,
human-readable materialized view. On the same filesystem, browseable versions should be hard links
to blobs; an implementation may fall back to verified copies. `current` is a managed pointer to the
latest verified version, never the only copy.

## Stable naming rules

Filesystem names are presentation, not identity.

- A course directory is created once from a safe initial course-code slug plus a generated local
  short key. A later course-code change updates SQLite display metadata but does not silently move
  the directory. A dedicated maintenance operation may rebuild the view.
- A resource directory combines a readable title slug with a generated local resource short key.
  This prevents collisions without exposing a source identifier in the pathname.
- Each verified binary uses a monotonically increasing per-resource version number, a short hash,
  and a safe display filename. The full hash and all identity relations remain in SQLite.
- The original filename is retained verbatim as private metadata, while the disk name is normalized
  with Unicode NFKC, path separators and control characters replaced, reserved names rejected,
  trailing spaces/dots trimmed, and byte length capped. The detected format controls the trusted
  extension; a remote extension is only a hint.
- Temporary downloads use random names under `tmp/`. They are hashed and format-checked before an
  atomic move into `objects/` and a database transaction exposes the version.
- A semantic-classification change rebuilds only the course browse view. It never moves or mutates
  the canonical blob and never changes resource identity.

These rules handle duplicate filenames, renames, classification changes, and course-code changes
without using paths as keys.

## Binary integrity and history

SHA-256 is the baseline evidence for byte identity. The ingestion sequence is:

1. download to a private temporary file;
2. stream-compute hash and byte size;
3. detect file format from magic bytes/container structure, with MIME and filename as secondary
   evidence;
4. reject or quarantine an invalid/incomplete payload;
5. atomically place the blob at its content-addressed path;
6. insert or reuse `ResourceVersion` and record the `ResourceObservation` in one SQLite transaction;
7. update the browse view and `current` pointer only after commit.

Older versions are retained by default. Missing or unavailable remote state never garbage-collects
local versions. Future explicit retention policies must produce a preview and preserve database
audit records before removing unreferenced blobs.

## Export policy

No shareable export is produced implicitly. A future export command defaults to an allowlisted,
de-identified schema that excludes remote identifiers, personal metadata, local paths, credentials,
URLs, source text, and original attachments. Exporting private course content requires an explicit
private mode and writes only below the private runtime root unless the user selects another private
destination. Only fully synthetic artifacts may be written into this repository.

## SQLite responsibilities

Phase 3 should use the standard SQLite driver and explicit repositories before considering an ORM.
Recommended connection policy:

- enable foreign keys on every connection;
- use WAL mode for safe concurrent readers and one writer;
- set a bounded busy timeout;
- wrap each logical ingestion/reconciliation operation in a transaction;
- use database integrity checks in maintenance and backup workflows;
- keep the database and backups owner-readable only.

FTS5 is part of the same database but is a derived index, not canonical evidence. A relational
`search_document` table records entity kind, course, semantic type, title, filename, locator, and
canonical body reference. An external-content FTS5 table indexes selected text. Index updates and
their source rows commit together; the FTS index can be rebuilt from relational data.

## Schema migration

Use ordered, immutable SQL migrations such as `0001_initial.sql`, with a `schema_migration` table
containing version, checksum, and applied time. The migrator:

1. obtains an exclusive migration lock;
2. checks all applied checksums;
3. creates a timestamped backup before a non-trivial upgrade;
4. applies each pending migration transactionally where SQLite permits;
5. runs foreign-key and basic integrity checks;
6. restores no data automatically on failure, but leaves the backup path in a redacted error.

Migrations must be tested from every supported schema version using synthetic databases. An ORM or
service database may be reconsidered only if measured complexity justifies it.

## Optional semantic storage

Semantic search is a provider interface, not a database choice. A local provider may store vectors
and a manifest under `indexes/semantic/<provider>/<model>/`; SQLite stores only chunk mappings,
provider/model/version, input hash, and build state. A small future implementation may store vectors
as ordinary SQLite blobs if measured performance is adequate. No external vector service or cloud
embedding call is enabled by default.
