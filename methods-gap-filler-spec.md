# Methods Gap-Filler — Build Spec

Hackathon tool: ingest a paper's Methods section, reconstruct the protocol,
classify every parameter by how well the source specifies it, ask the user only
about the gaps that actually matter, **search the literature to ground the values
it fills**, **validate every citation it produces against a real bibliographic
source**, then emit an executable protocols.io-style protocol in which every
value is provenance-tagged.

The reproducibility story — and the demo's credibility with a scientific judge —
lives in three rules: **the tool never silently invents a parameter**, **when it
fills a gap it grounds the value in a real source or names the best-practice basis**,
and **every `literature_grounded` citation is resolved against a real database before
it ships**. Every number, reagent, and condition carries a provenance tag drawn from
five tiers: stated in the source, literature-grounded (backed by a citation the tool
found *and verified*), best-practice (a named standard, no specific citation),
user-supplied, or a flagged default to verify. The distinction between
literature-grounded and best-practice is deliberate — they are not the same epistemic
claim, and a scientific judge will care which one you're making. Backing an inferred
value with a citation the agent actually retrieved *and could resolve* is what
separates this from a plausible-sounding guess, and it is the agentic tool-use that a
"Built with Claude" panel is looking for.

---

## Architecture (three phases)

The interaction is a tool-use loop with a literature-search step in the middle.

**A note on forced tool choice — read this before wiring the loop.** Forcing
`tool_choice` to a named tool (`{"type": "tool", "name": "..."}`) means the model's
*next* output must be exactly that tool call. It therefore **cannot** call a search
tool first in the same request. You cannot both "run a scoping search" and "force
`request_clarifications`" in one API call — the two are mutually exclusive. The
pattern below reflects this: each phase runs an ordinary agentic loop with
`tool_choice: "auto"` and *all* tools available (the search tools **and** the phase's
structured tool); the model searches as many rounds as it needs and then emits the
structured tool, which is the terminal signal for that phase. If you want a hard
termination guarantee, make a final follow-up call with the search tools removed from
`tools` and `tool_choice` forced to the structured tool — but do that only after the
model has signalled it is done searching, never as the call that is supposed to search.

1. **Phase 1 — read, scope & ask.** Call the model with the system prompt + the
   pasted Methods text (or a PDF document block), with the literature-search tool(s)
   **and** `request_clarifications` all available and `tool_choice: "auto"`. The model
   reconstructs and classifies internally; for ambiguous gaps where the literature
   offers a discrete menu of established alternatives, it runs a *lightweight scoping
   search* to enumerate the real options, then emits `request_clarifications` with
   those options pre-populated. The model's emission of `request_clarifications` is the
   terminal signal for Phase 1 — do not pre-force it.
   - **Branch — not a methods section:** if the input is an abstract, a caption, or
     otherwise not a protocol, the model emits `request_clarifications` with
     `usable: false` and a reason, and the loop stops. Guard against confidently
     hallucinating a protocol from junk input.
   - **Branch — no gaps:** if `gaps` is empty, skip Phase 1's user prompt and go
     straight to Phase 2. Do not present an empty question set.
2. **Present** questions to the user. Ambiguous-gap questions render as selectable
   options, each showing its source and (where literature-derived) its citation, plus
   a one-line "pick this if…". Always append an "Other / not sure" escape — the menu
   is common approaches, never an exhaustive list. User-dependent questions render as
   plain choices or free text. Each carries a fallback the user can accept by skipping.
3. **Collect answers (host contract).** The host maps each user response back to its
   gap by `id` and appends a **new user turn** whose content is a single fenced JSON
   block of the form:
   ```json
   {"answers": [
     {"id": "gap_1", "value": "50", "skipped": false},
     {"id": "gap_2", "value": null, "skipped": true}
   ]}
   ```
   A skipped answer (`skipped: true`, `value: null`) means "accept the fallback"; the
   model must then treat that gap's `suggested_default` as a `default_verify` value.
   Do not rely on free-text association — the `id` map is the contract.
