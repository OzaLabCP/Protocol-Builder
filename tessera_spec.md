# Tessera — Design Specification

**Tertiary-contact stabilization by discontinuous structural-motif harvesting**

Version 0.2 (provisional) · 2026-07-06 · Prepared for the Oza Lab / Anfinis Bio computational pipeline

> **Scope boundary (read first).** This document specifies **Tessera the contact-reinforcement tool**: stages S1–S7 (§4–§7), which take a fixed backbone and emit ranked, oracle-scored residue substitutions plus design-loop constraints. §7A (stability triage) and §8A (GUI) describe **adjacent surfaces of the larger design platform** that Tessera plugs into; the platform capabilities they reference — *install mode*, *scaffold*, *grafting*, disulfide stapling, metal-site and HBNet installation, the PDC payload/linker — are **specified elsewhere and are out of scope here**. They appear only to define Tessera's hand-off contracts. Where §7A/§8A imply capabilities beyond contact reinforcement, §2's non-goals govern.

> *Working name.* "Tessera" (a mosaic tile) nods to the Mosaist/TERM heritage: a target fold is decomposed into recurring tertiary tiles, and the sequence that stabilizes the whole is assembled from what nature puts in each tile. Rename freely.

---

## 1. Purpose

Given a de novo backbone (typically an RFdiffusion / RFdiffusion-AA output, before or after inverse folding), Tessera identifies the **long-range tertiary contacts** that hold the fold together, searches the predicted protein universe for **natural recurrences of each contact's local geometry**, harvests the **residue identities nature uses at that geometry**, and hands the resulting candidate residue sets to a **calibrated ΔΔG oracle** to rank which substitutions most stabilize the contact.

It is a *targeted reinforcement and diagnostic* layer, not a replacement for inverse folding. It answers a question ProteinMPNN/LigandMPNN answer only implicitly and by likelihood, not energy: *for this specific spatially-converging residue pair, what sequence do structurally-analogous natural motifs favor, and does adopting it improve predicted fold stability?*

## 2. Scope and non-goals

**In scope.** Contact detection on a fixed backbone; construction of discontinuous (multi-segment) structural-motif queries; proteome-scale motif search; redundancy-corrected residue-frequency statistics per contact; candidate residue-set enumeration; ΔΔG screening and ranking; emission of position-specific constraints that can be fed back into a redesign step.

**Explicitly not in scope.**
- **Not a sequence-design method.** Tessera does not generate a full sequence. It reinforces specific contacts; global design stays with LigandMPNN.
- **Not an energy function.** The proteome-frequency signal is a *statistical propensity*, in the same currency as dTERMen — **not** a free energy. Any ΔG number in the output comes from the external oracle stage, not from the motif search.
- **Not a backbone generator or docking tool.** Backbone is a fixed input.
- **Not a binding-affinity predictor.** The ΔG oracle scores *monomer fold* stability (see §7.2 caveats). Binder affinity remains a Boltz-2 / dedicated-affinity concern.
- **Not a grafting / feature-installation engine.** Tessera reinforces *existing* contacts by substitution. It does not add new structural elements (disulfides, metal sites, HBNet, loops). The triage front-end (§7A) may *nominate* such features and hand them off, but installing them is the platform's `install`/`scaffold` modes, specified separately.

**Relationship to the platform.** Tessera is one module in the RFdiffusion-AA → LigandMPNN → Boltz-2 program. The triage front-end (§7A) and GUI (§8A) are integration surfaces, not part of the S1–S7 core; a valid Tessera build ships S1–S7 with neither. Treat §7A/§8A as forward-looking interface definitions whose downstream consumers live outside this spec.

## 3. Core design principles

1. **Two-stage separation of propensity and energy.** Retrieval + statistics produce *candidates*; a calibrated model produces *energies*. These are never conflated. This is the single most important architectural commitment and every module boundary respects it.
2. **Discontinuous motifs are first-class.** A tertiary contact is two (or more) segments far apart in sequence but proximal in space. The search engine must query that directly, not approximate it with whole-fold similarity.
3. **License hygiene for commercial use.** Every external binary is invoked as an isolated subprocess; no GPL source is linked into or redistributed with Tessera's own code (see §9). No non-commercial-restricted tool (e.g., dTERMen/Mosaist) is in the critical path.
4. **Redundancy is corrected, not assumed away.** Proteome-scale counts are dominated by over-sampled families unless explicitly de-weighted (see §6.3).
5. **Predictions are treated as predictions.** AFDB hits carry confidence; low-pLDDT geometry is filtered before it pollutes statistics (§6.2).
6. **Every stage is inspectable and rerunnable.** Intermediate artifacts are written to disk with stable schemas so a run can be resumed, audited, or partially recomputed.
7. **Retrieval must earn its place against the oracle.** The oracle (S6) can already enumerate substitutions directly — a ThermoMPNN site-saturation scan is cheap (§10) and the epistatic-double mode can score all 20×20 pairs per contact. So the motif-harvesting half (S1–S5) is only justified if it produces *better or cheaper* recommendations than oracle-only enumeration. Two caveats sharpen this: (a) retrieval is primarily a **candidate generator and pruner** for the double-mutant combinatorial space, not an independent energy; (b) its "orthogonality" to the oracle is **partial**, because ThermoMPNN-D is ProteinMPNN-derived and the harvested propensities come from the same structure–sequence co-occurrence the oracle was trained on — they share inductive biases and will agree on the easy cases. The retrieval half therefore ships **only if the ablation in §11.1 passes**; until then, S2–S5 are provisional and oracle-only is the fallback design mode (`--no-retrieve`).

## 4. Architecture overview

