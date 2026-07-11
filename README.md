# Methods Gap-Filler

Paste a paper's **Methods** section. The tool reconstructs the protocol, classifies
every parameter by how well the source specifies it, **asks you only about the gaps
that actually matter**, searches the literature to ground the values it fills, then
emits an executable, protocols.io-style protocol where **every value is
provenance-tagged and every citation is verified against a real database.**

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
DOI/PMID** against PubMed (Crossref for DOIs) and downgrades anything that doesn't
resolve to `default_verify`. Verified citations get a ✓ badge; the rest are flagged.

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

### Deploy (Docker)

```bash
docker build -t methods-gap-filler .
docker run -p 8000:8000 -e OPENROUTER_API_KEY=sk-or-... methods-gap-filler
```

Sessions are in memory, so run **one worker** (the image does). `GET /healthz` reports
liveness, the model, and which grounding sources are enabled. Sessions expire after
`GAPFILLER_SESSION_TTL` seconds (default 3600).

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `LLM_PROVIDER` | `openrouter` | `openrouter` (any model, one key) or `anthropic` (Claude API direct, `sk-ant-…`). Sets the base URL + provider-specific params. |
| `LLM_API_KEY` | — | Required. Your key for the chosen provider. (Alias: `OPENROUTER_API_KEY`.) |
| `LLM_MODEL` | provider default | Model id/slug — must support tool calling. Defaults: `anthropic/claude-opus-4-8` (openrouter) · `claude-opus-4-8` (anthropic). (Alias: `OPENROUTER_MODEL`.) |
| `LLM_BASE_URL` | provider default | Override the endpoint (e.g. a self-hosted compatible gateway). (Alias: `OPENROUTER_BASE_URL`.) |
| `GAPFILLER_REASONING_EFFORT` | `high` | Passed as `reasoning.effort` to models that support it; `""` to omit. |
| `GAPFILLER_SEND_REASONING` | on for `openrouter` | Whether to send the OpenRouter-only `reasoning` field; auto-off for other providers. Set `1`/`0` to force. |
| `GAPFILLER_MAX_TOKENS` | `16000` | Output token ceiling. |
| `GAPFILLER_REQUEST_TIMEOUT` | `600` | Per-request HTTP timeout (seconds). |
| `GAPFILLER_PUBMED_BUDGET` | `12` | Max literature searches per phase. |
| `GAPFILLER_ENABLE_PUBMED` | `1` | Set `0` to disable the PubMed grounding tool. |
| `GAPFILLER_ENABLE_PREPRINTS` | `1` | Set `0` to disable bioRxiv/medRxiv (Europe PMC) search. |
| `NCBI_API_KEY` | — | Optional; raises the E-utilities rate limit (3→10 req/s). |
| `PROTOCOLS_IO_TOKEN` | — | protocols.io developer token; when set, enables `search_protocols`. |
| `GAPFILLER_AUTH_TOKEN` | — | If set, `/api/*` requires it (`Authorization: Bearer` or `X-API-Key`). Off by default. |
| `GAPFILLER_RATE_LIMIT` | `0` | Per-client requests/minute on the model-driving endpoints; `0` disables. |
| `GAPFILLER_TRUST_PROXY` | `0` | Set `1` to read the client IP from `X-Forwarded-For` (only behind a proxy you control). |

## Tests

```bash
python tests/test_validation.py
```

The validation suite (citation resolution, provenance invariants, `assumptions_log`
consistency) runs without the network — the bibliographic resolver is injected.

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
  server.py       # FastAPI endpoints (analyze/resolve/revise/export) + sessions
static/index.html # paste/PDF UI, provenance render, export + refine controls
tests/            # 94 tests across validation, grounding, agent loop, render, HTTP
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
| `GET` | `/api/protocol/{id}.md` | download the protocol as Markdown |

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
- **Sessions** are in-memory (single process) — fine for a demo, swap for a store to scale.
- **Latency:** Phase 2 can run for a minute or two while it searches; the UI shows a
  working state. Streaming the emit call is a reasonable enhancement.