4. **Phase 2 — research & ground.** Append the answers turn and call the model with the
   literature-search tool(s) available — `web_search`, or the PubMed / bioRxiv /
   Scholar Gateway connectors — in a normal agentic tool-use loop with
   `tool_choice: "auto"` (let it call, read results, call again as needed). It searches
   to ground the remaining values it intends to fill, especially outcome-critical ones.
   Options the user picked from a literature-derived menu already carry their citation
   and need no re-grounding. This is the phase that earns the "literature-informed"
   claim. **Bound the loop:** cap Phase 2 at a fixed search budget (default: 12 search
   calls). If the budget is exhausted before a value is grounded, fill it as
   `best_practice` (if a named convention applies) or `default_verify`, never as an
   ungrounded `literature_grounded`.
5. **Phase 3 — emit.** Once research is done, make a final call with the search tools
   removed and `tool_choice` forced to `emit_protocol`. Every filled value carries its
   provenance tier and, where applicable, the citation the model retrieved.
6. **Citation validation (host, after emit).** Before rendering, resolve every
   `citation.identifier` (DOI or PMID) against a real bibliographic source — the PubMed
   connector's `lookup_article_by_citation` / `get_article_metadata`, or a DOI resolver.
   For each `literature_grounded` value: if the identifier resolves and the
   title/authors/year match, mark the citation **verified**; if it does not resolve, or
   the metadata mismatches, **downgrade** the value to `default_verify` and record the
   failure in `open_questions`. This step is what converts "trust the model" into "the
   tool checked," and it is the strongest demo beat with a scientific judge.

The two structured schemas below are used as tool `input_schema` definitions with
forced tool choice **on the final call of each phase only** — that is the robust way
to get structured JSON from the API. Do **not** rely on "respond only in JSON" prose,
and do **not** force a structured tool on a call that is also supposed to search
(see the note above). Search tools are offered normally in both Phase 1 (scoping) and
Phase 2 (grounding).

---

## System prompt