```
                    ┌─────────────────────────────────────────────┐
  backbone.pdb ───► │ S1  Contact detection                        │
  (+ optional       │     Cβ–Cβ map → long-range contact list      │
   seq / pLDDT)     └───────────────┬─────────────────────────────┘
                                    │ contacts.json
                    ┌───────────────▼─────────────────────────────┐
                    │ S2  Motif-query construction                 │
                    │     per contact → discontinuous query        │
                    └───────────────┬─────────────────────────────┘
                                    │ queries/*.pdb + residue lists
                    ┌───────────────▼─────────────────────────────┐
                    │ S3  Folddisco search (subprocess)            │
                    │     query vs AFDB50 / SwissProt / PDB index  │
                    └───────────────┬─────────────────────────────┘
                                    │ hits/*.tsv
                    ┌───────────────▼─────────────────────────────┐
                    │ S4  Hit parsing + statistics                 │
                    │     pLDDT filter, redundancy weighting,      │
                    │     per-position & pairwise residue tables   │
                    └───────────────┬─────────────────────────────┘
                                    │ stats/*.parquet
                    ┌───────────────▼─────────────────────────────┐
                    │ S5  Candidate enumeration                    │
                    │     propensity-ranked residue sets per       │
                    │     contact, deduped vs current sequence     │
                    └───────────────┬─────────────────────────────┘
                                    │ candidates.json
                    ┌───────────────▼─────────────────────────────┐
                    │ S6  ΔΔG screening (oracle, subprocess/API)   │
                    │     ThermoMPNN-D primary; FoldX/Rosetta opt. │
                    └───────────────┬─────────────────────────────┘
                                    │ scored_candidates.parquet
                    ┌───────────────▼─────────────────────────────┐
                    │ S7  Ranking, reporting, constraint emission  │
                    │     report.html + ligandmpnn_bias / resfile  │
                    └─────────────────────────────────────────────┘
```

Each stage is a standalone module with a typed input/output contract; the orchestrator (`tessera run`) chains them and supports `--from`/`--to` for partial reruns.

## 5. Stage details — retrieval half

### S1 · Contact detection

**Input.** A backbone `PDB`/`mmCIF` (single chain assumed for v0.1; multi-chain flagged in §12). Optional companion sequence (if the backbone is already threaded) and per-residue pLDDT/pAE if available.

**Algorithm.**
- Compute the Cβ–Cβ distance matrix (Cα substituted for glycine).
- Define a contact as a residue pair (i, j) with `d(i,j) ≤ d_contact` (default **8.0 Å**) and sequence separation `|i − j| ≥ sep_min` (default **12**; expose a "strict tertiary" preset at 24).
- Optionally weight/rank contacts by a **buriedness** proxy (Cβ neighbor count within 10 Å) and by contact **support** (how many other contacts share either residue). Which support level to prioritize is a **selectable strategy**, not a fixed assumption — the two defensible policies point opposite ways: `reinforce-marginal` targets lightly-supported contacts on the theory that a redundantly-packed contact is already secured, while `protect-loadbearing` targets highly-connected contacts on the theory that they carry the most fold energy and are worth the strongest residues. Default is `reinforce-marginal`, but the choice is exposed and its effect on outcomes is a validation question (§11), not a settled claim.
- Cluster adjacent contacts into contact *clusters* so that a β-strand pairing or a helix–helix packing is treated as one motif with multiple residue pairs, not many independent single pairs.

**Output — `contacts.json`.**
```json
{
  "backbone": "design_042.pdb",
  "params": {"d_contact": 8.0, "sep_min": 12},
  "contacts": [
    {"id": "c001", "i": 14, "j": 63, "d_cb": 6.2,
     "buriedness_i": 9, "buriedness_j": 11, "support": 2,
     "cluster": "k1"}
  ],
  "clusters": {"k1": {"contacts": ["c001","c004"], "segments": [[12,16],[61,65]]}}
}
```

**Notes.** No external tool required; ~15 lines of NumPy over the coordinates. `biotite` or `MDAnalysis` for parsing. This deliberately does *not* use Foldseek's 3Di — 3Di encodes per-residue nearest-neighbor geometry, which is a fine fold descriptor but not a contact enumerator.

**Geometry provenance.** Contact geometry is read from the *input* backbone. Two failure modes follow. (1) An unrelaxed backbone can carry local strain that propagates into the query geometry and thus the search; offer an optional light relaxation (idealization / short minimization) before extraction, recording whether it ran, since it changes the query. (2) Backbones with **chain breaks or missing residues** must be detected here — a contact spanning a gap has undefined geometry. Gaps are flagged in `contacts.json` and such contacts are excluded from query construction rather than silently producing garbage coordinates.

### S2 · Motif-query construction

**Input.** `contacts.json` + backbone.

**Algorithm.** For each contact cluster, extract the constituent segments (a few residues flanking each partner, default ±2) as a **discontinuous query** — this is exactly the multi-segment query model Folddisco supports natively. Emit:
- a query PDB containing only the motif residues (coordinates preserved), and
- a residue-index list in Folddisco's query format.

Expose the geometric tolerances that Folddisco will use downstream (`-d` distance threshold, `-a` angle threshold) as per-query overrides, tighter for well-defined motifs and looser for flexible ones.

**Feature availability — sequenced vs. bare backbone.** Folddisco's discrimination uses residue-pair geometry *including side-chain orientation* (Cβ direction, N–Cα/Cβ torsions). A backbone that has already been inverse-folded carries the side chains needed to compute these; a **bare pre-inverse-folding backbone does not**, and idealized-Cβ placement recovers Cβ position but not χ-dependent orientation. Consequences, made explicit rather than left to §1's "before or after":
- **Preferred mode: run Tessera post-inverse-folding**, on a threaded sequence. This is also the natural fit — Tessera *improves* an existing assignment, so a sequence usually exists.
- **Bare-backbone mode is supported but degraded.** Queries fall back to backbone-only geometric features (Cα/Cβ positions, backbone torsions); the manifest records `feature_mode: backbone_only`, and every downstream artifact inherits that flag so results are never silently compared against sequenced-mode runs. Expect lower specificity and smaller N_eff.
- The chosen mode is written into `queries/<cluster>.json` and the resolved config, because it materially changes what the hits mean.

