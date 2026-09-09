# Authentication and source boundaries

## Boundary decision

Authentication mechanics and NTULearn-specific transport are replaceable adapters. Domain, storage,
parser, search, and AI layers receive no cookies, tokens, CSRF values, SSO URLs, or signed download
links.

The observed evidence establishes that normal reads use a browser-managed authenticated session.
It does not establish the minimum authentication dependency, expiry/renewal lifecycle, anonymous
download behavior, or signed-link lifetime. The architecture therefore defines contracts without
pretending those details are known.

## Interfaces

```text
SessionProvider
  acquire(purpose: ReadPurpose) -> AuthorizedReadSession
  status() -> SessionStatus
  invalidate(reason) -> None

ReadOnlyTransport
  send(request: SafeReadRequest) -> ReadResponse

SourceProvider
  capabilities() -> SourceCapabilities
  list_courses(session, page) -> Page[CourseRecord]
  list_content(session, course: CourseId, parent: ContentId?, page) -> Page[ContentRecord]
  get_announcements(session, course: CourseId, page) -> Page[AnnouncementRecord]
  get_assessments(session, course: CourseId, page) -> Page[AssessmentRecord]
  get_schedule_items(session, course: CourseId, window, page) -> Page[ScheduleRecord]
  get_due_items(session, course: CourseId, window, page) -> Page[DueRecord]
  get_resource_metadata(session, resource: AttachmentId) -> ResourceMetadataRecord
  open_resource_stream(session, resource: AttachmentId) -> EphemeralByteStream
```

The concrete `NtulearnSourceAdapter` owns endpoint paths, response-shape translation, pagination,
and ephemeral download routing. The core sees typed records and explicit coverage/capability data.
Separate schedule and due-item methods preserve the confirmed distinction between these sources.

`ContentId` and `AttachmentId` are not interchangeable even when a transport route happens to
contain both. Assessment, grading-column, announcement, and calendar-related identities also retain
their own types.

## Read-only safety

`ReadOnlyTransport` accepts only an allowlisted set of safe retrieval operations required by adapter
capabilities. The initial provider does not expose submit, start-attempt, post, message, grade,
profile, completion/progress, enrollment, or destructive methods. Redirects are revalidated against
download policy before credentials are forwarded.

Some platforms may record ordinary views as incidental access telemetry. The adapter documents this
possibility and avoids explicit state-changing controls; it does not claim that every remote side
effect is impossible.

## Session-provider variants

Potential implementations include:

- `BrowserSessionProvider`, which requests a valid browser-managed read session;
- `InteractiveLoginProvider`, if a supported and safe login flow is validated later;
- a test provider that supplies fully synthetic responses.

These names are extension points, not claims that renewal is solved. On expiry, the provider returns
`AuthenticationRequired` or `SessionExpired`; the sync engine records affected scopes as failed or
partial and preserves local data. Business logic never tries to repair cookies or replay SSO itself.

Secrets should use the OS credential store where possible. Any provider state under
`~/.ntulearn-skill/auth/` is owner-only and opaque. Credentials never enter the metadata database,
fixtures, normal logs, exports, crash reports, or public documentation.

## Signed URL policy

Signed and preview URLs are ephemeral transport details:

- they are acquired only when a resource stream is needed;
- they are not resource identity and never participate in deduplication;
- they are not stored in durable metadata, provenance, normal caches, or logs;
- query strings and authorization-bearing redirects are redacted before diagnostics;
- expiry is handled by discarding the route and requesting a new one through the adapter;
- a byte stream is accepted only after policy checks, format validation, and hashing.

If short-lived transport state must exist during a download, it remains in memory or an owner-only
temporary area and is removed at operation end.

## Capability negotiation for unknowns

`SourceCapabilities` initially treats the following as unsupported or unknown until private live
validation establishes otherwise:

- stable original-file validators and conditional requests;
- precise remote delta tokens;
- automatic session renewal;
- globally complete cross-course calendar reads;
- stable long-term identity across course copies/replacements;
- specific parser formats or content structures not encountered.

The sync planner branches only on declared capabilities. Missing optimizations fall back to explicit
pagination, metadata observations, verification intervals, and local hashes. An unsupported
capability produces a typed result rather than an improvised endpoint or guessed protocol.

## Provider response hygiene

The adapter maps only required fields into typed records. Durable sanitized metadata snapshots use
an allowlist; unknown fields are not copied wholesale. Raw authenticated responses may be retained
only for deliberate private debugging under ignored local storage, with short retention and
redaction. They are never needed for public tests, which use invented fixtures.