```
You are a scientific protocol engineer. Your job is to turn an underspecified
published Methods section into a complete, executable, bench-ready protocol —
while making every hidden assumption explicit rather than silently filling it in.

Published methods routinely omit the details that determine whether an experiment
actually works: reaction volumes, reagent grades and concentrations, incubation
times and temperatures, equipment specifics, and controls. Your value is not
summarizing what is written — it is reasoning rigorously about what is MISSING and
handling each gap with appropriate epistemic honesty.

## Core discipline (non-negotiable)

Never present an invented or assumed parameter as if it came from the source. Every
value in the final protocol carries one of five provenance tags:
- "stated"             — explicitly given in the source text.
- "literature_grounded"— not in the source, but you filled it and backed it with a
                         specific citation you RETRIEVED via search this session.
                         Record the citation (title, authors, year, and a DOI or
                         PMID identifier). The host will independently resolve that
                         identifier before the protocol ships; a citation that does
                         not resolve will be downgraded, so only cite sources you
                         actually retrieved.
- "best_practice"      — not in the source; filled from a widely-accepted standard
                         for this method that you did not tie to a single citation.
                         Name the standard or convention as the basis.
- "user_input"         — supplied by the user in answer to a clarifying question, and
                         NOT itself backed by a specific citation. (A value the user
                         chose from a literature-derived menu is "literature_grounded"
                         with selected_by_user=true — see below.)
- "default_verify"     — not stated and genuinely uncertain; you chose a reasonable
                         default that the user MUST verify before use. Flag it.

Do not claim "literature_grounded" unless you actually retrieved a supporting source
this session AND recorded a resolvable DOI/PMID — an unretrieved or unresolvable
citation is a fabrication and is worse than an honest "best_practice" or
"default_verify". When in doubt about confidence, drop a tier toward default_verify.
It is always better to flag for verification than to overstate.

## Provenance of user-selected literature options (resolve this one way)

When the user picks an option from an ambiguous-gap menu, the tag depends on where the
value's AUTHORITY comes from, not on who clicked:
- The user picked a "literature" option (it carried a citation) -> tag the value
  "literature_grounded", keep its citation, and set selected_by_user=true. The
  literature is what grounds the value; the user only located themselves in the
  decision space.
- The user picked a "standard_practice" option -> "best_practice", selected_by_user=true.
- The user picked "model_suggestion" or "Other / not sure", or typed free text ->
  "user_input" (or "default_verify" if you are filling a fallback they skipped).
Never tag the same value with two provenance types. selected_by_user is a separate
boolean, not a provenance tier.

## Input guard (check first)

Before anything else, confirm the input is actually an experimental methods/protocol
section. If it is an abstract, a figure caption, results prose, or unrelated text,
return usable=false with a brief reason and stop. Never fabricate a protocol from
non-protocol input.

## Gap classification (do this internally before asking anything)

Reconstruct the protocol as an ordered sequence of steps. For every parameter each
step depends on, classify it into exactly one bucket:

1. STATED            — present in the source. No action.
2. FILLABLE          — not stated, but resolvable without the user, either by
                       searching the literature (-> literature_grounded) or from an
                       unambiguous standard (-> best_practice). Do NOT ask the user;
                       resolve it in the research phase.
3. USER_DEPENDENT    — cannot be known from any paper because it depends on the
                       user's own setup, scale, or goal (e.g. reaction volume,
                       number of replicates, target yield). ASK.
4. AMBIGUOUS         — genuinely underdetermined and outcome-critical, with no
                       defensible default (e.g. a buffer whose identity changes the
                       result). ASK.
5. DEFERRED          — the source points to an external protocol ("extract prepared
                       as described previously", a cited buffer recipe). Not
                       ambiguous — it is elsewhere. Record the pointer/citation so
                       the user can chase it; search for it if the reference is
                       identifiable.

## Asking discipline (phase 1)

Ask ONLY about USER_DEPENDENT and AMBIGUOUS parameters. Never ask about anything you
can resolve yourself. Rank questions by how much the answer cascades into the rest of
the protocol, and return at most 5 (fewer is better).

Two kinds of question, handled differently:

- USER_DEPENDENT: the answer lives only in the user's head (scale, replicates, goal).
  Offer plain discrete choices where the space is discrete (e.g. scale: uL / mL / L)
  or free text/number otherwise. No search needed. Use multi_select ONLY when the
  answer genuinely can be several values at once (e.g. "which controls will you run?");
  default to single_select otherwise.

- AMBIGUOUS: the source is silent but the FIELD offers a discrete menu of established
  alternatives, and which one fits depends on the user's system or goal. Before
  asking, run a lightweight scoping search to enumerate the real options actually
  reported for this method. Present each as a selectable option carrying: its source
  (literature / standard_practice / model_suggestion), a citation when it came from
  the literature, and a one-line "pick this if…". Set option_context to a short phrase
  explaining why these are the choices. This turns the question from "guess what I
  mean" into "locate yourself in the real decision space."

For every question, always include an "Other / not sure" escape, and treat option
menus as common approaches, NOT exhaustive lists — never imply the menu is complete.
Provide a sensible fallback the user can accept by skipping; that fallback becomes a
"default_verify" value. Keep the scoping search disciplined: only for ambiguous gaps
with a plausibly discrete literature menu, never for user-dependent bins.

Reason about parameter cascades: choices like reaction volume propagate into every
downstream reagent amount, so surfacing them early is high-value. When the user picks
an option that carried a citation, that citation travels with the value into the final
protocol as literature_grounded + selected_by_user=true.

## Research & grounding discipline (phase 2)

After you have the user's answers, use the search tools available to you to ground
the values you will fill. Prioritize outcome-critical parameters and anything where a
wrong value would ruin the experiment; do not burn searches on trivia, and stay within
the search budget the host has set (it will stop offering search tools once it is
exhausted). For each value you ground this way, capture a real citation (title,
authors, year, and a DOI or PMID identifier) and tag it "literature_grounded". If a
search does not turn up specific support but the value is fixed by well-established
practice, fill it and tag "best_practice", naming the convention. If neither holds,
use "default_verify". Never invent a citation you did not retrieve.

Conflict between the user and the literature: if the user answered a gap and your
research strongly contradicts their choice on an outcome-critical parameter, the
user's choice WINS in the emitted protocol (it is their experiment), but you MUST
surface the tension in open_questions with the conflicting evidence and its citation.
Do not silently overwrite a user answer with a literature value.

For DEFERRED parameters, search for the referenced external protocol when the
reference is identifiable, and either fold in what you find (grounded) or record the
pointer for the user to chase.

## Emitting discipline (phase 3)

Produce a complete protocols.io-style protocol: ordered numbered steps with
sub-steps, a materials list with amounts and units, equipment, timing, temperatures,
critical parameters, and warnings. Then produce a consolidated assumptions_log
listing every value that was NOT stated in the source, with its provenance, basis,
and citation where applicable — this is the reproducibility payoff and must be
exhaustive.

The assumptions_log is a PROJECTION of the inline provenance, not a second opinion.
Every non-stated value you put inline (in materials, steps, or critical_parameters)
must appear in the assumptions_log with the SAME value, the SAME provenance tier, and
the SAME citation. Do not let the two representations drift. List anything still
unresolved under open_questions.

Write instructions in the imperative, at the level of detail a competent researcher
new to this exact protocol could follow. Never merge two provenance types into one
value; split a step if parts have different provenance.

When you scale reagent amounts to a user-supplied reaction volume, show the scaling
arithmetic in the value's provenance note (e.g. "12 mM stock -> 2 mM final in 50 uL =
8.3 uL"). Do not silently emit a scaled number — exposing the math makes any error
visible and preserves the tool's honesty.

## Domain grounding

You are producing wet-lab molecular biology / biochemistry protocols. Cell-free
protein synthesis (CFPS) methods are a frequent and especially gap-prone input:
extract source and prep, energy-regeneration system, Mg2+/K+ optima, additive
concentrations (e.g. chaperones or oxidative-folding components), and reaction
scale are commonly omitted. Treat such omissions as first-class gaps.

## Safety

If a described protocol concerns a select agent, a controlled toxin, or otherwise
would provide meaningful uplift toward causing harm, do not produce an executable
protocol. State the concern plainly and stop. (This is a single model-judgment gate,
not a validated screen — it is a floor, not a guarantee.)

## Worked example (classification calibration)

Source excerpt: "CFPS reactions were assembled with S30 extract and a PANOx-SP
energy system and incubated at 30 C. Disulfide-bonded product was obtained."

- Reaction volume -> USER_DEPENDENT (depends on the user's scale). ASK.
- Incubation time -> FILLABLE via literature (PANOx-SP CFPS incubation times are
  reported; search and ground). Do NOT ask.
- Mg2+ / K+ concentration -> FILLABLE (PANOx-SP has characteristic optima) but
  outcome-critical, so ground with a citation if possible, else best_practice.
- "Disulfide-bonded" with no folding aid named -> AMBIGUOUS. Scope-search first, then
  ask with populated options, e.g.: CyDisCo co-expression [literature, cite] · DsbC
  supplementation [literature, cite] · oxidized/reduced glutathione buffer [literature,
  cite] · Other / not sure. option_context: "oxidative-folding strategies reported for
  CFPS." Whichever the user picks carries its citation forward as literature_grounded
  + selected_by_user=true.
- "S30 extract ... as described" if the text defers to a prior prep -> DEFERRED.
  Record the pointer; search for the referenced prep if identifiable.

The rule the example encodes: ask only what no source can answer; everything else is
resolved by search or standard practice, not by interrogating the user.

## Output contract

Always respond by calling the tool you are given — never free text. In phase 1 call
request_clarifications (or return usable=false if the input is not a protocol). In
phase 2 you may call the search tools freely; when your research is complete, the host
will ask you to call emit_protocol. Do not attempt to search on a call whose only
available tool is a structured schema tool.
```

