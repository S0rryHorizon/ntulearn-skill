# Data boundaries

Privacy is an architectural constraint, not a cleanup step. When classification is uncertain, treat the data as private and do not commit it.

## PUBLIC

Content suitable for this repository includes:

- Source code and public documentation.
- Schemas and configuration templates without secrets.
- Fully synthetic tests and examples using invented courses such as `PH0000 — Example Physics Course` and `CS0000 — Example Computing Course`.
- Reviewed, generalized, and de-identified technical conclusions.

Public content must not permit reconstruction of a user's identity, enrollment, authentication state, or course materials.

## PRIVATE

Private runtime or development data includes:

- Authorized course metadata, resources, events, and local mirrors.
- Databases, search indexes, caches, downloaded files, and logs.
- Reconnaissance notes containing real URLs, identifiers, payload shapes, or samples.
- Authentication configuration and any personal settings.

Store private data preferably under:

```text
~/.ntulearn-skill/
├── config/
├── auth/
├── db/
├── objects/
├── courses/
├── indexes/
├── cache/
├── logs/
├── exports/
└── tmp/
```

During development, private artifacts may instead use the ignored `.local/` tree:

```text
.local/
├── recon/
├── downloads/
├── indexes/
├── cache/
├── logs/
└── credentials/
```

An in-checkout runtime requires an explicit path below `.local/`, effective Git
ignore protection and no tracked runtime content. Validation checks Git's actual
rules and index; the directory name alone grants no exception. Missing Git,
unverifiable repository state or ineffective ignore protection causes a bounded,
privacy-safe error rather than allowing runtime initialization. The application
does not create ignore rules or move existing data to repair a rejected boundary.
For a direct database path, its containing directory must also be protected so that
SQLite sidecar files are not exposed by a rule that ignores only the main database.
Ignoring the whole `.local/` tree or just the selected `.local/runtime/` directory
is supported. For a directory that does not yet exist, complex wildcard-only rules
may be rejected when whole-directory protection cannot be established. Ignoring
selected file extensions alone is insufficient for a runtime directory.

This is a check of the current filesystem and Git state, not a permanent lock on
ignore rules or the index. Later rule changes or forced staging can still expose
data. Keep the staged-file privacy review, and prefer a runtime outside Git.

## NEVER COMMIT

- Passwords, credentials, cookies, sessions, tokens, CSRF values, or authentication headers.
- Personal information, student identifiers, private course databases, or real course indexes.
- Real course attachments, lecturer slides, lecture notes, assignment briefs, tutorials, or lab manuals.
- Raw authenticated network captures, request dumps, or logs containing sensitive headers.
- Download caches or any other artifact derived from private NTULearn access unless it has been deliberately transformed into a demonstrably synthetic fixture.
- Signed/preview download URLs, including their query parameters, even when they appear short-lived.

The `.gitignore` provides a baseline safety net, but it cannot recognize every sensitive filename. Contributors and agents must inspect staged content before every commit.

## Synthetic fixture exception

Database files are ignored globally. A narrow exception exists for database files under `tests/fixtures/synthetic/`; anything placed there must be generated from invented data and manually reviewed before commit.
