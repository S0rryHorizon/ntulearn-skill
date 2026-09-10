# Deferred capability validation

Synthetic acceptance is not evidence of live NTULearn compatibility. The following
Phase 2 unknowns remain gated until a bounded, authorized private check establishes
otherwise. Private evidence belongs only in `.local/` or the private runtime root.

| Capability | Evidence | Safe fallback | Validation trigger | Gate impact |
| --- | --- | --- | --- | --- |
| Long-term remote identity | UNKNOWN | Preserve typed observed identities; do not infer replacements | Naturally repeated observations | Optimization deferred |
| Large-list pagination | UNKNOWN | Follow supplied pages with explicit bounded coverage | Authorized larger collection | Cannot claim globally complete coverage |
| Original-file validators / revisions | UNKNOWN | Metadata signals plus periodic SHA-256 verification | Naturally revised original | Conditional downloads disabled |
| Authentication renewal | UNKNOWN | Typed expiry; preserve local data; request interactive login | Expired authorized session | Renewal disabled |
| Ordinary schedule coverage | UNKNOWN | Separate announcements, assessments, due items and documents | Bounded accessible schedule comparison | Never use calendar alone |
| Real PPTX | OBSERVED original storage; parsing unsupported | Preserve original and explicit unsupported state | Supported parser implementation and bounded validation | PPTX text retrieval deferred |
| Scanned PDF / visual fallback quality | UNKNOWN | Preserve originals; partial diagnostics | Authorized representative scan | No complete visual-understanding claim |
| Complex DOCX tables | UNKNOWN | Preserve structure where supported; report limitations | Representative authorized original | Compatibility deferred |
| Legacy / other office formats | UNKNOWN | Store original; explicit unsupported state | Specific format requirement | Compatibility deferred |
| Precise remote delta tokens | UNKNOWN | Explicit traversal and scoped freshness | Validated source contract | Delta optimization disabled |
| Signed route lifetime | UNKNOWN | Acquire route only for an active fetch | Bounded resource retrieval | Never persist route |

Live checks of course discovery, content traversal, announcements, assessments/due
reads, and resource retrieval require a usable authorized transport for current live validation. They are required to
characterize real usability separately from the public synthetic suite.

## Current live transport gate

An authenticated browser UI was accessible during M3 preparation. Direct navigation
to an already documented read-only API endpoint was blocked by the browser client.
No credentials were extracted and no bypass attempted. Automatic end-to-end adapter
transport remains NOT VALIDATED; synthetic tests and prior-capture replay cannot
remove this limitation. Detailed evidence is private.

The M3 adapter translates discovered course/content records and persists resource
metadata under synthetic tests. Its session provider and wire executor must be
supplied by an authorized integration. Resource bytes additionally require an
explicit fresh-route capability; no browser credential extraction or automatic
SSO executor is supplied. Exact download identity is checked before dispatch.

## Bounded historical replay

The implemented membership translator accepted a prior captured response and its
terminal-page pagination. A single assessment content object also translated,
using a synthetic page envelope. This does not validate a full child-list traversal
or attachment discovery: those response samples were unavailable in the bounded
capture set. Assessment/announcement translators were not yet implemented at this
checkpoint. No network calls were made; detailed replay evidence is private.

## Event-source implementation limits

M6 supports synthetic announcement and assessment reads, immutable source observations,
and deterministic candidate extraction. Ordinary schedule reads remain unsupported until
a response shape is validated. Due-calendar start/end fields are retained without assuming
they establish due semantics. Structured assessment due fields retain separate provenance.
Prose extraction covers bounded English patterns with explicit dates, course weeks, and
selected timezone forms; completed processing does not establish exhaustive event recall.
Canonical reconciliation is implemented in M7; automated event sync modes are implemented in M8.

A bounded replay after M6 acceptance passed the implemented announcement, assessment,
and due-item translators against historical responses. Assessment due fields retained
their distinct sources; due-item composite identities remained distinct and coverage
remained UNKNOWN. No current network transport or live synchronization was validated.

## Reconciliation limits

M7 preserves candidate evidence in source-backed Claims, projects accepted fields,
and returns unresolved identity matches and field conflicts. Source chronology is
distinct from observation order; unknown chronology cannot establish supersession.
Change extraction recognizes bounded affirmative English constructions and abstains
on unsupported wording. This does not establish exhaustive natural-language recall.
Manual field decisions are revisable and retained across automatic reruns. Moving an
already-bound source to another event is conservatively rejected. Local user Claims
remain explicitly local and are excluded from source-backed FTS documents; event
FTS documents index the supported title, while field wording is retrieved through
Claim hits. Source-backed historical references remain stable after projection updates.