---

## Schema 1 — `request_clarifications` (phase 1 output)

The `citation` object is defined once under `$defs` and referenced wherever a value
can be literature-grounded, so its shape is part of the machine-readable schema (not
just prose). A `literature` option MUST carry a non-null citation; the host validates
this after the call.

```json
{
  "name": "request_clarifications",
  "description": "Return the reconstructed protocol summary and the ranked set of gaps that require a human answer.",
  "input_schema": {
    "type": "object",
    "$defs": {
      "citation": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "title": { "type": "string" },
          "authors": { "type": "string" },
          "year": { "type": "integer" },
          "identifier": { "type": "string", "description": "DOI or PMID." },
          "url": { "type": ["string", "null"] }
        },
        "required": ["title", "authors", "year", "identifier"]
      }
    },
    "properties": {
      "usable": {
        "type": "boolean",
        "description": "False if the input is not an experimental methods/protocol section."
      },
      "reason": {
        "type": ["string", "null"],
        "description": "If usable is false, a brief explanation. Otherwise null."
      },
      "protocol_title": { "type": ["string", "null"] },
      "source_summary": {
        "type": ["string", "null"],
        "description": "One or two sentences on what the protocol accomplishes."
      },
      "reconstructed_step_count": { "type": ["integer", "null"] },
      "gaps": {
        "type": "array",
        "maxItems": 5,
        "description": "Empty array is valid and means no user input is needed.",
        "items": {
          "type": "object",
          "properties": {
            "id": {
              "type": "string",
              "description": "Stable identifier used to map the user's answer back in phase 2 (see the answers host contract)."
            },
            "parameter": { "type": "string" },
            "step_reference": {
              "type": "string",
              "description": "Which reconstructed step this affects."
            },
            "why_it_matters": {
              "type": "string",
              "description": "The downstream effect of this parameter."
            },
            "classification": {
              "type": "string",
              "enum": ["user_dependent", "ambiguous", "deferred"]
            },
            "question": { "type": "string" },
            "answer_type": {
              "type": "string",
              "enum": ["single_select", "multi_select", "number", "text"]
            },
            "option_context": {
              "type": ["string", "null"],
              "description": "For ambiguous gaps: short phrase explaining why these options were surfaced (e.g. 'oxidative-folding strategies reported for CFPS')."
            },
            "options": {
              "type": "array",
              "description": "Present for select answer_types. For ambiguous gaps, these are literature-scoped alternatives. The UI must always add an 'Other / not sure' escape; do not include it here.",
              "items": {
                "type": "object",
                "properties": {
                  "label": { "type": "string" },
                  "value": { "type": "string" },
                  "source": {
                    "type": "string",
                    "enum": ["literature", "standard_practice", "model_suggestion"]
                  },
                  "note": {
                    "type": ["string", "null"],
                    "description": "One-line 'pick this if…' guidance."
                  },
                  "citation": {
                    "anyOf": [{ "$ref": "#/$defs/citation" }, { "type": "null" }],
                    "description": "Required (non-null) when source is 'literature'; null otherwise. If picked, this travels with the value into the protocol as literature_grounded + selected_by_user=true."
                  }
                },
                "required": ["label", "value", "source"]
              }
            },
            "unit": {
              "type": ["string", "null"],
              "description": "Expected unit for number answers (e.g. 'uL', 'mM')."
            },
            "suggested_default": {
              "type": ["string", "null"],
              "description": "Fallback value if the user skips; becomes a default_verify entry."
            }
          },
          "required": ["id", "parameter", "step_reference", "why_it_matters",
                       "classification", "question", "answer_type"]
        }
      }
    },
    "required": ["usable", "gaps"]
  }
}
```

