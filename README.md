# Methods Gap-Filler

![CI](https://github.com/OzaLabCP/Protocol-Builder/actions/workflows/ci.yml/badge.svg)

Paste a paper's **Methods** section. The tool reconstructs the protocol, classifies
every parameter by how well the source specifies it, **asks you only about the gaps
that actually matter**, searches the literature to ground the values it fills, then
emits an executable, protocols.io-style protocol where **every value is
provenance-tagged and every citation is resolved against a real database — its
identifier and metadata checked, with a supporting excerpt attached when the source
exposes one.**

Model-agnostic: the agentic tool-use loop speaks the **OpenAI-compatible
chat-completions** protocol, so it works with any provider that does. Set
`LLM_PROVIDER=openrouter` for one key + any model OpenRouter serves (Claude, GPT,
Gemini, Grok, Llama, …), or `LLM_PROVIDER=anthropic` to talk to the **Claude API
directly** with an `sk-ant-…` key. Switching is a change of `LLM_PROVIDER` +
`LLM_API_KEY` (+ `LLM_MODEL`); the only requirement is tool/function calling. Design
rationale lives in [`methods-gap-filler-spec.md`](./methods-gap-filler-spec.md).

## Why it's trustworthy

Every filled value carries one of five provenance tiers — `stated`,
`literature_grounded`, `best_practice`, `user_input`, `default_verify` — and the tool
never presents an invented value as if it came from the source. Crucially, it doesn't
just *claim* a citation: after the model emits the protocol, the host **resolves every
DOI/PMID** against PubMed (Crossref for DOIs), confirms the retrieved title/year match
the cited ones, and downgrades anything that doesn't resolve — or resolves to a clearly
different work — to `default_verify`. A resolved identifier with matching metadata earns
a `✓ citation metadata verified` badge; this attests the reference is real and points to
the cited work, **not** that the source's text supports the specific value. When the
retrieved source exposes an abstract/description whose text actually contains that value,
the host attaches it as a supporting excerpt and adds `✓ supporting excerpt attached`;
otherwise the value stays grounded but is flagged `⚠ evidence unavailable`. Citations
that don't resolve or point to a different work are flagged and downgraded.

## How it works

| Phase | What happens |
|-------|--------------|
| **1 — read, scope & ask** | Input is pasted Methods text **or an uploaded PDF** (text is extracted host-side and the model finds the Methods section within it — so it works behind any model). It reconstructs the protocol, classifies each parameter, runs a light scoping search for ambiguous gaps, and calls `request_clarifications` with 0–5 targeted questions (menu options carry their source + citation). |
| **2 — research & ground** | Your answers come back as the tool result; the model grounds outcome-critical values with app-run search tools — `search_pubmed` (NCBI), `search_preprints` (bioRxiv/medRxiv via Europe PMC), and `search_protocols` (protocols.io, if a token is set) — then calls `emit_protocol`. Grounding is app-run, so it's identical across providers. |
| **3 — validate** | The host resolves every citation, enforces the provenance/citation invariants, checks the `assumptions_log` against the inline values, then renders the result. |

Forced `tool_choice` is only ever used on a call that is *not* also searching — the
model runs each phase under `tool_choice: "auto"` and the structured tool call is the
terminal signal. (See the spec's Architecture note for why forcing + searching in one
call is impossible.)

### Deterministic checks & quality gate

Alongside the model-run audit, the host runs a **pure, deterministic quality gate**
(`app/checks.py`) over the finalized protocol — no model, no network, no randomness, so
equal input always yields byte-identical output. It re-derives the arithmetic the protocol
asserts and flags what is provably wrong:

- **Dilutions** — `C1·V1 = C2·V2` and dilution-factor math (a stated transfer volume that
  disagrees with `C2·V2 / C1`, or a "final" concentration above the stock).
- **Mass ↔ molarity** — `mass = C·V·MW` (or `V·density`) when a molecular weight or density
  is present.
- **Mixtures** — component volumes vs a stated total; percent/ratio groups that sum past 100%.
- **Plate layout** — `conditions × replicates (+ controls)` against a declared 96-/384-well
  capacity.
- **Physical sanity** — negative volumes/masses/concentrations, pH outside 0–14, percentages
  over 100%, temperatures below absolute zero, and numeric values that carry no unit.
- **Completeness heuristics (conservative, warning-max)** — three checks that flag a likely
  gap but **demote to no finding on any ambiguity**, so they never drive a correct protocol to
  `blocked` and never fire on the example fixture: **`PREP_MISSING`** (a material whose name
  denotes a prepared solution — a buffer/stock/master-mix — with no recipe, no vendor, and no
  preparation step; demotes if any of those is present), **`READOUT_MISSING`** (a ≥2-step
  protocol whose steps never read/measure/acquire and which has no titration series; a
  single-step or titration protocol demotes), and **`CONTROL_MISSING`** (a *clearly
  comparative* design — a titration series or screen/compare/dose/titrate wording — that names
  no control/blank/reference arm anywhere; a non-comparative design is skipped).
- **Structural completeness** — a step with no instruction, a material with no name, an
  incomplete critical parameter, a substep with no instruction, or an unlabeled titration point
  is a **blocking** `STRUCT_*` error (see *Structural repair* below), so a repaired-placeholder
  protocol can never ship as if it were clean.

Findings carry one of four severities, and **ambiguity always resolves downward**
(`info/pass › assumption › warning › error`) so a correct protocol is never driven to
`blocked`:

- **error** — provably wrong given complete, unit-compatible, in-tolerance data. **Any error
  sets the gate `status` to `blocked`** and surfaces that line into `open_questions`.
- **warning** — off or unverifiable because a premise is missing or ambiguous (e.g. a value
  with no unit, incompatible units). In-report only.
- **assumption** — the check supplied an unstated premise instead of computing (e.g. a
  mass/molarity cross-check with **no molecular weight** stated) and recorded *why*. Neutral:
  it never affects status or counts.
- **info** — the check ran and passed.

The bucketed result lands on `report["quality_gate"]` (`status`, per-severity lists, and
`counts`); `status` is `blocked` if any error, else `warnings` if any warning, else `ok`.
Alongside `status` the gate also exposes a spec-facing **`status_label`** — a pure additive
projection of `status`: `ok → ready`, `warnings → ready_with_warnings`, `blocked → blocked`.
It is a projection only; `status` and `counts` are byte-identical to before, so every existing
reader is untouched. Each finding also carries additive fields for the UI — `severity_label`
(`error → blocker`, `warning`/`info`/`assumption → warning`/`information`), a deterministic
per-code `suggested_fix` string, a `location_id` id anchor, and `host_verified: true` (the
host, not the model, produced it) — while the seven original finding keys stay unchanged.
Every assumption is also projected into a human-facing **assumptions log**
(`report["assumptions"]`), each entry naming the premise the check supplied and the reason it
did so — kept disjoint from `open_questions`, which carries only blocking errors. Structural
anchors (`structural-path-v1` ids) and the whole gate are recomputed from scratch on every
validation, so re-running it is idempotent — identical `quality_gate`, identical ids, and no
accumulation in `open_questions`.

### Structural repair, `schema_version` & the emit boundary

Before the gate runs, the emitted protocol passes through a **validation/repair layer**
(`app/models.py`, Pydantic v2). It is a *validate/repair gate only*: the emitted dict stays the
runtime source of truth — models are `model_validate`d, never serialized back over it, so
nothing is re-typed or reordered. The model tree is deliberately **lenient** (`extra="allow"`,
so host attestation keys and the stable-id siblings survive; numeric fields accept
`str | number` with no write-back) and rejects only *truly incomplete* entries. Every
finalized protocol is stamped, **host-authoritatively**, with a string
`schema_version = "emit_protocol/1"` (namespaced on purpose — orthogonal to the projects
layer's integer `CURRENT_SCHEMA_VERSION`); a model-supplied value is overwritten, not trusted.

- **Per-entry repair, never a silent gap.** A step with no instruction (or a material/parameter/
  substep/titration-point with its identity field empty) is filled with a **loud frozen
  sentinel** — e.g. `[MISSING INSTRUCTION — reconstruct or delete this step]` — and a `[BLOCKED]`
  note is appended to `open_questions`. The gate's `STRUCT_*` check then turns that sentinel into
  a **blocking error**, and the renderer shows a visible marker, so a repaired protocol is
  loud, never rendered as if it were complete.
- **Bounded emit-boundary repair.** When the model emits a *structurally unusable* payload — the
  FATAL class: not an object, an empty/missing `title`, or a `steps` that is not a list — the
  agent re-emits **exactly once**, quoting the error back to the model; if the re-emit is still
  unusable it raises an actionable error that the existing transactional rollback surfaces as a
  normal error response — **never a partial render**. (Per-entry gaps are *not* fatal: they are
  handled deterministically by the repair-plus-`STRUCT_*` path above, so model calls stay
  bounded.)

### Revision-stable ids

Every entity keeps two ids side by side: the existing **positional** `_id` (`mat:0`,
`step:1/param:2` — moves when a list shifts) and an additive **content-derived** stable id
(`material_id`/`step_id`/`parameter_id`/`substep_id`/`point_id`/`component_id`, e.g.
`s_1a2b3c4d`). The stable id is a hash of the entity's **identity** (its name/label/instruction,
*not* its mutable value), so a corrected value keeps the same id, and it survives both a fresh
re-emit (revise/apply-fixes) and an in-place edit. Assignment is idempotent and preserving, and
same-identity duplicates disambiguate deterministically in document order (`m_<h>`, `m_<h>_1`,
`m_<h>_2`). `/edit` accepts **either** id for its `target_id` (positional first; the prefixes
are disjoint, so there is no ambiguity), so a cached stable id still targets the right entity
after the protocol has been revised out from under it.

### Host-generated assumptions log

The model's own `assumptions_log` is never trusted as an independent copy. On every validation
the host derives its **own canonical log** from the inline non-stated entries
(`report["host_assumptions_log"]`, also attached to the protocol for the renderer), then runs a
**complete field-level comparison** against the model's log — matched by normalized parameter
name — and records every disagreement (value, citation identifier, unit, provenance,
`selected_by_user`, host-derived `verify`, plus missing/extra/duplicate rows) as structured data
at `report["consistency"]["disagreements"]`. So a `10 mM` inline value against a `100 mM` log
copy, or a provenance/citation-identifier that the log gets wrong, is caught with the inline
entry's `_id`/`stable_id` as the anchor. Each disagreement carries its own `severity` (a value or
citation-identifier contradiction is `error`-tier; unit/provenance/selected/verify drift is
`warning`-tier; an empty or unparseable value demotes to `unverifiable`). In this layer the
disagreements are reported **as data** alongside the pre-existing name-based
`consistency` keys — which stay byte-identical — rather than being routed through the quality
gate, so gate `status` is unchanged by them.