## Incremental synchronization limits

M8 composes quick, course, selected-course all, resource fetch, and exact-scope refresh
through injected read-only source/session providers. It distinguishes metadata-based
assumption from verified hash equality and a change back to a historical binary.
Verification receipts are tied to the authoritative version. Unsupported capabilities
remain explicit and are not accessed. No remote delta or validator optimization is used.

Freshness separates latest attempt from prior success/completeness, including equal-time
run ordering and exact windows. Historical/cache-only retrieval never refreshes; allowed
refresh retries local retrieval once. REFRESH_IF_STALE without a configured maximum age
reports unsatisfied/unknown instead of guessing a policy.

Resource omissions are inferred only from complete comparable inventories, never from
partial traversal or inaccessible parents. Evidence ordering protects against delayed
reads and delayed inventories. Content/course availability is not inferred from omission:
those entities do not yet have an immutable lifecycle observation table. Local bytes and
all provenance remain retained. Repeated omissions never establish remote deletion.

Local jobs carry durable configuration contracts and exact parse dependencies. A changed
service contract fails safely; a current plan creates the appropriate new input identity.
Bounded/uncompleted processing and postcommit receipt, job, or browse repair gaps produce
partial scope results. Repeated identical source snapshots reuse their first exact
observation provenance while current freshness is recorded separately.

## Public interfaces

M9 provides a versioned result envelope through the standalone Python CoreService,
CLI JSON/human output, and one thin Codex tool dispatcher. The CLI uses explicit local
keys; no LLM package is required. Default runtime storage is private and local paths
are returned only on explicit resource-path requests. Unconfigured sync returns a
safe configuration error; no automatic browser/SSO executor is bundled.

Queries preserve exact-scope freshness, source references, field conflicts, incomplete
event processing and temporal uncertainty. Explicit historical version queries do not
access the source. One permitted refresh cycle may discover new courses while leaving
their unobserved scopes UNKNOWN. WEEK_ONLY or missing event times remain visible with
partial coverage. Floating dates use conservative bounds rather than an invented timezone.

The Codex dispatcher is a Python integration surface, not an installed plugin or an
LLM-backed agent. Semantic search and the ChatGPT integration remain deferred.

## Historical Phase 4 live gate recheck

A fresh dedicated browser tab again reached the authenticated course UI. The same
known read-only course API was blocked by the browser client. The tab was closed
without changing account/course state. Current end-to-end live checks remain BLOCKED;
this is not evidence that logging in again alone would resolve the transport limitation.
Synthetic/runtime safety passed independently; no live capability is promoted by that result.

## Browser-assisted follow-up

The built-in browser capture provider and `--browser-capture` CLI entry use private
observations and normal downloads collected through the connected browser host. The
standalone CLI imports those observations; it does not perform browser login or prove
raw API transport compatibility. See [the current diagnosis and evidence boundary](connection-followup.md).

One newly collected course/PDF/announcement/assessment sample has passed installed-wheel
normal CLI ingestion, local search, exact physical-page resolution, persisted-process
retrieval and same-capture reuse checks. Repeated sync preserves resource-backed event
and Claim search projections; simulated capture expiry preserves cached data. This does
not validate real SSO expiry. This bounded observation does not promote
large-list pagination, all courses, all formats or the raw API translator to validated.
A second normal browser download passed targeted hash-verified reuse, preserving the
immutable version, derived counts and source locators. The lock blocker is resolved.
Aggregate coverage and raw API validation remain open. Private extraction evaluation
and its remaining visual/context limits are recorded
in the follow-up rather than inferred from synthetic parser success.

## Daily-library follow-up

The accepted repeatable connection chain is complete. Subsequent bounded collection
covered six accessible course inventories, including four prioritized formal courses.
Normal browser downloads and the installed CLI stored 66 originals, with 65 supported
PDF/DOCX parse results and one unsupported PPTX. Coverage remains layered and partial:
web lesson bodies, embedded content, external modules and selective visual inspection
are not inferred complete from tree traversal or parser success.

See [daily-library validation](daily-library-validation.md) for current acceptance.
The earlier raw API blockers above are historical evidence for that separate lane,
not a prerequisite for the documented browser-host-assisted local trial. No bypass,
automatic SSO renewal or additional external service is part of that trial.