When `usable` is false, return only `usable` and `reason`; the loop stops. When
`gaps` is empty, the loop advances straight to the research phase. The host validates
that every `options` entry with `source: "literature"` carries a non-null `citation`
before presenting the questions.

---

## Schema 2 — `emit_protocol` (phase 3 output)

Every provenance field uses the same five-tier enum:
`"stated" | "literature_grounded" | "best_practice" | "user_input" | "default_verify"`.

The `citation` object is defined once under `$defs` and `$ref`-ed wherever a value can
be literature-grounded. It is required (non-null) when provenance is
`"literature_grounded"`, may also be present on a value the user picked from a
literature-derived option menu (in which case that value is `literature_grounded` with
`selected_by_user: true`), and is null otherwise. The host resolves every non-null
`identifier` after emit and downgrades any value whose citation does not resolve.

```json
{
  "name": "emit_protocol",
  "description": "Emit the complete, executable, provenance-tagged protocol.",
  "input_schema": {
    "type": "object",
    "$defs": {
      "citation": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "title": { "type": "string" },
          "authors": { "type": "string" },
          "year": { "type": "integer" },
          "identifier": { "type": "string", "description": "DOI or PMID." },
          "url": { "type": ["string", "null"] }
        },
        "required": ["title", "authors", "year", "identifier"]
      },
      "provenance": {
        "type": "string",
        "enum": ["stated", "literature_grounded", "best_practice", "user_input", "default_verify"]
      }
    },
    "properties": {
      "title": { "type": "string" },
      "summary": { "type": "string" },
      "source_citation": { "type": ["string", "null"] },
      "estimated_duration": { "type": "string" },
      "materials": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "name": { "type": "string" },
            "amount": { "type": ["number", "null"] },
            "unit": { "type": ["string", "null"] },
            "vendor_or_grade": { "type": ["string", "null"] },
            "provenance": { "$ref": "#/$defs/provenance" },
            "selected_by_user": {
              "type": "boolean",
              "description": "True when the user chose this value from an option menu. Independent of provenance."
            },
            "provenance_note": {
              "type": ["string", "null"],
              "description": "Basis/standard named; scaling math if scaled; verify note for defaults."
            },
            "citation": {
              "anyOf": [{ "$ref": "#/$defs/citation" }, { "type": "null" }],
              "description": "Required (non-null) when provenance is literature_grounded; null otherwise."
            }
          },
          "required": ["name", "provenance"]
        }
      },
      "equipment": { "type": "array", "items": { "type": "string" } },
      "steps": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "number": { "type": "integer" },
            "title": { "type": "string" },
            "instruction": { "type": "string" },
            "duration": { "type": ["string", "null"] },
            "temperature": { "type": ["string", "null"] },
            "provenance": { "$ref": "#/$defs/provenance" },
            "critical_parameters": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "name": { "type": "string" },
                  "value": { "type": "string" },
                  "unit": { "type": ["string", "null"] },
                  "provenance": { "$ref": "#/$defs/provenance" },
                  "selected_by_user": {
                    "type": "boolean",
                    "description": "True when the user chose this value from an option menu."
                  },
                  "provenance_note": { "type": ["string", "null"] },
                  "citation": {
                    "anyOf": [{ "$ref": "#/$defs/citation" }, { "type": "null" }],
                    "description": "Required (non-null) when provenance is literature_grounded; null otherwise."
                  }
                },
                "required": ["name", "value", "provenance"]
              }
            },
            "substeps": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "number": { "type": "string" },
                  "instruction": { "type": "string" },
                  "provenance": { "$ref": "#/$defs/provenance" },
                  "provenance_note": { "type": ["string", "null"] }
                },
                "required": ["number", "instruction", "provenance"]
              }
            },
            "warnings": { "type": "array", "items": { "type": "string" } }
          },
          "required": ["number", "title", "instruction", "provenance"]
        }
      },
      "assumptions_log": {
        "type": "array",
        "description": "Every value NOT stated in the source. A projection of the inline provenance: each entry must match an inline value's tier, value, and citation exactly. Must be exhaustive.",
        "items": {
          "type": "object",
          "properties": {
            "parameter": { "type": "string" },
            "value": { "type": "string" },
            "provenance": {
              "type": "string",
              "enum": ["literature_grounded", "best_practice", "user_input", "default_verify"]
            },
            "selected_by_user": {
              "type": "boolean",
              "description": "True when the user chose this value from an option menu."
            },
            "basis": {
              "type": "string",
              "description": "The citation summary, the named standard, or how the value was derived."
            },
            "citation": {
              "anyOf": [{ "$ref": "#/$defs/citation" }, { "type": "null" }],
              "description": "Required (non-null) when provenance is literature_grounded; null otherwise."
            },
            "verify": {
              "type": "boolean",
              "description": "True if the user should confirm before running."
            }
          },
          "required": ["parameter", "value", "provenance", "basis", "verify"]
        }
      },
      "open_questions": {
        "type": "array",
        "description": "Gaps that remain unresolved even after user input, plus any user/literature conflicts (with the conflicting evidence) and any citation that failed host validation.",
        "items": { "type": "string" }
      }
    },
    "required": ["title", "summary", "estimated_duration", "materials",
                 "steps", "assumptions_log"]
  }
}
```

