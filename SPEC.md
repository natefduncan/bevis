# Bevis — Specification (draft 0)

Bevis is a host for **auditable reports**. A report is prose, charts, and
the evidence behind them. Every claim and chart links to the queries,
rows, and arithmetic that support it. AI agents author and edit reports
through an API; humans scrutinize and share them through links.

Status: draft. Decisions below were made in design discussion; open items
are listed at the end.

---

## 1. Goals and non-goals

**Goals**
- A reader can click any claim or chart mark and see the SQL, the cited
  rows, the assumptions, and the derivation behind it.
- The host verifies what it can (arithmetic, number matching, key
  uniqueness) and is honest about what it cannot.
- Agents have full read/write access through an API, with a human
  approval gate on what the report *asserts*.
- Reports are shared by link, and portable through a one-way export.

**Non-goals (v1)**
- The host never connects to the user's data sources and holds no data
  credentials (Architecture 1).
- No user accounts, no import, no live re-run of queries.
- No natural-language-number parsing ("twelve percent").
- No real-time multi-user editing.

---

## 2. Architecture

```
agent (local/sandbox) --evidence + edits--> host API --> SQLite (source of truth)
                                              |
                              validator (every write)  --> web view (browser renders)
                                                       --> export job (bundle, PDF)
```

- **Agents run queries wherever they already run** and submit results.
  The host stores snapshots and checks them.
- **Source of truth: the database.** Files are a one-way export.
- **Host:** Python (FastAPI) and one SQLite file. The service is a single
  process whose only external dependencies are Pandoc/XeLaTeX (PDF) and
  `vl-convert` (chart rendering).
- **Interfaces:** REST and an MCP server are two front ends over the same
  handlers. There is one contract.
- **Rendering:** the browser renders the web view from report JSON.
  PDF/static exports are rendered server-side from a bundle. Text blocks
  are the one exception in the web view: the server converts them to
  HTML with the same Pandoc parse the validator uses, so the page marks
  exactly the spans that were checked (§7.1).
- **Deployment:** develop locally; package as one container from day one;
  move to an always-on host when the first report is shared with someone
  else. All state is the SQLite file. Secrets come from environment
  variables, never from the database.

---

## 3. Data model

### 3.1 Report, draft, versions

- A report has one **working draft** and zero or more **published
  versions**.
- Agents and editors change the draft freely (subject to §6).
- **Publishing** validates the draft and stores an **immutable snapshot**:
  blocks, claims, chart specs, and pinned evidence versions (pointers,
  not copies).
- Publishing is blocked if validation **errors** exist, unless the
  publisher overrides. The override and its reason are recorded on the
  version. Warnings never block.
- There is no separate "frozen" state. Published versions are inherently
  immutable.

### 3.2 Blocks and claim spans

A block has: `id`, `order`, `type` (`heading` | `text` | `chart` | `image`),
and content.

Text blocks are Markdown with **inline claim spans** using Pandoc span
syntax:

```markdown
[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}
```

- A span `#cN` must have a row in `claims`. A claim row whose span is
  missing from the text is an error. The validator rejects a write that
  drops a span for a live claim.
- A span may cover **several numbers**. Its evidence must support all of
  them.
- Edits that rewrite surrounding text keep spans, so claim links survive.

### 3.3 Claims

A claim has: `id`, `block_id`, the list of **numbers** it covers, its
evidence references, and a `status` computed by the validator.

Each number in a claim is checked at the strongest tier available:

| Tier | Meaning | Check |
|---|---|---|
| `direct` | The number is a cell value, formatted | Exact match to the cell, at displayed precision |
| `computed` | A structured expression over cells | Host evaluates the expression; result must match at displayed precision |
| `asserted` | Free-text derivation only | Recorded, not verified; shown amber in audit mode |

Claims with no numbers (qualitative assertions) are `asserted` and must
still cite evidence.

The host **re-evaluates every claim on every write** (cheap, prevents
stale green checks).

### 3.4 Evidence

An evidence record is **immutable and versioned**.

- Each submission is logged as a **run**. A run whose data differs from
  the latest version creates a new version. Versions are never edited.
- Evidence is identified by `id` (for example `q-rev-yoy`) and an integer
  `version`. A claim **pins** `(id, version)`.
