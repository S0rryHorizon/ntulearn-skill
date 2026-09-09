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
| Real PPTX | UNKNOWN | Store original; unsupported parser state | Authorized original encountered | Compatibility deferred |
| Scanned PDF / visual fallback quality | UNKNOWN | Preserve originals; partial diagnostics | Authorized representative scan | No complete visual-understanding claim |
| Complex DOCX tables | UNKNOWN | Preserve structure where supported; report limitations | Representative authorized original | Compatibility deferred |
| Legacy / other office formats | UNKNOWN | Store original; explicit unsupported state | Specific format requirement | Compatibility deferred |
| Precise remote delta tokens | UNKNOWN | Explicit traversal and scoped freshness | Validated source contract | Delta optimization disabled |
| Signed route lifetime | UNKNOWN | Acquire route only for an active fetch | Bounded resource retrieval | Never persist route |

Live checks of course discovery, content traversal, announcements, assessments/due
reads, and resource retrieval are pending implementation. They are required to
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
Canonical reconciliation is implemented in M7; automated event sync modes follow in M8.

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