**Output.** `queries/<cluster>.pdb` and `queries/<cluster>.txt` (residue list), plus a manifest mapping cluster → query files → the specific (i, j) pairs whose residues we will later read out of the hits, and the `feature_mode` above.

### S3 · Folddisco search

**Why Folddisco and not plain Foldseek.** Plain Foldseek aligns *linear* 3Di sequences and finds globally similar folds; a tertiary contact is a *discontinuous* motif that a linear query cannot express. Folddisco is the motif-search algorithm that indexes proximal residue pairs with geometric features (including side-chain orientation and N–Cα/Cβ torsions) and supports both short and long discontinuous-segment queries at proteome scale. In the authors' own benchmark it out-performed MASTER (the dTERMen backend) on segment queries — so it is both the right query model *and* license-clean (GPLv3, invoked as a subprocess), unlike the Mosaist/dTERMen path.

**Index (one-time, per database).**
```
folddisco index -p <structures|foldcomp_db> -i <index_dir> -t <threads> [-m big] [-v]
```
Recommended target: **AFDB50** (the clustered ~53M-structure set; the clustering is itself part of the redundancy story — see §6.3), stored/queried on the IP-sensitive compute node (§10). SwissProt and a PDB index are useful smaller/higher-confidence complements. Pre-built indices are downloadable from the Folddisco distribution; for IP-sensitive work, prefer building locally so no query structures leave the controlled environment.

**Query (per motif).**
```
folddisco query -i <index_dir> -p <query.pdb> -q <query_residues.txt> \
    -d <dist_thresh> -a <angle_thresh> --top <N_prefilter> -t <threads>
```
Run with full matching for the shortlist and `--skip-match` (prefilter only) for fast, wide scans; the prefilter ranking is competitive on its own and much cheaper.

**Output.** `hits/<cluster>.tsv` — one matched motif per line, with target ID, matched residue indices, RMSD, and Folddisco's coverage/rarity score.

## 6. Stage details — statistics half

### S4 · Hit parsing and residue statistics

This is where retrieval becomes a residue distribution, and where the two most dangerous biases are handled.

**S4.1 Read-out.** For each hit, map the matched target residues back to the query's (i, j) pairs and record the target's amino acids at those positions. This yields, per contact, a table of observed residue *pairs* (not just marginals) — pairwise is essential because the whole point is a contact, and marginal frequencies discard the covariation that carries the stabilization signal.

**S4.2 Confidence filter.** AFDB hits are predictions. Discard matched positions whose pLDDT is below a threshold (default **70**) and hits whose motif-region mean pLDDT is low; predicted geometry in low-confidence regions produces garbage 3D features and therefore spurious matches. PDB/SwissProt hits are exempt.

**S4.3 Redundancy correction.** Proteome-scale counts are dominated by over-sampled families; a naive frequency reports "what got sequenced a lot," not "what is structurally favored." Mitigations, applied in combination:
- Query **AFDB50** rather than the full AFDB (cluster representatives, not every member).
- Cluster hits by target sequence/structure identity (mmseqs2 / a Foldseek-cluster pass) and assign each cluster a total weight of 1, distributed across members — i.e., sequence-weighting analogous to MSA reweighting.
- Optionally fold Folddisco's rarity score into the weight so that structurally common (less informative) matches are down-weighted.

**S4.4 Statistics.** From weighted pair counts, compute per contact:
- a weighted pairwise residue-pair frequency table (20×20, with an effective-count `N_eff`). **Concrete definitions (an agent must not invent these):** `N_eff` = the sum of redundancy-corrected hit weights after §6.3 collapse — i.e., roughly the number of *independent* sequence clusters contributing, not the raw hit count. Frequencies use **Dirichlet pseudocounts** (default α = 0.5 per cell, background-weighted) so that low-`N_eff` cells do not produce infinite or wild log-odds; the pseudocount total is folded into `N_eff` reporting so the smoothing is visible.
- marginal per-position frequencies,
- a **log-odds propensity** vs a background: `s(a,b) = log[ f(a,b) / f0(a,b) ]`. This log-odds is the propensity score — explicitly labeled as *not* ΔG. **The background `f0(a,b)` is a first-class, configurable choice, not an afterthought — it determines the entire candidate ordering.** Note that the naive independent-marginal null `f0(a)·f0(b)` conflates *pairing* preference with each position's individual composition preference; it is offered only as a baseline. The default and recommended null is **geometry/burial-conditioned**: `f0(a,b) = P(a,b | contact geometry bin, burial class)`, estimated from a background set of contacts with similar Cβ–Cβ distance, orientation, and buriedness. **Concrete default bins (override in config):** Cβ–Cβ distance in 1 Å bins over 4–8 Å; orientation in 3 coarse classes (parallel / anti-parallel / orthogonal Cβ vectors); burial in 3 classes (buried / intermediate / exposed by the S1 neighbor-count proxy). The background corpus is a fixed, versioned set of high-confidence contacts (PDB or pLDDT>90 AFDB), built once and pinned like any other database (§8). This isolates "what this *specific* converging geometry favors" from "what any buried pair favors." The chosen background, its bins, and the background corpus are recorded in `stats_summary.json` for provenance.
- an information/confidence flag on `N_eff` (contacts with too few independent hits are marked low-confidence and excluded from candidate generation). **Default threshold `N_eff_min = 10`** (config-exposed); the §11.2 pre-study calibrates this, and until it runs the threshold is treated as provisional.

