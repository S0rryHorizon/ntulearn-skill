# Daily question workflow

## Select the private runtime and course

Use the user's configured private runtime root. The CLI discovers it in this order:
`--root`, `NTULEARN_DATA_DIR`, private host file `~/.ntulearn-skill/config.json`, then
the default `~/.ntulearn-skill/`. The host file contains an absolute `runtime_root` and
must stay outside the installed Skill and public repository. First run:

```console
ntulearn --json courses --freshness cache-only
ntulearn --json library-status --freshness cache-only
```

Commands accept the positive local `course_key`, not the displayed course code or a
remote identifier. If exactly one local course clearly matches the question, select it.
If several match or the course is ambiguous, show code, title, term, local key, and
coverage, then ask the user to choose. Never combine courses silently.

Within one task, retain the selected runtime root and local course key so follow-up
questions resume without another selection round. If the runtime root changes or a later
`courses` result no longer contains that key, select again. The host configuration
selects only the runtime root, not a default course. Any durable course preference must
remain in private host state outside the repository and must be revalidated with
`courses` before use.

`library-status` reports content-node counts and file-backed download, parse, and index
progress, plus local job totals and pending/running/failed counts for that course. It does
not establish that non-file content-page bodies or folder descriptions were captured.
Surface its coverage and `content_page_body_coverage_unavailable` warning. If local work
remains after a user-requested fresh sync, the browser workflow may resume the same
idempotent queue with a bounded `sync COURSE_KEY --max-jobs N`; never raise the bound in
a cache-only answer.

## Decompose a bounded question

Tell the user which local evidence families and lexical terms you checked. Generate
small searches from the question's own meaningful terms plus their Chinese/English
counterparts. Run concepts separately because local FTS joins words within one query
with `AND`. Do not search only a single canned English keyword, and do not claim broad
NLP or semantic recall.

| Intent | Primary Core/CLI reads | Bilingual lexical decomposition and fallback |
| --- | --- | --- |
| Future assignments, tests, exams, presentations | `upcoming --days N`, then `assessments` | Search separate variants drawn from `assignment/homework/作业`, `quiz/test/exam/测验/考试`, and `presentation/oral/slides/展示/汇报`. Include course-specific nouns from the question. |
| Assessment requirements | `assessments` for typed instructions and dates | Search the assessment title or topic, then separate variants such as `requirements/instructions/rubric/submission/format/word limit/要求/说明/评分/提交/格式/字数`. |
| Which lectures are examined | lexical `search` over local metadata and file text | Search separate variants such as `exam/test/midterm/final/考试/期中/期末`, then `scope/coverage/covered/topics/范围/考点` and relevant lecture/topic names. A hit containing “lecture” alone does not prove inclusion in an exam. |
| Recent material updates | `recent-materials COURSE_KEY --days N`, plus `library-status --course COURSE_KEY` | Report only `FIRST_OBSERVED`, `FIRST_RECORDED`, `METADATA_CHANGED`, `BINARY_AVAILABLE`, `BINARY_CHANGED`, or `AVAILABILITY_CHANGED` records backed by actual resource observations. Do not infer an update from a parse/index replay time. |
| Another bounded content question | `search` with the original significant terms and transparent translations | Return literal local hits and coverage. Say the intent was not one of the supported typed routes; do not present the fallback as general NLP. |

For example, “未来 7 天有什么作业、考试和 presentation？” normally uses:

```console
ntulearn --json upcoming --course COURSE_KEY --days 7 --freshness cache-only
ntulearn --json assessments COURSE_KEY --freshness cache-only
ntulearn --json search "assignment" --course COURSE_KEY --freshness cache-only
ntulearn --json search "homework" --course COURSE_KEY --freshness cache-only
ntulearn --json search "exam" --course COURSE_KEY --freshness cache-only
ntulearn --json search "test" --course COURSE_KEY --freshness cache-only
ntulearn --json search "presentation" --course COURSE_KEY --freshness cache-only
```

This decomposition lets a Chinese question retrieve English source evidence. Adjust the
variants to the user's wording; do not execute every example term when it is irrelevant.

## Present an upcoming window conservatively

Do not describe every item returned by `upcoming` as confirmed inside the requested
window. Core deliberately retains an event when any relevant temporal evidence is
unbounded because it may overlap. Inspect `start_time`, `end_time`, `due_time`, open
conflicting claims, `precision`, `date`, `local_time`, `week`, and `source_text` before
presenting it.

- Put an exact instant inside the window under confirmed upcoming work.
- For `DATE_ONLY` inside the requested calendar-date range, say the date is known and
  the specific time is unconfirmed. Do not invent a timezone.
- If an exact instant or source date is clearly before the requested window, omit it
  from the main upcoming list or label it as past evidence.
- Put `WEEK_ONLY`, `UNKNOWN`, and a `local_time` without a source date in a separate
  “时间未定位” group. Quote the retained source wording and never claim it falls within
  the next N days.
- If open alternatives cross the window boundary, show the conflict instead of choosing
  the convenient date.

Synthetic example for a request covering `[D, D+7)`: an exact `D+2` deadline is
confirmed; a source date `D-1` is past; `Week 4` and `14:00` without a date are both
“时间未定位”. The latter two remain visible because the Core result is conservative,
not because their membership in the seven-day window was established.

## Empty events and evidence fallbacks

An empty event result is conclusive only for its explicit time window when every relevant
coverage and freshness entry is complete and current. Even then it establishes only the
typed event projection. Check `assessments` and targeted searches of announcements,
assessment instructions, metadata, and indexed file text before answering a practical
question about assignments, exams, or presentations.

If those fallbacks are empty or incomplete, say “在当前本地覆盖范围内未找到” and name
the missing or stale scopes. Do not say the arrangement does not exist. Normal content
page bodies and folder descriptions are not currently established as captured/indexed;
they may contain requirements or dates missing from cache-only results.

## Exact sources and answer wording

Use each relevant result's `provenance` entry with `source`:

```console
ntulearn --json source SOURCE_KEY --kind source_observation
ntulearn --json source SOURCE_KEY --kind resource_observation
ntulearn --json source LOCATOR_KEY --kind source_locator --context-window 1
```

Present PDF physical page indices as one-based page numbers. Keep source keys, version
keys, and locators in the answer so a later query can reproduce the evidence. Separate
source-backed statements from interpretations and mention unresolved conflicts.

For `recent-materials`, “no items” means no changed resource observations were recorded
in the local window. It does not prove that NTULearn had no remote changes between
observations. For library progress, show both numerator and denominator; avoid saying
“fully indexed” when non-file content bodies are unavailable or parse coverage is partial.

## Cache-only boundary

For a cache-only request, pass `--freshness cache-only` on every query. Do not pass
`--browser-capture`, `refresh-if-stale`, `require-current`, or `--max-age-seconds`; do
not call `sync` or `fetch`. A cache-only answer must have `refresh_attempted=false`.
