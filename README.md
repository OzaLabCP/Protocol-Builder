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
Every assumption is also projected into a human-facing **assumptions log**
(`report["assumptions"]`), each entry naming the premise the check supplied and the reason it
did so — kept disjoint from `open_questions`, which carries only blocking errors. Structural
anchors (`structural-path-v1` ids) and the whole gate are recomputed from scratch on every
validation, so re-running it is idempotent — identical `quality_gate`, identical ids, and no
accumulation in `open_questions`.

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
- **`review_status`.** The host — not the model — adjudicates the result into a single
  `fix_verification` object plus a compact string mirror `review_status` ∈
  `verified_clean` (every finding `confirmed_fixed`/`not_applicable`, no new defects),
  `issues_remain` (something is still open), or `not_reviewed` (the pass couldn't run). A verifier
  failure never blocks delivery — the corrected protocol is still returned, marked `not_reviewed`.
- **It gates the project.** `review_status` feeds `ValidationSummary.status` monotonically toward
  `blocked`: an unresolved **critical/major** prior or new finding escalates the summary to
  `blocked`; a minor-only `issues_remain` escalates a `clean` summary to `warnings`;
  `verified_clean` and `not_reviewed` never loosen it. This stays a **distinct axis** from the
  deterministic Epic-2 quality gate — both feed the one status.

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
`./projects.db`. The validation suite covers citation resolution, provenance invariants,
and `assumptions_log` consistency; `tests/test_store.py` and
`tests/test_projects_acceptance.py` cover the projects layer's acceptance criteria —
**restart survival, input preservation across a workflow change, per-project isolation,
404 on unknown ids, 409 on concurrent updates, and migration/version handling**.

## Layout

```
app/
  schemas.py      # the two tool input_schemas (request_clarifications, emit_protocol)
  prompts.py      # the protocol-engineer system prompt
  agent.py        # the three-phase tool-use loop (dispatches client tools)
  literature.py   # PubMed / preprint / protocols.io grounding tools
  resolvers.py    # DOI/PMID resolution (Crossref -> DataCite / PubMed)
  validation.py   # host-side citation validation + invariants + consistency
  render.py       # protocol -> Markdown export
  projects.py     # projects/intake domain model + deterministic workflow classifier
  store.py        # durable SQLite projects store (versions, optimistic concurrency, migrations)
  server.py       # FastAPI endpoints (analyze/resolve/revise/export + projects) + sessions
static/index.html # paste/PDF UI, provenance render, export + refine controls
tests/            # 161 tests across validation, grounding, agent loop, render, HTTP, projects
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