---

## Host-side validation (runs after `emit_protocol`, before rendering)

These checks are the difference between "the model says it grounded this" and "the
tool verified it." Run all of them and reflect failures in the rendered output.

1. **Citation resolution.** For every value tagged `literature_grounded`, resolve
   `citation.identifier` (DOI or PMID) against a real source — PubMed
   (`lookup_article_by_citation` / `get_article_metadata`), a DOI resolver, or the
   Scholar Gateway connector. Confirm the returned title/authors/year match. On a
   resolve failure or metadata mismatch, **downgrade** the value to `default_verify`,
   null its citation, and append a note to `open_questions`. Render a "verified" badge
   only on citations that passed.
2. **Provenance/citation invariant.** Reject (or repair by downgrading to
   `default_verify`) any `literature_grounded` value whose `citation` is null, and any
   non-`literature_grounded` value that carries a non-null citation *unless*
   `selected_by_user` is true (a user-picked literature option legitimately carries a
   citation).
3. **assumptions_log ↔ inline consistency.** Every non-stated inline value must appear
   in `assumptions_log` with the same value, tier, and citation, and vice versa. On a
   mismatch, surface it rather than silently trusting either copy. (Simplest robust
   implementation: derive the log from the inline provenance yourself and diff against
   the model's log; if they disagree, prefer the inline values and flag the discrepancy.)

---

## Notes for the build

- **Input:** paste-text for the MVP (removes the one fragile dependency). PDF is an
  easy stretch — the API reads PDFs natively via a `document` content block, so you
  can pass the source paper directly without a parsing library.
- **Search tooling:** simplest path is the built-in `web_search` tool in the Phase 2
  call. For richer, more citable grounding — and because the citation-validation step
  needs a real bibliographic API anyway — wire PubMed / bioRxiv / Scholar Gateway as
  connectors. The demo story "the agent pulled a PMID to justify this Mg2+
  concentration, and the tool then resolved that PMID to confirm the paper is real" is
  far stronger with a real bio source than a generic web hit. Start with `web_search`
  for grounding; use the bibliographic connector for validation from the start.
- **Rendering:** the `emit_protocol` JSON maps 1:1 onto a protocols.io-style layout.
  Color-code the five provenance tiers, make `literature_grounded` values link out to
  their citation, and show the "verified" badge from the validation step. The
  `assumptions_log`, rendered as a distinct panel with those verified citations
  visible, is the single most demo-legible feature.
- **Scaling:** when reagents are scaled to a user-supplied volume, render the
  arithmetic from `provenance_note` so the math is auditable. Consider a tiny
  deterministic calculator tool if you see the model fumbling the numbers.
- **Demo robustness:** the flow depends on a live search returning citable results on
  stage. Pre-select the source paper, and keep a cached/canned search + validation
  path for the demo so a flaky retrieval doesn't sink the best beat.
- **Usability (post-hackathon):** add an edit-and-regenerate turn (feed a correction
  as another user message and re-emit) and markdown/PDF export. Both are cheap and
  are what make it a tool you keep using rather than a one-shot demo.
- **Demo tip:** pick a deliberately thin CFPS methods section. The best demo beat is
  an ambiguous gap where the tool searches, comes back with three *real* literature
  options each linked to a paper, and asks the researcher to pick — then that choice
  flows into the protocol still carrying its citation, and the validation step stamps
  it "verified." The questions it asks, the menu it builds from the literature, the
  assumptions_log it produces, and the citations it resolves are the proof it read
  what wasn't on the page and then went and found it.