## Run it

Requires Python 3.10+ and an API key for one provider.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Option A — OpenRouter (one key, any model):
export LLM_PROVIDER=openrouter
export LLM_API_KEY=sk-or-...                 # https://openrouter.ai/keys
# export LLM_MODEL=openai/gpt-5              # optional; defaults to anthropic/claude-opus-4-8

# Option B — Claude API directly (use your sk-ant-… key):
# export LLM_PROVIDER=anthropic
# export LLM_API_KEY=sk-ant-...
# export LLM_MODEL=claude-opus-4-8           # bare Anthropic id, no "anthropic/" prefix

uvicorn app.server:app --reload --port 8000   # or: cp .env.example .env  &&  edit
```

(The older `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` / `OPENROUTER_BASE_URL` names still
work as aliases for the `LLM_*` ones.)

Open <http://localhost:8000>, then either **paste a Methods section** (or click
**Load example (CFPS)**) **or upload the paper as a PDF** — the text is extracted
host-side and the tool locates the Methods section itself. Answer the questions, read
the protocol, then **download it as Markdown**, **print / save as PDF**, or **refine
it** ("use 150 µL wells and drop to 2 replicates") to rebuild with full context.

Every value is also flagged inline for how to *read* it: 🎛 **flexible** (has latitude,
with the range) versus load-bearing, and 👤 **needs your input** (a decision that still
depends on your setup and wasn't already clarified). An **adversarial correctness + practicality audit runs automatically as part of the pipeline**
(`GAPFILLER_AUTO_REVIEW`, on by default): after the protocol is emitted, a skeptical pass
attacks it for both **correctness** (logic/value/unit/ordering/missing-control errors) and
**practicality** (missing detail, unclear or impractical steps), and its fixes are applied
before you ever see it — so you receive an already-corrected protocol with the findings shown
for transparency (clearly labeled model-generated; any cited identifiers host-resolved and their
metadata checked). Then two
review follow-ups sit next to the protocol: **Design review** (the experiment around it) and
**Test a hypothesis** (does it directly test your hypothesis); **Re-review & fix** re-runs the
audit on demand (e.g. after a manual refine).

### Review as a gate

Applying fixes isn't taken on faith. After either apply path (`POST /api/critique {apply:true}`
or `POST /api/apply_fixes`) rebuilds the corrected protocol, the host runs **one independent,
fresh-context re-review** of the *corrected* artifact against the original findings. The verifier
is a hostile auditor: it runs on a **from-scratch transcript** that never shows the "I just applied
these fixes" turn, so it can't rubber-stamp — it judges only the protocol in front of it and must
quote positive evidence to call a defect fixed.

- **Per-finding verdicts.** Each original finding is judged into one of five outcomes —
  `confirmed_fixed`, `not_applicable`, `still_present`, `partially_addressed`, or `regressed`.
  Identity is a **host-computed content hash** (`finding_key`, severity-excluded so it survives
  re-sorting) that the model can't forge, rename, or invent: keys it didn't receive are dropped,
  and any finding the model stays silent on defaults to `still_present`. The verifier also reports
  new defects its own fixes introduced.
- **`fix_verification.status`.** The host — not the model — adjudicates the result into a single
  `fix_verification` object whose `status` ∈
  `verified_clean` (every finding `confirmed_fixed`/`not_applicable`, no new defects),
  `issues_remain` (something is still open), or `not_reviewed` (the pass couldn't run). A verifier
  failure never blocks delivery — the corrected protocol is still returned, marked `not_reviewed`.
  (This three-value object is a distinct axis from the response-level five-value `review_status`
  label — see [Review gate configuration & idempotency](#review-gate-configuration--idempotency).)
- **It gates the project.** `review_status` feeds `ValidationSummary.status` monotonically toward
  `blocked`: an unresolved **critical/major** prior or new finding escalates the summary to
  `blocked`; a minor-only `issues_remain` escalates a `clean` summary to `warnings`;
  `verified_clean` and `not_reviewed` never loosen it. This stays a **distinct axis** from the
  deterministic Epic-2 quality gate — both feed the one status.

### Review gate configuration & idempotency

**Reviewer tier (`GAPFILLER_REVIEW_MODEL`).** Both the adversarial correctness review and
the post-fix verifier run on their own model tier. It defaults to `LLM_MODEL` — already
resolved to the right per-provider slug — so out of the box the reviewer *is* the main model
and behaves byte-identically on OpenRouter and the Anthropic-direct endpoint. Point it at a
different, provider-appropriate slug to get a genuine **second opinion**: the review reads
only the artifact plus host-owned ground truth (source, protocol JSON, captured user
decisions, retrieved-evidence log, the deterministic quality gate) on a **fresh transcript**
that never contains the authoring reasoning, so a different model audits without inheriting
the first model's blind spots. The slug is used verbatim — no provider mangling.

**Failure policy (`GAPFILLER_REVIEW_REQUIRED`).** A review is best-effort by default: if it
raises (provider hiccup, tool-contract miss), the host **degrades** — it stamps
`review_status="unavailable"`, attaches a human `review_warning`, appends a single deduped
`[REVIEW UNAVAILABLE] …` line to the protocol's `open_questions`, and **still returns the
protocol** (HTTP 200). Set `GAPFILLER_REVIEW_REQUIRED=1` and the same failure instead
**blocks delivery** with `HTTP 424 (Failed Dependency)` — an actionable message naming the
env var, and **no protocol body** — so on a locked-down instance a protocol is never shipped
unreviewed. `424` is deliberately distinct from the `409` reserved for per-session state
conflicts, and the block raises **inside** the session lock and propagates out, so the lock
releases and the transactional rollback/retry semantics are untouched.

**`review_status` vocabulary.** Every completed response carries a single host-decided
`review_status` string (never chosen by a model). It is a pure projection of the deterministic
quality gate plus the fix-verification outcome, first-match-wins:

| `review_status` | Meaning |
|-----------------|---------|
| `skipped` | The response carries no protocol (clarification questions, `/discover` assay options, design-review-only, alignment-only). |
| `unavailable` | A review was attempted but couldn't run (audit or verifier raised) — see the accompanying `review_warning`. |
| `failed` | The deterministic gate is `blocked`, **or** a fresh re-review left an unresolved finding (blocking or otherwise, including any `unconfirmed`). |
| `passed_with_findings_fixed` | Findings existed, fixes were applied, and the independent re-review confirmed every one resolved. |
| `passed` | Ran clean with nothing to fix (no findings, or a gate that only `warnings`). |

This is a separate axis from the `fix_verification` object (whose own three-value `status` —
`verified_clean` / `issues_remain` / `not_reviewed` — is unchanged and still attached
verbatim whenever a verify pass ran). Per-finding outcomes gained one value, `unconfirmed`
("I cannot determine this fix landed"), which counts as unresolved and blocks a clean verdict —
a proposed fix that remains present is marked `still_present`/`unconfirmed`, **never** promoted
to fixed just because a re-emit completed.

**Idempotency keys.** The mutating endpoints (`/api/resolve`, `/api/revise`, `/api/design`,
`/api/critique`, `/api/apply_fixes`, `/api/align`, `/api/choose_assay`) accept an optional
`idempotency_key` in the request body. On a repeat of the same key **on the same session**, the
server returns the **byte-identical** prior result — same `protocol_version_id`, same
`fix_verification` — **without invoking the model again** (no second `ProtocolVersion`, no
duplicate lifecycle advance). The check, the model work, and the record all happen under one
continuous hold of the reentrant per-session lock, so a double-click can't race; the record
happens **only on success**, so a failed or `424`-blocked op is never cached and stays
retryable. The cache is a bounded per-session LRU (keys namespaced by operation), so it is
inherently **per-session** — the same key string on a different session does fresh work — and is
reclaimed with the session. Omit the field and behavior is exactly as before.

**UI control-locking.** While any session-mutating operation is in flight the front-end disables
**every** mutating control (analyze, build, revise, design review, re-review & fix, apply fixes,
align, discover, choose-assay, inline-edit save) via a single global guard, released on both the
success and error paths of the same progress plumbing every long op already runs through. This is
the client-side complement to the server's per-session lock + idempotency: a second click can't
even fire, and if one somehow does the key/lock make it a no-op. Read-only controls (downloads,
print, load-example) stay live.

### Readiness card, views & inline editing

Every reconstructed protocol is topped with a **readiness card** that gives one honest
verdict — `ready`, `attention`, or `blocked` — derived from a single classifier the card,
the screen-reader announcement, and the server all share. The **worst signal dominates**:
a result with no quality gate, or one whose independent fix-verification isn't clean, can
**never** read `ready`. Only a passing gate *plus* a clean/N-A review *plus* a real majority
of independently grounded values earns `ready`, and even then the card says "verify at the
bench — this is not a guarantee." The card lists the specific blocking/warning reasons and
links to the flagged value.

Two **views** toggle the same DOM with a CSS class flip (no re-render): **Evidence** (default,
byte-identical to before — full provenance tiers, citations, and grounding log) and **Bench**
(the clean run/print layout with provenance chrome hidden). The choice persists in
`localStorage`; the tabs are a keyboard-operable `tablist`, and print always emits the Bench
layout.

Any material amount/unit/concentration or critical-parameter value/unit can be **edited inline**
(`POST /api/protocol/{session_id}/edit`). An edit doesn't just swap the number: the server flips
that value's provenance to **`user_input`** ("corrected by you"), strips its now-stale citation
and grounding, then **re-runs the full deterministic gate** and re-renders. So an edit that
corrects a bad value flips the gate `blocked → ok`, and an edit that introduces one flips it
`ok → blocked` — the readiness card always reflects the freshly re-validated, server-returned
protocol, never an optimistic local guess.

### Clarification, PDF review, value locks & recovery

Four honesty-preserving controls sit around the reconstruction loop. Each is **additive and
degrades gracefully** — an older payload, a missing field, or a disabled endpoint falls back
to prior behavior.

- **Clarification with no implicit defaults.** Each gap is answered through a three-choice
  control — **Answer**, **Use the suggested default** (only offered when the model actually
  proposed one), or **Leave unresolved** — instead of a single skip box. The host stamps every
  gap with an **`outcome_critical`** flag (the model's own signal OR-combined with a
  conservative host heuristic: `user_dependent`, a dosing/readout/control keyword, or a numeric
  gap with a plausible range). An outcome-critical gap **blocks submission** until you make an
  explicit choice, announced via an assertive `#gate-error` region. The cardinal rule is that
  **an empty field is never a default**: `_normalize_answer` (the single backend source of
  truth) maps an absent mode + empty value to `unresolved`, downgrades `answered`+empty to
  `unresolved`, and maps every legacy skip/plain-value shape deterministically — so a blank box
  becomes a `default_verify` open question, never a silently invented number. A **preflight**
  panel summarizes what you answered, what defaults you accepted, and what you left unresolved
  before the protocol is generated.

