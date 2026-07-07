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
| **1 — read, scope & ask** | Model reconstructs the protocol, classifies each parameter, runs a light scoping search for ambiguous gaps, and calls `request_clarifications` with 0–5 targeted questions (menu options carry their source + citation). |
| **2 — research & ground** | Your answers come back as the tool result; the model grounds outcome-critical values with `web_search` (capped at 12 searches) and calls `emit_protocol`. |
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

Open <http://localhost:8000>, paste a Methods section (or click **Load example
(CFPS)**), answer the questions, and read the protocol.

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Required. |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | The model that runs the loop. |
| `GAPFILLER_SEARCH_BUDGET` | `12` | Max Phase-2 web searches. |
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
  agent.py        # the three-phase tool-use loop
  resolvers.py    # DOI/PMID resolution (Crossref / PubMed)
  validation.py   # host-side citation validation + invariants + consistency
  server.py       # FastAPI endpoints + session store
static/index.html # paste-text UI + provenance-tagged render
tests/            # validation unit tests
```

## Notes & next steps

- **Bio connectors:** grounding uses the built-in `web_search`; validation uses
  PubMed/Crossref. Wiring PubMed/bioRxiv as the *grounding* source too (via MCP) is the
  natural upgrade — `GAPFILLER_WEB_SEARCH_TYPE` and `resolvers.py` are the seams.
- **Sessions** are in-memory (single process) — fine for a demo, swap for a store to scale.
- **Latency:** Phase 2 can run for a minute or two while it searches; the UI shows a
  working state. Streaming the emit call is a reasonable enhancement.