**Failure handling.** Empty or near-empty hit sets are the *expected* outcome for rare geometries, not an error: a contact with zero surviving hits is recorded as `status: no_hits` and passed through as a low-confidence, no-candidate contact rather than crashing the run or being silently omitted. Oracle/subprocess crashes downstream fail that contact, not the whole run, and the run summary reports how many contacts reached each terminal state so a partial run is never mistaken for a clean one.

**Output — `stats/<contact>.parquet`** plus a run-level `stats_summary.json` (N_eff, top pairs, confidence flag, and terminal status per contact).

### S5 · Candidate enumeration

**Input.** `stats/*` + the current sequence (from LigandMPNN, if threaded; otherwise Tessera proposes de novo at the contact positions).

**Algorithm.**
- For each contact, rank residue pairs by log-odds propensity; keep the top-k (default 5) that beat the current assignment's propensity by a margin.
- Deduplicate against the current sequence (don't propose what's already there).
- Compose per-contact candidates into a bounded combinatorial set across a cluster, capping total candidates per design (default 200) to keep S6 tractable. **The cap is reached quickly for dense clusters, so the sampling strategy under the cap is specified, not left to truncation order:** rank the joint combinations by summed per-pair log-odds and take a **beam** (default width = cap / n_clusters) per cluster, guaranteeing every cluster gets representation rather than one dense cluster consuming the whole budget. Record how many combinations were dropped so the report never reads as exhaustive when it was pruned.
- **Cluster scoring honesty.** S1 groups coupled contacts into clusters, but the S6 oracle scores at most *pairs* (§7.1) — a 3-body-plus buried core cannot be scored jointly. So clusters are handled as **sets of pairwise candidates plus a whole-cluster refold check**, never as a single summed multi-body ΔΔG. Where a cluster is genuinely 3-body-coupled, candidate enumeration marks it `multibody: true` and defers the verdict to the redesign-then-refold loop (§12) rather than trusting summed pair scores.
- Respect user hard constraints (fixed positions, forbidden residues — e.g., exclude surface Cys, see §7.2).

**Output — `candidates.json`:** a list of mutation sets, each an ordered list of `(position, from_aa, to_aa)` with the originating propensity score and provenance (which contact, N_eff).

## 7. Stage details — energy half

### S6 · ΔΔG screening (oracle)

This stage converts candidates into calibrated energetics. It is the only stage that emits a ΔG-like number.

**S7.1 Primary oracle — ThermoMPNN-D.** Chosen because it is structure-based, ProteinMPNN-derived (so it inherits the same contact-aware embeddings we care about), and — critically — it has a **pairwise/epistatic** mode with a **distance filter** that restricts double mutations to spatially proximal residues and a stabilizing-ΔΔG threshold. That is a near-exact match to "score the two residues of a tertiary contact together." Modes used:
- *epistatic double* for the two residues of a contact scored jointly (not the additive sum of singles),
- *single* for one-sided reinforcements,
- optional site-saturation scan at a contact position when we want the full landscape rather than the harvested shortlist.

Output units: ΔΔG in kcal/mol relative to the input structure; more negative = more stabilizing.

**Single-point-of-failure discipline.** The entire energy half rests on this one model, its weights' license is unverified (§9), and the specific **epistatic-double-with-distance-filter mode is load-bearing** — if it is immature, unreleased, or license-encumbered, the core value proposition fails. Therefore: (a) S6 sits behind an **oracle interface** (`score_single`, `score_double`, `score_saturation`) so any backend satisfying it is swappable; (b) **Phase 0 gates on verifying the ThermoMPNN-D double mode actually exists, reproduces published pairwise ΔΔG, and clears its license** (§13) before it is trusted; (c) the named fallback is not "nothing" but **FoldX / Rosetta `cartesian_ddg` promoted from cross-check (§7.3) to primary**, at higher cost. No build ships with ThermoMPNN-D as an unverified hard dependency.