- **PDF review before analysis.** `POST /api/extract_pdf` extracts a PDF's text **host-side**
  (no model) and reports `pages`, `chars_total`/`chars_included`, a `truncated` flag, and an
  **`is_image_only`** flag for scanned/photographed pages. You review and **correct** the
  extracted Methods text in place; the corrected text then flows into `/api/analyze`
  (`is_full_paper=true`) **without re-extraction**. OCR is a clean **seam**: `_ocr_available()`
  returns `False` and `_ocr_pdf` raises `NotImplementedError` with a paste-instead message —
  a future OCR drop-in flips the flag with no caller edits. Direct PDF upload to `/api/analyze`
  still works unchanged.

- **Value locks.** Any material amount/unit or critical-parameter value can be **locked**
  (🔒, persisted per project/session in `localStorage`). The locked id set is sent as
  `locked_ids` on every revise/apply. After the model returns, `_enforce_locks` **restores** a
  locked value the model changed (matched by stable id) and returns the restored ids; if a
  revision makes a locked entity **disappear** it aborts with a `409` and the last valid
  protocol is left untouched. The same set is subtracted client-side so a locked value never
  gates as an "unrelated change."

- **Deep diff, unrelated-change gate & recovery.** After a revise, a field-by-field **deep
  diff** (matched by stable id, so it survives list reordering) detects unit, value,
  provenance, and citation-identifier changes plus warning add/remove. Changes **not** named in
  your instruction and **not** locked raise a confirm gate with a host-only **Revert unrelated**
  action (`POST /api/protocol/{id}/restore`, no model call, provenance intact). Every mutating
  op is **cancellable** (an in-flight `AbortController`; a cancel reads as "your last protocol is
  unchanged," never an error), **retryable** (Retry re-invokes the last op), and a soft
  **Start over** clears transient panels while preserving the last valid protocol, session, and
  locks. A failed or cancelled op **always** preserves the last valid protocol.

## Projects & intake (durable layer)

Above the in-memory run engine sits a thin, **durable projects layer** (SQLite). A
**project** is the persistent record of one piece of work: what you submitted, which
workflow was detected, and every protocol version that was generated for it. Unlike a
session, a project **survives an application restart** and can be reopened by URL
(`?project=<id>`).

- **Universal intake — `POST /api/project`.** Instead of committing to a screen up front,
  you drop in *anything* — free text, a DOI/PMID/URL, a pasted protocol, a hypothesis, a
  measurement goal, or a PDF — and a **deterministic, model-free classifier** picks the
  workflow. This intake path **never calls the model** (its only host-side cost is PDF text
  extraction), so classifying is instant and free.
- **Seven workflows.** `reproduce`, `adapt`, `design`, `measure`, `review`, `troubleshoot`,
  `scale`. Detection auto-emits five of them (a DOI/PMID/PDF ⇒ *reproduce*, a pasted
  protocol ⇒ *adapt*, a hypothesis ⇒ *design*, a measurement goal ⇒ *measure*, review
  intent ⇒ *review*); *troubleshoot* and *scale* are reachable via the workflow cards. Each
  workflow maps to an **entry mode** (`paper` or `hypothesis`) that routes into the existing
  run engine — the engine itself is unchanged.
- **Confirm / override — `POST /api/project/{id}/workflow`.** When the classifier is
  unsure it asks you to confirm; you can also override to any of the seven. Changing the
  workflow **preserves every submitted input** — nothing is cleared or re-extracted.
- **Versioned protocols.** Every time the engine finishes a protocol for a project it writes
  an immutable **protocol version** (the full result snapshot, verbatim), bumps the project's
  `version`, and points `current_protocol_version_id` at the newest. `GET /api/project/{id}`
  rehydrates the latest result so a reopened project renders exactly as before, and
  `GET /api/project/{id}/protocol.md` / `…/materials.csv` serve restart-safe downloads.
- **Isolation & concurrency.** Projects never share transcript or protocol state. Writes use
  **optimistic concurrency** (a per-row `row_version`): a stale write loses the race and the
  API returns **409**; an unknown project id returns **404**.
- **Migrations.** Two orthogonal layers, both fail-closed: a DDL layer keyed on
  `PRAGMA user_version` and an app-data layer keyed on each project's JSON `schema_version`.
  A database or payload stamped newer than the running code supports is rejected rather than
  silently mis-read.

The classic session endpoints still work standalone — `analyze`/`discover` transparently
attach-or-create a project behind the scenes, and a stale project id never blocks a
generation.

### Deploy (Docker)

```bash
docker build -t methods-gap-filler .
docker run -p 8000:8000 -e OPENROUTER_API_KEY=sk-or-... methods-gap-filler
```

Sessions are in memory, so run **one worker** (the image does). `GET /healthz` reports
liveness, the model, and which grounding sources are enabled. Sessions expire after
`GAPFILLER_SESSION_TTL` seconds (default 3600); **projects are durable** and persist in the
SQLite database at `GAPFILLER_DB_PATH` (default `./projects.db`) — mount it on a volume to
keep projects across container restarts.

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `LLM_PROVIDER` | `openrouter` | `openrouter` (any model, one key) or `anthropic` (Claude API direct, `sk-ant-…`). Sets the base URL + provider-specific params. |
| `LLM_API_KEY` | — | Required. Your key for the chosen provider. (Alias: `OPENROUTER_API_KEY`.) |
| `LLM_MODEL` | provider default | Model id/slug — must support tool calling. Defaults: `anthropic/claude-opus-4-8` (openrouter) · `claude-opus-4-8` (anthropic). (Alias: `OPENROUTER_MODEL`.) |
| `LLM_MODEL_FAST` | provider fast default | Cheaper/faster model for the light phases (clarifications, assay discovery); the heavy emit + reviews + fixes stay on `LLM_MODEL`. **Built-in split:** on the default `LLM_MODEL`, the light phases run on the provider's fast model (`…/claude-sonnet-5`) and the heavy work on the strong one (Opus). Picking a custom `LLM_MODEL` turns the split off (one model everywhere) unless you also set this. |
| `LLM_BASE_URL` | provider default | Override the endpoint (e.g. a self-hosted compatible gateway). (Alias: `OPENROUTER_BASE_URL`.) |
| `GAPFILLER_REASONING_EFFORT` | `high` | `reasoning.effort` for the **heavy** phases (emit, reviews, fixes) on models that support it; `""` to omit. |
| `GAPFILLER_REASONING_EFFORT_FAST` | `low` | `reasoning.effort` for the **light** phases (clarifications, assay discovery) — spend thinking tokens only where they add value; `""` to omit reasoning there. |
| `GAPFILLER_SEND_REASONING` | on for `openrouter` | Whether to send the OpenRouter-only `reasoning` field; auto-off for other providers. Set `1`/`0` to force. |
| `GAPFILLER_AUTO_REVIEW` | `1` (on) | Run the adversarial correctness + practicality audit and auto-apply its fixes as part of every generation, so the user receives an already-corrected protocol. Adds ~2 model calls per run; set `0` to make it the manual "Re-review & fix" button instead. |
| `GAPFILLER_REVIEW_MODEL` | `LLM_MODEL` | Model tier for the adversarial correctness review **and** the post-fix verifier. Defaults to the (already provider-resolved) `LLM_MODEL`, so byte-identical by default; set a provider-appropriate slug for an **independent second-opinion** model. Used verbatim — never provider-mangled. |
| `GAPFILLER_REVIEW_REQUIRED` | `0` (off) | When a review **cannot run**: off degrades to `review_status="unavailable"` and still delivers the protocol; on **blocks delivery** with `HTTP 424` (no protocol body) so a review is never silently skipped. |
| `GAPFILLER_PROMPT_CACHE` | on for `openrouter` | Cache the stable prefix (system prompt + source text) so multi-phase runs re-read it instead of re-billing it. Big input saving, identical output. Off by default for non-OpenRouter providers whose compat endpoint may not honor `cache_control`. |
| `GAPFILLER_MAX_TOKENS` | `16000` | Base output ceiling. You only pay for tokens actually generated. |
| `GAPFILLER_MAX_TOKENS_CAP` | `32000` | A truncated emit auto-retries at 2× the budget, up to this cap — so a large protocol completes instead of erroring, while normal ones stay cheap. Set `== GAPFILLER_MAX_TOKENS` to disable escalation. |
| `GAPFILLER_REQUEST_TIMEOUT` | `600` | Per-request HTTP timeout (seconds). |
| `GAPFILLER_PUBMED_BUDGET` | `6` | Max literature searches per phase. Kept modest so each phase stays fast (PubMed is rate-limited) and the emit/audit/fix phases converge within their tool-round budget instead of spending it all on searches. Raise for deeper grounding. |
| `GAPFILLER_ENABLE_PUBMED` | `1` | Set `0` to disable the PubMed grounding tool. |
| `GAPFILLER_ENABLE_PREPRINTS` | `1` | Set `0` to disable bioRxiv/medRxiv (Europe PMC) search. |
| `NCBI_API_KEY` | — | Optional; raises the E-utilities rate limit (3→10 req/s). |
| `PROTOCOLS_IO_TOKEN` | — | protocols.io developer token; when set, enables `search_protocols`. |
| `GAPFILLER_DB_PATH` | `./projects.db` | SQLite file for the durable **projects** layer (intake, detected workflow, protocol versions). Projects survive restarts; put it on a persistent volume in Docker. `:memory:` is honored for ephemeral/test use. |
| `GAPFILLER_AUTH_TOKEN` | — | If set, `/api/*` requires it (`Authorization: Bearer` or `X-API-Key`). Off by default. |
| `GAPFILLER_RATE_LIMIT` | `0` | Per-client requests/minute on the model-driving endpoints; `0` disables. |
| `GAPFILLER_TRUST_PROXY` | `0` | Set `1` to read the client IP from `X-Forwarded-For` (only behind a proxy you control). |

## Tests

```bash
python tests/test_validation.py            # or any tests/test_*.py
```

Every suite runs **without the network** (resolvers/LLM clients are injected or the path
is model-free) and against a throwaway SQLite DB, so nothing touches a real provider or
`./projects.db`. The validation suite covers citation resolution, provenance invariants, and
`assumptions_log` consistency — including the **host-generated canonical log** (a `10 mM`
inline value vs a `100 mM` log copy, and a provenance/citation-identifier mismatch, are
detected) and **revision-stable ids** (a step keeps its `step_id` when an unrelated step is
prepended and its positional `_id` moves). `tests/test_models.py` covers the Pydantic
validate/repair layer: `schema_version` is stamped host-authoritatively, an empty
instruction/name is rejected, a missing step instruction is **repaired to a loud sentinel and
blocks the gate** (never silently rendered), and `coerce_emit_payload` flags only the FATAL
class; `tests/test_agent_loop.py` covers the **bounded emit-boundary repair** — one re-emit on
a FATAL payload, then an actionable failure (never a partial protocol). `tests/test_checks.py`
covers the deterministic gate: a bad `C1V1=C2V2` and an overflowing plate are errors, the new
prep/readout/control heuristics fire and then demote on ambiguity, and the gate exposes
`ready`/`ready_with_warnings`/`blocked` mapped from `ok`/`warnings`/`blocked`;
`tests/test_example_fixture.py` asserts the shipped fixture still validates clean with the new
checks **not** false-firing. `tests/test_store.py` and `tests/test_projects_acceptance.py`
cover the projects layer's acceptance criteria — **restart survival, input preservation across
a workflow change, per-project isolation, 404 on unknown ids, 409 on concurrent updates, and
migration/version handling**. `tests/test_edit.py` covers the inline-edit endpoint: a valid
edit sets `user_input` provenance and re-runs the gate (flipping `blocked → ok` and
`ok → blocked`), resolution **by either positional or stable id**, the error paths
(`404`/`409`/`422`), idempotency, and durable `ProtocolVersion` persistence with
`source_op="edit"`.

The **clarification / PDF / locks / recovery** controls have their own no-network suites.
`tests/test_clarify_modes.py` covers the host `outcome_critical` heuristic and the
`_normalize_answer` invariant that **an empty field is never a default** (across every legacy
skip/value shape); `tests/test_phase1_critical_stamp.py` (fake LLM) confirms both the paper and
`choose_assay` phase-1 paths stamp every gap and honor the model's own flag;
`tests/test_continue_modes.py` proves `continue_with_answers` emits the correct per-mode host
directive (`default`/`unresolved`/`answered`), appends **no** directive on the empty-answers
fast path, and carries `mode` onto `session.decisions`; `tests/test_resolve_modes.py` asserts
the served three-choice/preflight/gate markup and that `/api/resolve` accepts `mode` (and a
legacy no-`mode` answer). `tests/test_extract_pdf.py` (monkeypatched extractor/OCR seam) covers
the report fields, truncation, image-only + unparseable-but-valid-magic, the `400`/`422` guards,
the OCR seam raising cleanly and wiring on without endpoint edits, corrected text flowing to
`/api/analyze`, and direct-PDF back-compat. `tests/test_locks.py` covers `_enforce_locks`
(restore-on-change, `409`-on-disappearance, ignore-unknown) and an end-to-end `/api/revise` that
preserves a locked value the model tried to change; `tests/test_restore.py` covers the host-only
`/restore` rollback (unrelated change persists, provenance intact, `404` on unknown id) and the
recovery invariant that a **failed op preserves the last valid protocol**. The frontend controls
are asserted structurally over the served `static/index.html`: `tests/test_locks_ui.py` (lock
toggle gated on a stable id, `localStorage` persistence, `locked_ids` on every POST),
`tests/test_deep_diff.py` (unit/provenance/citation/warning tracking, stable-id matching, and the
accessible caption + `thead`/`tbody` table), `tests/test_unrelated_confirm.py` (the pure
unrelated-vs-related classification and the revise-armed gate), `tests/test_recovery_ui.py`
(cancel/abort/retry/start-over), and `tests/test_a11y_tables.py` (every protocol table wrapped in
a scrollable `.table-wrap` region, the assertive `#sr-alert`, and a plain-language provenance
legend per tier).

## Layout

```
app/
  schemas.py      # the two tool input_schemas (request_clarifications, emit_protocol)
  models.py       # versioned Pydantic model tree + schema_version + bounded structural repair
  prompts.py      # the protocol-engineer system prompt
  agent.py        # the three-phase tool-use loop (dispatches client tools) + emit-boundary gate
  literature.py   # PubMed / preprint / protocols.io grounding tools
  resolvers.py    # DOI/PMID resolution (Crossref -> DataCite / PubMed)
  checks.py       # pure deterministic quality gate + stable ids + host assumptions log
  validation.py   # host-side citation validation + invariants + consistency + gate
  render.py       # protocol -> Markdown export
  projects.py     # projects/intake domain model + deterministic workflow classifier
  store.py        # durable SQLite projects store (versions, optimistic concurrency, migrations)
  server.py       # FastAPI endpoints (analyze/resolve/revise/export + projects) + sessions
static/index.html # paste/PDF UI, provenance render, export + refine controls
tests/            # 13 script-runnable test files across validation, grounding, agent loop,
                  # the Pydantic/repair layer, stable ids, render, HTTP, and projects
Dockerfile        # single-worker container; /healthz healthcheck
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | UI |
| `GET` | `/healthz` | liveness + config |
| `POST` | `/api/analyze` | Phase 1 (multipart: `methods_text` field or PDF `file`) |
| `POST` | `/api/resolve` | Phase 2 + 3 + validation |
| `POST` | `/api/revise` | apply a correction and re-emit |
| `POST` | `/api/design` | experiment-design review (controls, variables, readout, replication) |
| `POST` | `/api/critique` | adversarial correctness + practicality audit; `apply:true` also fixes and rebuilds in one step |
| `POST` | `/api/apply_fixes` | (granular) apply a prior review's fixes and rebuild the protocol |
| `POST` | `/api/align` | does the protocol directly test the hypothesis? |
| `GET` | `/api/protocol/{id}.md` | download the protocol as Markdown |
| `POST` | `/api/project` | universal intake: classify the workflow & create a durable project (no model call) |
| `GET` | `/api/projects` | list recent projects (most-recently-updated first) |
| `GET` | `/api/project/{id}` | restore a project (survives restart) — latest result, versions, lifecycle |
| `POST` | `/api/project/{id}/workflow` | confirm/override the workflow (preserves all inputs) |
| `GET` | `/api/project/{id}/protocol.md` | restart-safe protocol download for the project's current version |
| `GET` | `/api/project/{id}/materials.csv` | restart-safe materials CSV for the project's current version |

## Notes & next steps

- **Provider:** the loop speaks OpenAI-compatible chat-completions (`app/llm.py`), so it
  runs on OpenRouter (any model, one key) or the Claude API directly — pick with
  `LLM_PROVIDER`, switch with a one-line env change and no code edits. The only
  requirement is tool/function calling. OpenRouter-only params (`reasoning.effort`,
  attribution headers) are auto-suppressed on other providers. PDFs are read by
  extracting text host-side, so image-only PDFs must be pasted as text.
- **Grounding sources:** `search_pubmed` (NCBI), `search_preprints` (bioRxiv/medRxiv
  via Europe PMC), and `search_protocols` (protocols.io, token-gated) — all app-run, so
  grounding behaves identically across providers. Validation resolves DOIs via Crossref then DataCite
  (so protocols.io/Zenodo/data DOIs verify) and PMIDs via PubMed. Add a new source by
  mirroring a function in `literature.py` and registering it in `agent._CLIENT_TOOLS`.
- **Security:** the API key is server-side only (never sent to the browser, never
  logged; `/healthz` reports only a boolean). Before any public/multi-user deployment,
  set `GAPFILLER_AUTH_TOKEN` + `GAPFILLER_RATE_LIMIT` (both off by default) and cap the
  OpenRouter key's spend. 500s return a generic message; full detail is logged
  server-side. The source paper is untrusted model input — prompt injection is bounded
  by the host-side citation/quote verification, which can't be talked past.
- **Cost & efficiency:** a run is a multi-phase loop (analyze → ground → emit, plus any
  refine/design follow-up), so the stable prefix — system prompt + source text — would be
  re-billed on every round-trip. Several defaults keep both tokens and latency down without
  touching output quality: **prompt caching** (`GAPFILLER_PROMPT_CACHE`) caches that prefix
  so phases 2…N read it cheap; the system prompt is **scoped per phase** (the asking phase
  omits the emit rules and vice-versa); full-paper PDFs are **trimmed host-side to the Methods
  section**; bulky grounding results are **compacted out of the transcript** for follow-ups;
  multiple grounding searches in one turn run **concurrently**; when no grounding tool applies
  the terminal is **forced on the first call** (no wasted auto→text→nudge round-trip); and the
  HTTP client uses **HTTP/2 + a keep-alive pool**. The **model tier split is built in**: the
  light phases (clarifications, assay discovery) run on the provider's fast model (Sonnet) while
  the heavy emit + reviews + fixes keep the strong one (Opus) — spending the top tier only where
  the reasoning earns it. Override either side with `LLM_MODEL` / `LLM_MODEL_FAST`.
- **Sessions vs projects:** live *sessions* (the in-flight run engine) are in-memory and
  single-process, but *projects* — intake, detected workflow, and every emitted protocol
  version — are **durable in SQLite** (`GAPFILLER_DB_PATH`) and survive restarts, with
  optimistic-concurrency writes and fail-closed schema migrations. A restored project whose
  session has since expired re-renders from its stored latest version.
- **Latency:** Phase 2 can run for a minute or two while it searches, drafts, audits and
  fixes. The UI shows a **live activity feed** — the server records each stage (searching a
  source, drafting, auditing, applying N fixes) to a per-session buffer that the browser
  polls at `GET /api/progress/{session_id}` alongside the in-flight request (sync endpoints
  run in Starlette's threadpool, so the poll isn't blocked by the build). Token-level
  streaming of the emit is a further enhancement.