- Dedup: a run whose data hash equals the latest version's is recorded
  as a "same result" run against that version and does not create a new
  version. If its SQL, source, params, assumptions, `partial`,
  `source_row_count` or key differ from that version's, the run entry
  flags it ("assumptions changed", "partial changed", "source row count
  changed", "key changed", like "query text changed"). The version keeps
  its own metadata, and the `add_evidence` response note names the
  metadata the run did not store.
- When a newer version exists, claims pinned to older versions are
  **stale**. An agent or editor re-points them. The host re-verifies the
  claim against the new data and shows the diff (changed cells, changed
  computed values).
- **Evidence is append-only and does not require approval**, because it
  changes nothing a reader sees until a claim points at it (§6).

**What the agent sends** (the host derives everything else):

| Field | Source |
|---|---|
| `id`, `source`, `sql`, `params`, `assumptions[]` | agent |
| `key` (columns identifying a row) | agent |
| `columns[]` with types | agent |
| `rows` (typed values) | agent |
| `run_at` | agent (informational) |
| `row_count`, `data_hash`, cell index | **host** |
| `provenance` | host; `agent-reported` in v1 |

- **IDs:** agent-chosen slugs: lowercase letters, digits and hyphens,
  3 to 64 characters, starting with a letter. Unique **per report**. A
  slug is the **identity** of the evidence, not its SQL text. Submitting
  an existing slug adds the next version, even if the SQL changed, so an
  agent can fix a bug and keep its claims' references. The diff for a
  new version flags "query text changed" or "source changed" so a
  reviewer sees it. The `add_evidence` response states whether it
  created version 1 or version N of an existing slug, and the call
  accepts `create_only: true` to fail instead of versioning, which
  protects against two agents unknowingly choosing the same slug.
- **Types:** `string`, `integer`, `decimal`, `date`, `datetime`,
  `boolean`, `null`. Decimals are exact (serialized as strings, stored
  and evaluated as exact decimals, never floats).
- **Hash:** SHA-256 over a canonical serialization (columns in declared
  order, rows sorted by key, typed values in canonical text form). The
  exact serialization and the canonical form of each type are defined in
  `spec/bundle/README.md`.
- **Key:** a composite of one or more columns. The host rejects a record
  whose key is not unique. A single-row result may declare an empty key.
- **Size:** a host-configurable cap on each record, defaulting to
  **10,000 rows and 5 MB**. Above it the host rejects with an error such
  as "result has N rows; aggregate, or submit a sample and state that in
  `assumptions`".
- **Partial results:** a record may be a sample or top-N of a larger
  result. It then carries `partial: true` and `source_row_count`, shown
  in the PDF and web view ("sample of 3,000 of 48,211 rows"). Claims can
  only cite cells that are present. A claim whose arithmetic needs the
  full result must cite an aggregate record instead.
- **Provenance levels:** `agent-reported` (v1). Reserved for later:
  `host-verified`, `browser-checked`, `receipt-signed`.

### 3.5 Cell references

A cell is addressed by `(evidence id, version, key values, column)`:

```
cell(q-seg-delta, "Mid-market", 2025, revenue)
```

Key values are given in the declared key-column order. A record with an
empty key (single row) is addressed with no key values:
`cell(q-total, revenue)`.

**Version comes from the pin.** Expressions do not name a version. A
cell reference resolves to the version that the enclosing claim (or
chart) pins for that evidence id. A claim may pin **at most one version
per evidence id**; pinning two versions of the same id is an error. A
comparison across versions is made by submitting the data under
separate slugs, or as one record that holds both.

### 3.6 Charts

A chart block is either:

- **A Vega-Lite spec bound to evidence** (first-class). The spec's data
  is a **reference** to a pinned evidence version, never inline values or
  external URLs. Each mark carries its row key, so clicking a mark opens
  that row in the evidence panel. The validator enforces "chart data =
  cited evidence". Specs have a size and complexity cap. A spec may not
  contain a `url` key or an `image` mark anywhere, since either makes the
  renderer load something from outside the evidence.
  - **Transforms are strict.** The only allowed entry in `transform` is
    `filter`, so each mark corresponds to one evidence row. Every other
    Vega-Lite transform is a validation error. That includes transforms
    that create or combine values (`aggregate`, `joinaggregate`,
    `window`, `calculate`, `bin`, `timeUnit`, `lookup`, `pivot`, `fold`,
    `impute`, `density`, `regression`, `loess`, `quantile`) and `sample`,
    which drops rows unpredictably.
  - **Encodings are checked too.** An encoding channel may not set
    `aggregate`, `bin`, `timeUnit`, or `impute`, because each of these
    merges or creates rows. An encoding `sort` is allowed, since it only
    orders marks.
  - **Composite marks are refused.** A `mark` (a string or `{"type": ...}`)
    may not be `boxplot`, `errorbar`, or `errorband`, at any depth (layers,
    concat, facet), because each draws one mark from many rows.
  - **Top-N and derived series are done in SQL.** Vega-Lite has no limit
    transform. A chart of the top N rows cites an evidence record that
    holds those N rows (marked `partial` if it is a slice of a larger
    result). A derived series is computed in SQL and submitted as its
    own evidence record.
  - Stacking, scales, colors, and axes stay allowed.
  - The host attaches each row's key columns to its mark, so authors do
    not wire this up.
  - Layered charts are allowed, each layer bound to one evidence record.
  - If strictness proves too limiting, a relaxed mode that falls back to
    figure-level evidence for transformed charts can be added later.
- **An image** (escape hatch). Linked to evidence at figure level only.
  Audit mode shows a visible **"figure-level evidence only"** badge.

---

## 4. Expression language (v1, deliberately small)

Evaluated by the host in exact decimal arithmetic. No variables, loops,
or I/O. The grammar and a shared test-case suite are part of the spec.
They live in `spec/expr/` so other implementations can be checked
against them.

```
expr     := term (("+" | "-") term)*
term     := factor (("*" | "/") factor)*
factor   := number | cell | call | "(" expr ")" | "-" factor
cell     := "cell" "(" evidence_id ("," key_value)* "," column ")"
call     := name "(" [expr ("," expr)*] ")"
name     := "pct_change" | "share" | "sum" | "min" | "max" | "rank"

evidence_id := [a-z][a-z0-9-]{2,63}          (the slug rule, §3.4)
column      := identifier | string
identifier  := [A-Za-z_][A-Za-z0-9_]*
key_value   := number | "-" number | string | "true" | "false" | "null"
number      := [0-9]+ ("." [0-9]+)?           (no exponent, no separators)
string      := '"' ( strchar | escape )* '"'
strchar     := any character except '"' and '\'
escape      := '\' '"' | '\' '\'
```

In the grammar, single quotes enclose one literal character. Inside a
string, the only escapes are backslash-quote (a `"`) and
backslash-backslash (a `\`). Any other backslash is a parse error.

Lexical and parsing rules:

- Whitespace between tokens is ignored. Keywords and function names are
  case-sensitive.
- The first argument of `cell` is always read as a slug, so hyphens in
  it are not minus signs.
- The **last** argument of `cell` is the column and the arguments
  between the slug and the column are key values. Parsing therefore needs
  no knowledge of the evidence. The host then checks that the number of
  key values equals the record's key arity; a mismatch is an error.
- Key values are coerced to the key column's declared type: a number for
  `integer` or `decimal` (exact), a string for `string`, an ISO 8601
  string (`"2025-03-01"`, `"2025-03-01T00:00:00Z"`) for `date` or
  `datetime`, `true`/`false` for `boolean`. A value that cannot be
  coerced is an error.
- A column name that is not a valid identifier (spaces, hyphens, a
  leading digit) is written as a string: `cell(q-x, 2025, "net revenue")`.
- A cell whose value is `null` cannot be used in arithmetic; doing so is
  an evaluation error.

Functions:

| Function | Meaning |
|---|---|
| `pct_change(old, new)` | `(new - old) / old` |
| `share(part, total)` | `part / total` |
| `sum(e, ...)`, `min(e, ...)`, `max(e, ...)` | aggregate over arguments |
| `rank(x, e, ...)` | 1-based rank of `x` among the `e` arguments, descending |

`rank` uses competition ranking: the result is 1 plus the number of `e`
arguments strictly greater than `x`, so tied values share the best rank
(values 9, 7, 7, 5 rank 1, 2, 2, 4). `x` must equal at least one `e`
argument, or evaluation fails. A claim such as "ranked second" over tied
values is therefore checked against the shared rank.

Division by zero is an evaluation error (the claim fails validation).

Division (`/`, `pct_change`, `share`) rounds to 34 significant digits,
half-even (decimal128 precision). Every other operation is exact. Number
matching (§4.1) rounds this 34-digit result. Error codes and the case
format are in `spec/expr/README.md`.

### 4.1 Matching a computed value to displayed text

The validator parses the number as displayed and its precision, then
compares to the evaluated value:

- Parsed forms: thousands separators, `$`, `%`, suffixes `K` `M` `B`
  (case-insensitive), and negatives written with `-` or `−`. Digits are
  ASCII `0`–`9` only, here and in the year and date rules of §5. The
  exact token pattern is in `spec/expr/README.md`, with cases in
  `spec/expr/matching.json`.
- Compare `round_half_up(value scaled to the displayed unit, displayed
  decimals)` to the displayed number. `$4.21M` matches 4,210,000 and does
  not match 4,215,000. Rounding is half-up (away from zero) and fixed
  (not configurable).
  A less precise display is a weaker claim: `$4.2M` accepts any value
  that rounds to 4.2 in millions.
- Percentages: the value is multiplied by 100 before comparison.
- **Sign:** a number has an **explicit sign** when `+`, `-`, or `−`
  immediately precedes it, or precedes its `$` (`-12.0%`, `+3%`,
  `-$1.2M`). Without one, the comparison uses magnitude only. With one,
  the signed displayed value is compared to the signed evaluated value,
  so `+12.0%` against a value of −0.11995 is a mismatch (an error). Zero
  matches either sign. Direction words are checked separately (§4.2).
- Written-out numbers are not parsed and produce a warning.

### 4.2 Direction check

Applies only to `computed` numbers that express a **change**. It does
not apply to `direct` numbers.

A number is a change when the **outermost** operation of its expression,
after removing enclosing parentheses, is either a `pct_change` call or a
binary subtraction. `pct_change(a, b)`, `a - b`, and `(a - b)` are
changes. `(a - b) / a`, `share(a - b, c)`, and `-a` are not, and are not
direction-checked. An author who wants a ratio checked writes it with
`pct_change`.

The validator looks for a direction word in the same span:

- **Down:** fell, dropped, declined, decreased, shrank, contracted.
- **Up:** rose, grew, increased, climbed, expanded.

Words match whole words, case-insensitively, and only in the forms
listed. `up`, `down`, `higher`, and `lower` are deliberately excluded:
they often appear in phrases that state no direction ("made up of",
"higher-margin", "broken down by"), and a false match would be an error.

Outcomes:

- A direction word that **contradicts** the sign of the evaluated value
  is an **error**.
- **No** direction word and **no** explicit sign is a **warning**
  ("direction not stated").
- A direction word and an explicit sign are checked independently: the
  sign by number matching (§4.1), the word here. "Fell −12.0%" passes
  both when the value is negative; "fell +12.0%" fails the sign check
  whatever the word says.
- A span with words from **both** lists ("revenue fell 12% while costs
  rose 3%") is not direction-checked, because the validator cannot tell
  which word belongs to which number. It reports info ("direction
  ambiguous; split the span"). Each change in its own span gets a full
  check.
- A change that evaluates to exactly zero is not direction-checked.
- Words outside the lexicon are ignored, so a miss means no check, never
  a wrong check.
- The lexicon is conservative and lives in `spec/expr/direction.json`,
  with test cases, so it can grow from real reports. A false alarm is
  cleared with the publish override.

---

## 5. Validator

Runs on every write, on suggestion submit, on suggestion accept, and at
publish.

Each rule has a stable code, used in findings and in the shared test
cases.

| Rule | Code | Severity |
|---|---|---|
| Claim has no evidence reference | `claim.no_evidence` | error |
| Span without claim row | `span.no_claim` | error |
| Claim without span, or with its span in another block | `claim.no_span`, `claim.wrong_block` | error |
| Claim span repeated, or inside another claim span | `span.duplicate`, `span.nested` | error |
| Claim number's text does not appear in its span | `number.not_in_span` | error |
| Number in a span that the claim does not cover | `span.uncovered_number` | error |
| Number in a span that cannot be parsed ("3rd", "4.2bn") | `span.unparsed_number` | warning |
| Claim number's text is not a readable number | `number.unparseable` | error |
| `direct`/`computed` number does not match at displayed precision | `number.mismatch` | error |
| Cited cell is null or not numeric | `number.non_numeric` | error |
| Expression fails to parse | `expr.parse` | error |
| Expression fails to evaluate, or cited cell does not exist in the pinned version, or key count/coercion fails | `eval.<code>` (codes in `spec/expr/README.md`) | error |
| Cited rows are withheld at this disclosure level | `eval.withheld` | info (unverified) |
| Chart data is not a reference to cited evidence, or loads anything external | `chart.data` | error |
| Chart uses a disallowed transform or encoding property (§3.6) | `chart.transform`, `chart.encoding` | error |
| Chart uses a composite mark: `boxplot`, `errorbar`, or `errorband` (§3.6) | `chart.mark` | error |
| Chart spec exceeds the size cap (64 KB) | `chart.size` | error |
| Pin to an evidence version that does not exist | `pin.unknown` | error |
| `direct` cell cites a version the claim does not pin | `pin.unpinned_cell` | error |
| Claim or chart pins two versions of the same evidence id | `pin.two_versions` | error |
| Explicit sign does not match the sign of the evaluated value | `number.sign_mismatch` | error |
| Evidence key is not unique, or hash mismatch | (bundle/write rejected) | error (rejects write) |
| Claim pinned to a stale evidence version | `pin.stale` | warning |
| Number tier is `asserted`, or claim states no numbers | `number.asserted`, `claim.asserted` | info (amber in audit mode) |
| Direction word contradicts the sign of a computed change | `direction.contradicts` | error |
| Computed change with no direction word and no explicit sign | `direction.unstated` | warning |
| Span holds both up and down words (direction not checked) | `direction.ambiguous` | info |
| Number in block text outside any span | `text.uncited_number` | warning |
| Written-out number in a span | `text.written_number` | warning |
| Image chart with figure-level evidence only | `image.figure_level` | info |

**Statuses.** A number's status is the worst of its findings: `error`,
`warning`, `unverified` (from `number.asserted` or `eval.withheld`), or
`ok`. A claim's status is the worst of its numbers and its claim-level
findings, and `claim.asserted` makes it `unverified`.

**Numbers in spans.** Every number in a span is covered by one of the
claim's numbers. Each claim number consumes one occurrence of its exact
text, and an uncovered number is an error, since a span's evidence must
support all of its numbers (§3.2). Years and dates in a span, by the same
rules as below, need no covering number. A claim number whose text is a
year is checked like any other.

**Uncited numbers.** The validator scans text outside spans for numeric
tokens: block text, heading text, and chart and image captions. It ignores:

- **Years:** a bare integer of exactly four digits from 1900 to 2100,
  with no separator, decimal point, `$`, `%`, or unit suffix.
- **Dates:** ISO 8601 dates and datetimes, and a day (1 to 31) or year
  number directly next to a capitalized month name or its abbreviation
  ("March 3", "3 March 2025", "Sept. 14").
- **List markers:** a number at the start of a line followed by `.` or
  `)`.
- **Code:** anything inside inline code or a code block.

A four-digit number used as a quantity ("2025 customers") is mistaken
for a year and not flagged. Listing it as a claim number still checks it.
An author or agent can mark a number **non-data** with a short reason
(`non_data` in `report.json`: block, exact token text, reason). That
clears the warning for that token in that block and is recorded in the
activity log.

Accepting a suggestion that leaves a claim failing validation is allowed
(a human may override). The claim shows red in audit mode.

---

## 6. Editing, roles, and approval

Token roles: `reader`, `commenter`, `suggester`, `editor`, `admin`.

| Change | reader | commenter | suggester | editor | admin |
|---|---|---|---|---|---|
| View (within disclosure level) | yes | yes | yes | yes | yes |
| Comment / file challenge; see comments and challenges | | yes | yes | yes | yes |
| Add evidence (append-only) | | | yes | yes | yes |
| Propose change to blocks, claims, charts | | | yes (suggestion) | direct | direct |
| Accept/reject suggestions | | | | yes | yes |
| Publish | | | | yes | yes |
| Manage tokens, share links | | | | | yes |

- **Credentials.** An **agent key** names an agent across the host. It is
  created by the operator (`bevis keys create <name>`) and can create
  reports. Its role is **editor** on reports it created and
  **suggester** on all others. A **link** (§7) is a capability for one
  report with a fixed role. Creating a report returns a one-time
  **admin link** for its human owner, who manages the other links. The
  host stores only SHA-256 hashes of secrets.
- Suggesters see the live draft, because a suggestion is made against it.
  Readers and commenters see published versions only.
- **Edits are operations.** A direct edit and a suggestion are both a
  list of operations, applied atomically: `set_title`, `upsert_block`,
  `move_block`, `delete_block` (which removes its claims and non-data
  markings), `link_claim`, `unlink_claim`, `repin` (moves claims and
  charts to another version of one evidence id), `mark_non_data`, and
  `unmark_non_data`. Every write validates the result and returns the
  findings, plus each claim or number whose status or computed value
  changed. An edit or a suggestion may carry `base_revision`, and it
  fails if the draft has moved on since.
- Block ids are host-assigned (`bN`) unless given. Claim ids are chosen
  by the author, because the span `{#cN}` must name them; `get_report`
  returns the next free ids.
- A **suggestion** is one atomic bundle (text change, claim links, new
  evidence references). It is validated on submit and again on accept,
  and records the draft revision it was validated against. The
  reviewer sees a diff and the validator results.
- Suggestion diffs highlight claims whose evidence version or computed
  value changed.
- Adding an evidence version re-validates the draft, since claims pinned
  to the old version become stale. The response lists those claims and
  charts and the diff from the previous version.

### 6.1 Activity log

Append-only. Each entry: `id`, `report_id`, `author` (`{kind, name,
verified}`), `action`, `before`, `after`, `timestamp`. Every edit,
suggestion, acceptance, publish, comment, and non-data marking is
logged. The author field can hold a verified or unverified identity so
accounts can be added later without changing the model.

---

## 7. Sharing and disclosure

- **Capability links, no accounts (v1).** A link is an unguessable token
  with a role, optional expiry, revocation, and a **disclosure level**.
  Commenters enter a display name (recorded as unverified). The author
  record then carries both the name given and the link's own name.
- An admin manages links in the web view (Share) or through the API.
  A new link's secret is shown once. An admin cannot revoke the link
  they are using.
- The `tokens` table is designed so a token can later map to a user
  identity without changing the permission model.
- Links target a **specific published version** or **"latest
  published"**. Only suggester, editor, and admin links see the live
  draft.

**Disclosure levels** (enforced on the server; withheld data is never
sent to the browser):

| Level | Includes |
|---|---|
| `full` | all stored rows |
| `cited` | only rows and columns that claims reference, plus SQL and metadata. A chart references every row it draws and the columns it encodes |
| `metadata` | SQL, row counts, derivations, assumptions, no data values. Computed values are withheld too; expressions stay |

Disclosure applies to the web view, API responses, and exports.

### 7.1 Web view

A link opens at `/view#key=<secret>`. The secret sits in the URL fragment,
which browsers never send to the server, so it stays out of server and
proxy logs. The fragment also carries the permalink state: `v` (version),
`c` (an open claim), and `e`/`row` (an open evidence record and row).

- **Claims** are underlined. Clicking one opens the evidence panel: the
  claim's numbers with their tier, expression or cell, evaluated value,
  and status, then each pinned evidence record with its source, run time,
  provenance, hash, assumptions, SQL, and rows. Cited rows are tinted and
  cited cells are bold. Tables page through all rows the link may see,
  with a "cited rows only" filter.
- **Chart marks** open their row in the panel. Strict charts draw one
  mark per row, so the mark's datum is the row.
- **Audit mode** (button, or the `a` key) colors every claim by status
  (green verified, amber not verified, orange warning, red failed). It
  also lists the findings and outlines blocks with uncited numbers.
- Links that see the draft can switch between the draft and published
  versions.

**Safety.** Author Markdown reaches the page only as server-made HTML:
raw HTML and TeX, images, every author attribute (`{onclick=…}`), and
links other than `http(s)` and `mailto` are removed. Report data is
inserted as text, never as HTML. The page sends a strict
Content-Security-Policy with no inline script and no `eval`. Charts run
with Vega's expression interpreter, so author chart expressions are
interpreted, never compiled, and `href` channels are dropped.
---

## 8. Challenges and comments

- A **challenge** is a first-class record attached to a claim, chart, or
  block **of a specific published version**, with optional highlighted
  cells, reader text, and a status: `open` | `answered` | `resolved` |
  `dismissed`.
- **Pull only in v1.** Agents fetch open challenges with a tool
  (`list_open_challenges`). There is no webhook. One can be added later
  without changing the model.
- An agent replies with a **comment** (explanation, links to evidence) or
  a **suggestion**. Suggestions go through §6, so a challenge cannot make
  an agent change the report unreviewed.
- Resolving requires a human with at least commenter rights. The original
  reader can reopen. An agent cannot close its own challenge.
- If a later version changes the challenged claim, the challenge shows
  against the new version, marked "carried over".

**Rules as built.**

| Action | Who | Status |
|---|---|---|
| File | commenter and up, on a published version | → `open` |
| Reply | anyone who sees challenges; with ops, suggester and up (the ops become a suggestion) | `open` → `answered` when someone other than the filer replies; `answered` → `open` when the filer replies |
| Resolve | a person (not an agent), commenter and up | → `resolved` |
| Dismiss | a person, editor and up | → `dismissed` |
| Reopen | the filer, or a person who is editor and up | → `open` |

- Challenges and comments are visible to commenters and up, and to
  agents. Readers do not see them, in the API or the web view, since their
  text can quote data beyond a reader's disclosure level.
- A comment is on the report (`{}`), a claim, or a block. A claim or block
  that the version commented on (the draft, for those who see it, when no
  version is given) does not have is 404.
- Highlighted cells are keyed by canonical text (as `row_keys` gives
  them). A filer may point only at cells its own disclosure level shows,
  so a challenge cannot probe for hidden rows. A viewer sees only the
  highlighted cells its level shows.
- Seen from version N, a challenge filed on an earlier version is
  listed when its target still exists in N. It is marked `carried_over`,
  with `target_changed` saying whether the claim or block changed. A
  challenge whose target is gone does not show against N.
- The filer is identified by credential, not by name, because names
  given with a shared link are unverified.

---

## 9. Agent interface

One contract exposed as REST (`/api`) and MCP (`/mcp`, streamable HTTP).
Both send `Authorization: Bearer <agent key or link>`. Tools:

`create_report`, `list_reports`, `get_report`, `add_evidence`,
`get_evidence`, `upsert_block`, `link_claim`, `edit_draft` (several
operations at once), `validate_report` (validate or preview ops without
storing), `suggest_edit`, `list_suggestions`, `accept_edit`,
`reject_edit`, `publish`, `add_comment`, `list_comments`,
`list_open_challenges`, `get_challenge`, and `reply_to_challenge`.

Errors are `{"error": {"code", "message", "details"}}`, with HTTP status
401 (no or bad credential), 403 (role too low), 404 (unknown, or not
visible to this credential), 409 (conflict: stale revision, validation
errors at publish, slug exists with `create_only`), 413 (evidence too
large), or 422 (malformed). Access comes before the payload: a report or
version the credential cannot see is 404 whatever its role or payload
(a link asking for a version outside its scope included), a role too
low is 403, and only then are the payload's contents checked (422, 413). A parameter or body of the wrong type
(including a `base_revision` that is not an integer) is 422 `malformed`,
with one detail per failing field. Over MCP the same object is the tool
error.

Thin client libraries (Python first, R later) run a query and POST the
result to `add_evidence`. They are conveniences only. The HTTP API is the
contract.

- **Python:** `bevis.client.Client` runs a DB-API query (`run_query`) or
  takes rows (`add_rows`). It reads each column's type off its values. A
  `Decimal` stays exact, and a float is sent as its shortest round-trip
  text. Query params are recorded as JSON: dates as ISO text, decimals as
  plain text, bytes as hex, other non-JSON values as their `str()`.
- **R:** the `bevis` package in `clients/r` takes a data frame
  (`bevis_add_evidence`) or runs a DBI query (`bevis_run_query`). Doubles
  are sent with 15 significant digits. For exact values, cast to text in
  SQL and declare the column `decimal`. A double column declared `integer`
  is sent as exact JSON numbers, also beyond R's 32-bit integer range; a
  fraction or `Inf` in it is an error.

---

## 10. Export

A published version exports one-way to a **bundle**. The JSON files are
canonical. Markdown, HTML, and PDF are derived.

```
bundle/
  manifest.json
  report.json
  evidence/
    <evidence-id>.v<version>.json
  assets/          (images for image blocks)
  charts/          (derived: chart SVGs for report.md)
  report.md        (derived)
  report.html      (derived, self-contained)
  report.pdf       (derived)
```

- Exports are of a **published version**, never the live draft.
- `report.md` (GitHub-flavored) and `report.html` (one self-contained
  file) are rendered from the same Pandoc document as the PDF, with SVG
  charts. Author attributes and unsafe links are removed, as in the web
  view. Chart `href` channels are dropped in every export format,
  including inside layers. The host serves HTML exports as downloads
  under a CSP sandbox.
- The **disclosure level applies**: a bundle made at `cited` level must
  not contain data the link could not have seen.
- Validation results as of publish time are included.
- `schema_version` is set on `manifest.json` so old bundles stay
  readable. There is no import in v1.
- JSON Schemas for each file, the integrity rules a reader enforces, and
  the value encodings are in `spec/bundle/`.

### 10.1 `manifest.json`

```json
{
  "schema_version": 1,
  "report": { "id": "rpt_01", "title": "Revenue Review: 2024 vs 2025" },
  "version": {
    "number": 3,
    "published_at": "2026-10-20T14:30:00Z",
    "published_by": { "kind": "human", "name": "Nate Duncan", "verified": false },
    "override": null
  },
  "disclosure": "full",
  "validation": { "errors": 0, "warnings": 1, "info": 2 },
  "evidence": [
    { "id": "q-rev-yoy", "version": 1, "file": "evidence/q-rev-yoy.v1.json" }
  ]
}
```

### 10.2 `report.json`

```json
{
  "blocks": [
    { "id": "b1", "type": "heading", "level": 1, "text": "Revenue Review: 2024 vs 2025" },
    {
      "id": "b2", "type": "text",
      "text": "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}."
    },
    {
      "id": "b3", "type": "chart",
      "spec": { "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "data": { "evidence": { "id": "q-rev-yoy", "version": 1 } },
                "mark": "bar",
                "encoding": { "x": { "field": "year", "type": "ordinal" },
                              "y": { "field": "revenue", "type": "quantitative" } } },
      "evidence": [{ "id": "q-rev-yoy", "version": 1 }]
    }
  ],
  "claims": [
    {
      "id": "c1", "block_id": "b2",
      "evidence": [{ "id": "q-rev-yoy", "version": 1 }],
      "numbers": [
        { "text": "12.0%", "tier": "computed",
          "expression": "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))",
          "value": "-0.11995249...", "status": "ok" },
        { "text": "$4.21M", "tier": "direct",
          "cell": { "evidence": "q-rev-yoy", "version": 1, "key": [2024], "column": "revenue" },
          "status": "ok" },
        { "text": "$3.71M", "tier": "direct",
          "cell": { "evidence": "q-rev-yoy", "version": 1, "key": [2025], "column": "revenue" },
          "status": "ok" }
      ],
      "status": "ok"
    }
  ],
  "non_data": []
}
```

### 10.3 `evidence/<id>.v<N>.json`

```json
{
  "id": "q-rev-yoy",
  "version": 1,
  "source": "warehouse.orders",
  "sql": "SELECT year, SUM(amount) AS revenue FROM orders WHERE refunded = false AND year IN (2024, 2025) GROUP BY year ORDER BY year;",
  "params": {},
  "run_at": "2026-10-20T14:01:47Z",
  "provenance": "agent-reported",
  "key": ["year"],
  "columns": [
    { "name": "year", "type": "integer" },
    { "name": "revenue", "type": "decimal" }
  ],
  "rows": [[2024, "4210000"], [2025, "3705000"]],
  "row_count": 2,
  "data_hash": "sha256:…",
  "assumptions": ["revenue = SUM(amount), refunds excluded"],
  "disclosure": { "level": "full", "withheld_rows": 0 }
}
```

At `cited` level, `rows` contains only referenced rows and columns, and
`disclosure.withheld_rows` records how many were omitted. At `metadata`
level, `rows` is omitted.

### 10.4 PDF rendering (first deliverable)

`bevis export pdf <bundle-dir>` reads `manifest.json`, `report.json`, and
`evidence/*.json` and writes `report.pdf`. It requires no database or
server. It becomes the reference implementation of the export format;
the host's export job later writes a bundle and calls the same renderer.

Layout:
- Header: title, version number, publish date, validation summary
  (for example "3 warnings"), disclosure level.
- Body: prose with claim markers rendered as bracketed evidence IDs that
  link to the appendix. Charts are rendered from their Vega-Lite specs to
  vector graphics. Image charts carry the figure-level-evidence note.
- Appendix: one section per evidence record: SQL, result table,
  derivations, assumptions, provenance level, data hash.
- Result tables: up to **25 rows** are shown in full. Above 25, only
  the cited rows are shown. Cited rows and cells are bold, and a note
  reads "showing X of Y rows" whenever rows are omitted. In a full table
  all columns are shown (subject to a page-width limit); when a table is
  cut down, or too wide, the key columns and cited columns are shown.
  The web view is not bound by this and may paginate the full table,
  subject to the disclosure level.
- Engine: Pandoc with XeLaTeX (already installed).
- The PDF renderer honors the bundle's disclosure level and never reads
  data the bundle does not contain.

---

## 11. Storage sketch (SQLite)

`reports` (holding the draft as one JSON document, with its revision and
latest validation), `versions` (immutable snapshots), `evidence` (one row
per version, rows as JSON), `evidence_runs`, `suggestions`, `comments`,
`activity`, `credentials` (agent keys and links), `challenges`, and
`challenge_events`. The schema is in `src/bevis/host/db.py`.

A report is small (dozens of blocks), and every edit validates the whole
draft. So the draft is read and written whole, and history lives in the
activity log and published versions. The schema carries a version
number (2 since challenges were added), and the host migrates an older
database file when it opens it.

Evidence rows are stored with typed values. Large result sets may move to
files with the hash in the record (decided later from real data).

---

## 12. Build order

1. **Bundle schema and PDF renderer** (this document §10), tested on the
   example memo. Expression grammar and test cases in `spec/expr/`.
2. **Validator** over bundles: number matching, expression evaluation,
   uncited numbers.
3. **Host:** schema, API and MCP server, activity log.
4. **Web view:** evidence panel, claim highlighting, audit mode, permalinks.
5. **Sharing:** capability links, disclosure levels, challenges.
6. **Markdown/HTML export, R client, deployment.**

---

## 13. Open items

- ~~**O1. Sign and direction.**~~ Resolved: lexicon check within the span,
  contradiction is an error, unstated direction is a warning (§4.2).
- ~~**O2. Rounding.**~~ Resolved: half-up, fixed, not configurable. Exact
  decimal arithmetic decides boundary cases deterministically (§4.1).
- ~~**O3. Preview truncation in the PDF.**~~ Resolved: full table up to 25
  rows, cited rows only above that (§10.4).
- ~~**O4. Row cap.**~~ Resolved: defaults of 10,000 rows and 5 MB per
  record, configurable on the host; `partial` flag for samples (§3.4).
- ~~**O5. Chart mark keys.**~~ Resolved: strict, so each mark maps to
  one evidence row. The set of allowed transforms was settled in O7
  (§3.6).
- ~~**O6. Evidence IDs.**~~ Resolved: agent-chosen slugs, unique per
  report, a slug is the identity of the evidence (§3.4).
- ~~**O7. Chart transforms.**~~ Resolved: Vega-Lite has no `sort` or
  `limit` transform. Only `filter` is allowed; encoding `sort` is
  allowed; encoding `aggregate`, `bin`, `timeUnit`, and `impute` are
  not. Top-N is done in SQL (§3.6).
- ~~**O8. Cell addressing.**~~ Resolved: empty keys take no key values;
  the version comes from the claim's pin, at most one version per id;
  lexical rules for slugs, keys, and columns are defined, and the last
  `cell` argument is the column (§3.5, §4).
- ~~**O9. Rank ties.**~~ Resolved: competition ranking (§4).
- ~~**O10. Change detection and explicit signs.**~~ Resolved: a change
  is an outermost `pct_change` or subtraction; an explicit sign is
  checked as part of number matching; spans with both directions are not
  direction-checked (§4.1, §4.2).