**S7.2 Caveats that constrain interpretation (must surface in the report).**
- **ΔΔG, not per-contact ΔG.** The oracle scores the stability *change* of a mutation relative to a reference. It does not decompose total fold energy into a per-contact ΔG; "the kcal/mol this contact is worth" is a different, harder question (FoldX per-term decomposition or alanine scanning, both approximate).
- **Monomer folding stability, not binding.** Trained on the megascale protease-assay dataset — domain fold stability. For a binder's actual affinity this is the wrong quantity; route affinity to Boltz-2's affinity head (with the CMP-memorization / head-disagreement caveats already tracked) or a folding-to-binding transfer model. Tessera's job is fold integrity of the binder scaffold, which is a legitimate and separable concern.
- **Surface-cysteine artifact** (megascale assay chemistry) — forbid surface Cys candidates by default in S5.
- **Hydrophobicity / aggregation bias** — flag candidates that increase surface hydrophobicity; useful for a buried core contact, dangerous on the surface.
- **Epistasis is hard** — even the double-mutant model is imperfect for strongly coupled pairs; treat close ties as ties and defer to experiment.
- **ΔΔG has a noise floor.** ThermoMPNN-class predictors carry ~1 kcal/mol error; a recommendation of −0.3 kcal/mol is within noise and must not be presented as a real improvement. S7 enforces a **minimum effect size** (`ddg_min_effect`, default −0.5 kcal/mol *and* larger than the oracle's per-prediction uncertainty) below which a candidate is reported as "no confident effect," not ranked as a win. Per-prediction uncertainty (from oracle ensembling or reported confidence) is propagated into the report, not discarded.
- **Cross-contact epistasis, not just within-pair.** Candidates are scored one contact at a time but emitted *jointly* into a single design bias (§7). The combined effect of several accepted reinforcements is **not** the sum of their individual ΔΔGs. The per-candidate scores are therefore explicitly labeled as marginal/independent estimates, and any multi-site set's true effect is arbitrated by the Boltz-2 refold (§11.4), not by summing the table.

**S7.3 Optional cross-check oracles.** FoldX (fast empirical, permissive to run) and Rosetta `cartesian_ddg` (physics-based, slower) as independent second opinions on the shortlist; disagreement across oracles is a stronger signal than any single score and is reported as such. All invoked as subprocesses.

**Output — `scored_candidates.parquet`:** each candidate with propensity score (S5), ΔΔG per oracle, oracle agreement, and flags (surface-Cys, hydrophobicity, low-N_eff provenance).

### S7 · Ranking, reporting, constraint emission

- **Composite ranking:** primarily ΔΔG, with propensity and oracle-agreement as tie-breakers; low-N_eff or flagged candidates demoted, not silently dropped. Candidates below the §7.2 **minimum effect size** (or whose effect is within their own uncertainty band) are segregated into a "no confident effect" section rather than ranked as wins. Every ranked row carries its ΔΔG *with* an uncertainty estimate, so ties within noise read as ties.
- **Report (`report.html`):** per-contact panels — contact geometry thumbnail, N_eff and top harvested pairs, current vs proposed residues, ΔΔG with oracle spread, and all flags. A summary table ranks recommended reinforcements across the whole design.
- **Constraint emission for the design loop:** write the accepted reinforcements as (a) a **LigandMPNN bias/omit specification** (per-position AA bias) and/or (b) a **Rosetta resfile**, so the recommendations can be fed back into a constrained redesign rather than applied blindly. This closes the loop with the existing RFdiffusion-AA → LigandMPNN → Boltz-2 pipeline: Tessera sits after the first LigandMPNN pass as a targeted-reinforcement + diagnostic step, and its constraints seed an optional second pass.

## 7A. Stability-triage front-end — weak-region diagnostic (mode: `triage`)

An optional front-end that runs *before* any reinforcement or grafting and answers "**where** is this fold weak, and would grafting help there?" It produces a ranked weak-region map, not a per-residue ΔG — because no such quantity is rigorously well-defined (see the note at the end of this section).

### D0 · Premise and units discipline

Folding free energy is global and cooperative; it does not decompose into additive per-residue kcal/mol. This module therefore reports **converging evidence for local sub-optimality**, and every signal keeps its native units. It is a violation of this spec to emit a synthesized "per-residue ΔG" column.

### D1 · Signal 1 — energetic frustration

For each residue and contact, compute a frustration index: a Z-score of the native interaction energy against a decoy distribution generated by (a) substituting other residues at the same location (*mutational* frustration) or (b) altering the local geometry (*configurational* frustration). Highly frustrated = the native is worse than the alternatives = a locally weak/strained position; minimally frustrated = optimized/load-bearing. Mutational frustration is the direct analog of "would another residue be more stabilizing here."

- **Fast pass:** FrustraMPNN — ProteinMPNN-graph model emitting per-residue frustration for all positions in one shot (same embedding family as the rest of the pipeline). Sequence-only alternative: FrustrAI-Seq (pLM-based, proteome-scale).
- **Physics cross-check:** Frustratometer 2 (AWSEM energy, with electrostatics) for an independent, non-learned view.
- Output: per-residue and per-contact frustration index + discretized class {highly / neutral / minimally frustrated}.

### D2 · Signal 2 — stabilizing-mutation density (ΔΔG saturation)

Run a ThermoMPNN site-saturation scan at every position (reuses the S6 oracle). The weak-region signal is **not** high mutational sensitivity — a buried core where every substitution is destabilizing is *strong*, not weak. The signal is a high **density of available stabilizing substitutions** (many mutations with favorable ΔΔG), which means the native sequence is leaving stability on the table at that position. Output: per-position count/fraction of stabilizing mutations below a ΔΔG threshold, and the best available ΔΔG.

### D3 · Signal 3 — flexibility overlay

Per-residue flexibility as a corroborating (not primary) signal: pLDDT/pAE from the design model, crystallographic B-factor if available, or RMSF from a short MD run. Floppy ≠ unstable, but *weak-and-floppy together* strengthens a call. Loops that are flexible by design should not be over-weighted on this signal alone.

### D4 · Consensus scoring

Cluster spatially contiguous residues that agree across signals into candidate **weak regions**. A region is high-confidence when frustration (D1) and stabilizing-mutation density (D2) agree; flexibility (D3) adjusts confidence. Each region gets a consensus score with the contributing per-signal values retained (no unit collapse).

### D5 · Functional-frustration exclusion (the critical filter)

Frustration is frequently *functional and evolutionarily selected* — binding sites, catalytic residues, and allosteric hinges are often highly frustrated on purpose, and stapling them shut destroys function. Before any weak region becomes a graft target it is triaged against function:

- Exclude regions overlapping the **binder–target interface / paratope** (for ANF-001, the HER2-contacting surface — which will read as weak precisely *because* it is a binding surface).
- Exclude regions overlapping ligand/metal/active-site residues and known or predicted allosteric/hinge positions.
- Where family data exist, use frustration *conservation* (FrustraEvo-style) to distinguish selected functional frustration from incidental frustration.
- Surviving regions are labeled **incidental weak regions** — the only valid graft targets.

**De novo note.** RFdiffusion outputs tend to be *under*-frustrated (over-idealized), so a genuine weak region stands out sharply against a smooth background — the diagnostic is often cleaner on designed scaffolds than on natural proteins, and a weak region that survives on an idealized backbone is a real defect rather than evolutionary baggage.

### D6 · Geometry → feature routing

For each incidental weak region, its geometry/dynamics proposes a feature, which is then handed to install mode (compatibility triage → install or scaffold):

| Weak-region signature | Proposed feature | Rationale |
|---|---|---|
| Exposed, floppy loop (high RMSF / low pLDDT) | disulfide staple or salt bridge across it | pays down the entropic cost of ordering the loop |
| Frustrated buried patch | core repack, or metal-coordination anchor | adds enthalpic packing / a fixed anchor point |
| Frustrated surface charge cluster | salt-bridge / H-bond network (HBNet) | satisfies buried/clustered polar groups |

**Output — `weak_regions.json` + `triage_report.html`:** ranked incidental weak regions, per-signal evidence, functional-exclusion flags, proposed feature, and a handoff record to install mode. No per-residue ΔG column — a ranked map with converging evidence, as above.


```
tessera run    --backbone design.pdb --seq design.fasta \
               --index /data/afdb50_folddisco --oracle thermompnn-d \
               --out runs/design_042 [--from S3 --to S6] [--preset strict] \
               [--no-retrieve]   # oracle-only mode: skip S2–S5, enumerate via the oracle (§3.7/§11.1)
tessera contacts / query / search / stats / candidates / score / report
               # each stage runnable standalone
tessera index  --db afdb50 ...   # wraps folddisco index with sane defaults
```

**Config.** A single `tessera.yaml` captures all thresholds (d_contact, sep_min, pLDDT cutoff, N_eff minimum, top-k, candidate cap, oracle choice, forbidden residues, background-null spec, support strategy, `feature_mode`, `ddg_min_effect`). CLI flags override. The resolved config is written into every run directory for provenance.

**Database and model versioning.** Reproducibility (§3.6) is meaningless without pinning the artifacts that most affect results: the resolved config records the **AFDB50 release**, **PDB snapshot date**, index build parameters, oracle **model + weights hash**, and tool versions (Folddisco, mmseqs2). Two runs with the same config but different database versions are not comparable and are flagged as such.

**Python API.** Thin functional wrappers (`tessera.detect_contacts`, `.harvest`, `.score`) returning pandas/pyarrow tables, for notebook use and integration.

**Run directory layout.**
```
runs/design_042/
  config.resolved.yaml
  contacts.json
  queries/  hits/  stats/
  candidates.json  scored_candidates.parquet
  report.html
  constraints/{ligandmpnn_bias.jsonl, design.resfile}
  logs/
```

### 8A · Interactive GUI (optional front-end)

The CLI/API is the primary interface; the GUI is a thin client over the same backend for interactive design review.

**Interaction model — select → operate → review, *not* freehand drawing.** Every backend operation is a search or a constrained build (Folddisco retrieval, compatibility triage, install-or-scaffold, ΔΔG screening), so its output is a *ranked list of geometrically- and energetically-valid candidates*, never a single hand-drawn edit. The UI must reflect that. A structure-drawing metaphor (editing backbone by dragging atoms) or a chemical-sketch metaphor (ChemDraw-style bond drawing) would misrepresent what the tool can do — the user does not draw the modification, they pick a target and a goal and choose among proposals. The loop:

1. **Select** a region in the 3D viewer — a loop, a contact, or a triage-flagged weak region painted onto the structure.
2. **Choose an operation** from a menu (not a canvas): *harvest stabilizing residues for this contact* · *find a disulfide staple across this loop* · *graft a metal site here* · *diagnose weak regions*.
3. **Backend runs** the real work on the secure compute service and returns ranked candidates.
4. **Review** candidates as viewer overlays annotated with frustration index, ΔΔG (with units labeled and no synthesized per-residue ΔG), and oracle agreement; accept/reject; accepted changes emit the LigandMPNN-bias / resfile / RFdiffusion-job artifacts already defined in §7/§7A.

**Two editing surfaces (the one place a sketcher belongs).**
- **3D protein surface** — a molecular viewer for the backbone/fold. Operation-menu-driven per the loop above. This is where residue/fold/feature work happens.
- **2D chemical sketcher** — for the *payload and linker only* (the MMAE warhead, the cleavable linker). This is genuine small-molecule 2D chemistry, where a ChemDraw-style metaphor is correct. Recommend Ketcher (open-source, embeddable) or ChemDraw if licenses exist. For a PDC program (ANF-001) the protein/payload split is a natural two-surface design, not a forced one.

**Viewer options and tradeoffs.**

| Option | Type | License | Notes |
|---|---|---|---|
| Mol* | web (WebGL) | Apache-2.0 | **Recommended.** Embeddable in Electron/React shell; runs anywhere; clean selection/overlay model; no license question for Anfinis. |
| NGL / 3Dmol.js | web (WebGL) | MIT | Lighter-weight web viewers; good fallbacks. |
| PyMOL plugin | desktop (Qt/Python) | open-source BSD-ish; **Schrödinger/Incentive PyMOL is commercial** | Fastest to prototype if you already work in PyMOL; but distribution and the commercial-build license make it worse for a shared/company tool. |

**Architecture — thin client, secure backend.** This is a thick workload behind a light UI: Folddisco indices (~1.5 TB) and GPU scoring cannot live in the browser. The GUI is a thin viewer/UI that talks to a backend service running on the controlled compute environment (RunPod Secure Cloud per current practice); the client sends selections + operation requests and receives candidates as coordinate overlays + scored tables. The proprietary indices and design sequences never leave the secure boundary and are never handled client-side. The same backend serves the CLI, so GUI and CLI stay behaviorally identical.

## 9. Dependencies and licensing

| Component | Role | License | How Tessera uses it | Commercial (Anfinis) status |
|---|---|---|---|---|
| Folddisco | discontinuous motif search | GPLv3 (Foldseek lineage) | subprocess only; no linking/redistribution of modified source | **OK** to run internally; do not ship a modified binary or statically link |
| Foldseek / mmseqs2 | optional hit clustering for reweighting | GPLv3 | subprocess only | OK, same terms |
| ThermoMPNN-D | ΔΔG oracle | ProteinMPNN (MIT) lineage — **verify head/weights license in repo** | subprocess or local API | **verify before relying**; MIT-derived but confirm the trained head |
| ProteinMPNN / LigandMPNN | upstream design (external to Tessera) | MIT | interoperate via files | OK |
| FoldX | optional oracle | academic/commercial license required | subprocess | **requires commercial license** for company use |
| Rosetta | optional oracle | commercial license required | subprocess | **requires commercial license** |
| dTERMen / Mosaist | *deliberately excluded* | non-commercial only | — | avoided precisely to keep the path commercial-clean |
| biotite / MDAnalysis / numpy / pandas / pyarrow | Tessera internals | BSD/permissive | library import | OK |

**Rule enforced in code:** every third-party structural/energetic tool is called via `subprocess` with file-based I/O. Nothing GPL is imported as a library. A `LICENSES.md` and a startup license-check (warn if FoldX/Rosetta selected without a configured license path) ship with the tool. Before any production Anfinis use, confirm the ThermoMPNN-D weight license and, if FoldX/Rosetta oracles are enabled, that valid commercial licenses are in place.

## 10. Compute and infrastructure

- **Folddisco index:** AFDB50 ≈ 1.45 TB on disk, buildable in <24 h on a many-core node; queries return in seconds. Provision a persistent volume; this is the dominant storage cost.
- **IP sensitivity.** For Anfinis designs, run indexing, search, and scoring on the controlled GPU environment (RunPod Secure Cloud, per current practice) and build indices locally rather than pulling structures through external services, so no proprietary backbone or candidate sequence leaves the boundary. The public Folddisco/Foldseek webserver is fine for method sanity-checks on non-proprietary structures only.
- **GPU.** ThermoMPNN-D scoring is light; a single modern GPU handles site-saturation scans in minutes. Folddisco search is CPU-bound (GPU optional).
- **Throughput.** Per design: contact detection and query construction are trivial; search is seconds–minutes per motif; the cost scales with number of contacts × prefilter depth. The candidate cap (S5) bounds the oracle stage.
- **Redundancy reweighting is a likely real bottleneck.** The per-contact hit-clustering pass (mmseqs2 / Foldseek-cluster over each contact's hit set, §6.3) can dominate wall-clock when hit sets are large — potentially more than the search itself. Budget and benchmark it explicitly; cache cluster assignments keyed on the hit-set hash so reruns and shared subsequences don't recompute, and cap hits fed to clustering with a documented (not silent) limit.

## 11. Validation plan

The first two experiments are **gates**: they decide whether the retrieval half ships at all (§3.7) and run in Phase 0 (§13) before the full pipeline is built.

1. **Retrieval ablation (the headline experiment — gate).** Compare full Tessera (retrieval → oracle) against **oracle-only** enumeration (site-saturation + epistatic-double scan with no Folddisco) on the same contacts. Metrics: quality of the top-ranked stabilizing recommendations (validated where possible against measured or refold ΔΔG) and total compute. If retrieval does not produce better *or* meaningfully cheaper recommendations than oracle-only, S2–S5 do not ship and `--no-retrieve` becomes the default. This directly tests the concern that the oracle already covers the retrieval half.
2. **N_eff feasibility pre-study (gate).** On a panel of natural domains *and* idealized RFdiffusion backbones, measure the distribution of effective independent hits per contact after pLDDT filtering and redundancy collapse, as a function of query tolerance, segment padding, and `feature_mode` (sequenced vs. bare backbone). Report the fraction of contacts clearing the N_eff bar. If that fraction is low on idealized backbones, the statistics half is a niche add-on and phasing must reflect it. Cheap to run; must precede the AFDB50 index build.
3. **Recovery on natural folds.** Run S1–S5 on native domains with residues masked at contacts; check that harvested top propensities recover native residues at rates comparable to published TERM/dTERMen recovery. Establishes the retrieval+statistics stack is sound.
4. **End-to-end on a design.** Take a real binder scaffold, apply Tessera reinforcements via the emitted LigandMPNN bias, re-fold with Boltz-2, and check pLDDT/ipTM and predicted stability move the right way relative to the un-reinforced control. This refold is also the arbiter for multi-site sets whose cross-contact epistasis the per-candidate scores cannot capture (§7.2).
5. **ΔΔG calibration.** Benchmark S6 on a held-out slice of the megascale set and on any in-house stability measurements; report correlation, quantify the per-prediction uncertainty that feeds the §7.2 effect-size floor, and confirm the pairwise mode beats additive-singles on coupled pairs.
6. **Redundancy-correction ablation.** Show that reweighting materially changes top-ranked residues on families known to be over-sampled (guards against the "what got sequenced a lot" failure mode).
7. **Oracle-agreement audit.** Quantify how often ThermoMPNN-D, FoldX, and Rosetta agree on sign; define the confidence tiers used in the report from this.
8. **Support-strategy comparison.** Compare `reinforce-marginal` vs. `protect-loadbearing` contact selection (S1) on refold outcomes to settle which policy actually helps, rather than assuming.

## 12. Known limitations and open questions

- **Single-chain v0.1.** Inter-chain contacts (binder–target interface) need multi-chain contact detection and a binding-aware oracle; deferred, but the interface stabilization case is arguably the higher-value one and should be the first extension.
- **Propensity ≠ ΔG, restated.** The retrieval signal is a prior over sequence given geometry, not an energy; the whole architecture exists to keep these separate, and the report must never present a propensity as a ΔG.
- **AFDB confidence.** Even after pLDDT filtering, predicted-structure geometry has systematic errors; PDB-only runs are the high-confidence fallback at the cost of coverage/N_eff.
- **Backbone realism.** Statistics harvested for an idealized RFdiffusion backbone assume the contact geometry is physically realizable; if the backbone itself is strained, no residue set will stabilize it and Tessera will still dutifully report candidates. Pair with a backbone-quality check.
- **Coupled multi-body contacts.** Three-body+ packing (buried cores) exceeds the pairwise oracle; treat clusters holistically and lean on a redesign-then-refold loop rather than trusting summed pair scores.
- **ThermoMPNN-D scope.** Monomer folding stability only; do not overload it with binding questions.

## 13. Phasing

- **Phase 0 (MVP + gates):** S1, S2, S3 (single index, e.g., SwissProt or PDB for confidence), S4 with pLDDT filter + basic reweighting, S6 ThermoMPNN-D single+double, minimal ranked TSV output. **Three gates must pass here before Phase 1 is committed:** (a) the **retrieval ablation** (§11.1) — does retrieval beat oracle-only; (b) the **N_eff feasibility pre-study** (§11.2) — do enough contacts clear the bar, especially on idealized backbones; (c) **ThermoMPNN-D verification** (§7.1/§9) — does the double mode exist, reproduce published ΔΔG, and clear its license. A failed gate reshapes Phase 1 (e.g., ship oracle-only, or fall back to FoldX/Rosetta) rather than proceeding on assumption.
- **Phase 1:** AFDB50 index, full redundancy reweighting, candidate enumeration with constraints, HTML report, LigandMPNN-bias emission (closes the design loop).
- **Phase 2:** multi-chain / interface contacts + binding-aware scoring; FoldX/Rosetta cross-check oracles; oracle-agreement confidence tiers.
- **Phase 3:** feedback automation (constrained redesign → Boltz-2 refold → re-score) as a single command.

## 14. Build and implementation guidance

This section is for whoever (human or coding agent) implements Tessera. The governing reality: **most of the real stack cannot run during development** — no GPU, no ~1.5 TB index, licensed binaries (FoldX/Rosetta) and unreleased weights (ThermoMPNN-D) often absent, and no proprietary backbones to test on. The engineering plan is built around being verifiable *without* that stack.

**14.1 Target environment.** Linux, despite Windows workstations: every external tool (Folddisco, mmseqs2, Rosetta, the RunPod flow) is Linux-native. Develop in WSL2 or a Linux devcontainer; do not target native Windows. Pin Python + a locked toolchain (uv/poetry, pytest, ruff, a type checker) and a devcontainer so the environment is reproducible.

**14.2 Mock-first, real-second.** Build the full S1–S7 pipeline against **mock external tools plus captured fixtures first**, get it green end-to-end, then swap in real binaries on the Linux/GPU box. Every external tool sits behind a thin adapter (the S6 oracle interface is the template; extend it to Folddisco, mmseqs2, FoldX) with (a) a documented file-based I/O contract and (b) a fake implementation. Ship a `--dry-run`/mock mode so the pipeline — and the coding agent's own self-checks — run without the real stack.

**14.3 Fixtures are captured, never invented.** The single largest silent-failure risk is an implementer guessing an external tool's output format. Before writing parsers, capture **real** examples into `tests/fixtures/`: a real Folddisco `hits.tsv` (build a tiny index on ~100 structures and run one query), a real ThermoMPNN output, a handful of PDB/AFDB structures. Parsers are written to match these, and the exact tool versions/column orders are recorded.

**14.4 Contract-first.** Write and freeze the typed stage schemas (`contacts.json`, `stats/*.parquet`, `candidates.json`, `scored_candidates.parquet`) *before* stage logic, as validated models (e.g. pydantic + JSON Schema). This is what makes S1–S7 independently testable and `--from/--to` reruns real rather than aspirational.

**14.5 Invariants as types, not prose.** The spec's hard rules must be enforced by the compiler and tests, because an implementer under pressure will otherwise violate them:
- **Propensity and energy are distinct types** (`Propensity` vs `DeltaG`) with no defined addition or comparison between them — the §3.1 separation becomes structurally impossible to breach, and "sum a propensity into a ΔG" fails to compile.
- **No per-residue ΔG** — a CI test fails if such a column appears in any emitted artifact (§7A/D0).
- **No GPL linkage** — a CI/lint check asserts GPL tools are only ever `subprocess`'d, never imported (§9).
- **Provenance tags are mandatory fields**, not optional — `feature_mode`, database/model versions, background-null spec, and per-contact `status` are required by the schemas, so a run cannot silently drop them.

**14.6 Golden tests for the silent-bug zone.** S1 (contact detection) and S4 (reweighting, log-odds) produce plausible numbers even when wrong, so they need hand-verified expected values, not just "it runs": a toy structure with known Cβ distances for S1; a synthetic over-sampled hit set for S4 where reweighting *must* change the top residue (validation §11.6 as a unit test); a low-`N_eff` case that exercises pseudocount smoothing without blowing up.

**14.7 Build order.** Follow §13, and **build the Phase-0 gates (§11.1 ablation, §11.2 N_eff pre-study) before the full pipeline** — they can invalidate the architecture cheaply. Do not implement S2–S5 as load-bearing until the ablation passes; `--no-retrieve` (oracle-only) is the fallback design and should work from day one. Resolve the config defaults this section pins (N_eff, background bins, pseudocounts) before coding the stages that consume them.

**14.8 Repo hygiene.** Initialize git; keep this spec in-repo as the source of truth and update it as design reality shifts. A short `CLAUDE.md` (or `CONTRIBUTING.md`) should encode: the §14.5 invariants, the mock-first/Linux-target conventions, the license rule, and the requirement that external-tool contracts trace to a captured fixture. Confirm the ThermoMPNN-D weights license and any FoldX/Rosetta commercial licenses **before** writing integration code that assumes their availability.

---

*Open decisions to confirm before build:* target proteome index (AFDB50 vs PDB-first for confidence), whether the binder–target interface case should be pulled forward into Phase 0/1 given its value, and the ThermoMPNN-D weight license status for commercial use.
