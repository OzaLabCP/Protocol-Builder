# Methods Gap-Filler

Paste a paper's **Methods** section. The tool reconstructs the protocol, classifies
every parameter by how well the source specifies it, **asks you only about the gaps
that actually matter**, searches the literature to ground the values it fills, then
emits an executable, protocols.io-style protocol where **every value is
provenance-tagged and every citation is verified against a real database.**

Built on the Claude API with a three-phase agentic tool-use loop. Design rationale
lives in [`methods-gap-filler-spec.md`](./methods-gap-filler-spec.md).

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
| **1 — read, scope & ask** | Input is pasted Methods text **or an uploaded PDF** (read natively via a `document` block; the model finds the Methods section). It reconstructs the protocol, classifies each parameter, runs a light scoping search for ambiguous gaps, and calls `request_clarifications` with 0–5 targeted questions (menu options carry their source + citation). |
| **2 — research & ground** | Your answers come back as the tool result; the model grounds outcome-critical values with Anthropic's `web_search` plus app-run search tools — `search_pubmed` (NCBI), `search_preprints` (bioRxiv/medRxiv via Europe PMC), and `search_protocols` (protocols.io, if a token is set) — then calls `emit_protocol`. |
| **3 — validate** | The host resolves every citation, enforces the provenance/citation invariants, checks the `assumptions_log` against the inline values, then renders the result. |

Forced `tool_choice` is only ever used on a call that is *not* also searching — the
model runs each phase under `tool_choice: "auto"` and the structured tool call is the
terminal signal. (See the spec's Architecture note for why forcing + searching in one
call is impossible.)

## Run it

Requires Python 3.10+ and an Anthropic API key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...        # or: cp .env.example .env  &&  edit
uvicorn app.server:app --reload --port 8000
```

Open <http://localhost:8000>, then either **paste a Methods section** (or click
**Load example (CFPS)**) **or upload the paper as a PDF** — the API reads the PDF
natively and the tool locates the Methods section itself. Answer the questions, and
read the protocol.

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Required. |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | The model that runs the loop. |
| `GAPFILLER_SEARCH_BUDGET` | `12` | Max Phase-2 `web_search` uses. |
| `GAPFILLER_PUBMED_BUDGET` | `12` | Max `search_pubmed` calls per phase. |
| `GAPFILLER_ENABLE_PUBMED` | `1` | Set `0` to disable the PubMed grounding tool. |
| `GAPFILLER_ENABLE_PREPRINTS` | `1` | Set `0` to disable bioRxiv/medRxiv (Europe PMC) search. |
| `GAPFILLER_ENABLE_WEB_SEARCH` | `1` | Set `0` to disable Anthropic `web_search`. |
| `NCBI_API_KEY` | — | Optional; raises the E-utilities rate limit (3→10 req/s). |
| `PROTOCOLS_IO_TOKEN` | — | protocols.io developer token; when set, enables `search_protocols` (grounds methods/steps in published protocols). |
| `GAPFILLER_MAX_TOKENS` | `16000` | Output token ceiling. |
| `GAPFILLER_EFFORT` | `high` | Reasoning effort. |

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
  literature.py   # search_pubmed grounding tool (NCBI E-utilities)
  resolvers.py    # DOI/PMID resolution (Crossref / PubMed)
  validation.py   # host-side citation validation + invariants + consistency
  server.py       # FastAPI endpoints + session store
static/index.html # paste-text UI + provenance-tagged render
tests/            # validation unit tests
```

## Notes & next steps

- **Grounding sources:** `web_search` (Anthropic), `search_pubmed` (NCBI),
  `search_preprints` (bioRxiv/medRxiv via Europe PMC), and `search_protocols`
  (protocols.io, token-gated). Validation resolves DOIs via Crossref then DataCite
  (so protocols.io/Zenodo/data DOIs verify) and PMIDs via PubMed. Add a new source by
  mirroring a function in `literature.py` and registering it in `agent._CLIENT_TOOLS`.
- **Sessions** are in-memory (single process) — fine for a demo, swap for a store to scale.
- **Latency:** Phase 2 can run for a minute or two while it searches; the UI shows a
  working state. Streaming the emit call is a reasonable enhancement.
