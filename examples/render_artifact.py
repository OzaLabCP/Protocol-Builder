"""Render the validated emit_protocol JSON to a static protocol-sheet HTML fragment
(for publishing as an Artifact). Server-rendered, no client JS needed."""

import html
import json
import sys
from collections import Counter

proto = json.load(open("examples/binding_assay_eason2020.json"))


def e(s):
    return html.escape("" if s is None else str(s))


def pill(t):
    return f'<span class="tier t-{e(t)}">{e((t or "").replace("_", " "))}</span>'


def note(n):
    return f'<div class="pnote">{e(n)}</div>' if n else ""


tiers = Counter()
for m in proto["materials"]:
    tiers[m["provenance"]] += 1
for s in proto["steps"]:
    tiers[s["provenance"]] += 1
    for cp in s.get("critical_parameters", []):
        tiers[cp["provenance"]] += 1
    for ss in s.get("substeps", []):
        tiers[ss["provenance"]] += 1

TIER_ORDER = ["stated", "literature_grounded", "best_practice", "user_input", "default_verify"]

# ---- materials rows
mat_rows = ""
for m in proto["materials"]:
    amt = " ".join(str(x) for x in [m.get("amount"), m.get("unit")] if x not in (None, ""))
    mat_rows += (
        f'<tr><td>{e(m["name"])}</td><td class="num">{e(amt) or "—"}</td>'
        f'<td>{e(m.get("vendor_or_grade") or "—")}</td>'
        f'<td>{pill(m["provenance"])}{note(m.get("provenance_note"))}</td></tr>'
    )

# ---- steps
steps_html = ""
for s in proto["steps"]:
    cps = ""
    for cp in s.get("critical_parameters", []):
        val = " ".join(str(x) for x in [cp.get("value"), cp.get("unit")] if x not in (None, ""))
        cps += (
            f'<div class="cp"><span class="cp-name">{e(cp["name"])}</span>'
            f'<span class="cp-val">{e(val)}</span> {pill(cp["provenance"])}'
            f'{note(cp.get("provenance_note"))}</div>'
        )
    subs = "".join(
        f'<div class="sub"><span class="sub-n">{e(ss["number"])}</span> {e(ss["instruction"])} {pill(ss["provenance"])}</div>'
        for ss in s.get("substeps", [])
    )
    warns = "".join(f'<div class="warn">⚠ {e(w)}</div>' for w in s.get("warnings", []))
    meta = " · ".join(
        x for x in [
            f'⏱ {e(s.get("duration"))}' if s.get("duration") else "",
            f'🌡 {e(s.get("temperature"))}' if s.get("temperature") else "",
        ] if x
    )
    steps_html += f"""
    <li class="step">
      <div class="step-head"><span class="step-n">{e(s['number'])}</span>
        <h3>{e(s['title'])}</h3> {pill(s['provenance'])}</div>
      <p class="instr">{e(s['instruction'])}</p>
      {f'<div class="meta">{meta}</div>' if meta else ''}
      {subs}
      {cps}
      {warns}
    </li>"""

# ---- assumptions log
alog = ""
for a in proto["assumptions_log"]:
    vflag = '<span class="vflag">verify</span>' if a.get("verify") else ""
    alog += (
        f'<tr><td>{e(a["parameter"])}</td><td class="num">{e(a["value"])}</td>'
        f'<td>{pill(a["provenance"])}</td><td>{e(a.get("basis"))} {vflag}</td></tr>'
    )

oq = "".join(f"<li>{e(q)}</li>" for q in proto.get("open_questions", []))

legend = "".join(pill(t) for t in TIER_ORDER)
strip = "".join(
    f'<div class="stat"><b>{tiers.get(t,0)}</b>{pill(t)}</div>' for t in TIER_ORDER
)

CSS = """
<style>
:root{
  --bg:#f6f7f9; --surface:#ffffff; --surface-2:#eef1f5; --line:#dde2ea;
  --ink:#1a2230; --muted:#5b6675; --accent:#0b6d83;
  --t-stated:#1f9d57; --t-literature_grounded:#2563c9; --t-best_practice:#8257d6;
  --t-user_input:#0d9488; --t-default_verify:#c77d16;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f141b; --surface:#151b24; --surface-2:#1b232e; --line:#2a3441;
    --ink:#e6eaf0; --muted:#93a0b1; --accent:#3bbcd6;
    --t-stated:#43c98a; --t-literature_grounded:#6ea8fe; --t-best_practice:#b48cff;
    --t-user_input:#2dd4bf; --t-default_verify:#f2b13a;
  }
}
:root[data-theme="light"]{
  --bg:#f6f7f9; --surface:#ffffff; --surface-2:#eef1f5; --line:#dde2ea;
  --ink:#1a2230; --muted:#5b6675; --accent:#0b6d83;
  --t-stated:#1f9d57; --t-literature_grounded:#2563c9; --t-best_practice:#8257d6;
  --t-user_input:#0d9488; --t-default_verify:#c77d16;
}
:root[data-theme="dark"]{
  --bg:#0f141b; --surface:#151b24; --surface-2:#1b232e; --line:#2a3441;
  --ink:#e6eaf0; --muted:#93a0b1; --accent:#3bbcd6;
  --t-stated:#43c98a; --t-literature_grounded:#6ea8fe; --t-best_practice:#b48cff;
  --t-user_input:#2dd4bf; --t-default_verify:#f2b13a;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-variant-numeric:tabular-nums;}
.wrap{max-width:860px;margin:0 auto;padding:40px 22px 90px;}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
.eyebrow{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--accent);margin:0 0 10px;}
h1{font-size:30px;line-height:1.15;margin:0 0 10px;letter-spacing:-.01em;text-wrap:balance;font-weight:680;}
h2{font-size:15px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  font-family:ui-monospace,monospace;font-weight:600;margin:40px 0 12px;
  padding-bottom:8px;border-bottom:1px solid var(--line);}
h3{font-size:17px;margin:0;font-weight:640;}
.source{font-family:ui-monospace,monospace;font-size:13px;color:var(--muted);margin:0 0 18px;}
.source a{color:var(--accent);}
.summary{max-width:68ch;color:var(--ink);margin:0 0 6px;}
.dur{color:var(--muted);font-size:14px;margin:0 0 24px;}
.strip{display:flex;flex-wrap:wrap;gap:10px;margin:22px 0;}
.stat{display:flex;align-items:center;gap:8px;background:var(--surface);
  border:1px solid var(--line);border-radius:10px;padding:9px 12px;}
.stat b{font-size:20px;min-width:18px;text-align:center;}
.legend{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 6px;}
.tier{font-family:ui-monospace,monospace;font-size:10.5px;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;padding:2px 8px;border-radius:999px;white-space:nowrap;}
.t-stated{background:color-mix(in srgb,var(--t-stated) 16%,transparent);color:var(--t-stated);}
.t-literature_grounded{background:color-mix(in srgb,var(--t-literature_grounded) 16%,transparent);color:var(--t-literature_grounded);}
.t-best_practice{background:color-mix(in srgb,var(--t-best_practice) 16%,transparent);color:var(--t-best_practice);}
.t-user_input{background:color-mix(in srgb,var(--t-user_input) 16%,transparent);color:var(--t-user_input);}
.t-default_verify{background:color-mix(in srgb,var(--t-default_verify) 16%,transparent);color:var(--t-default_verify);}
.callout{background:var(--surface-2);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:12px 16px;margin:18px 0;font-size:14px;color:var(--muted);max-width:70ch;}
.callout b{color:var(--ink);}
.tablewrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:14.5px;}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
th{font-family:ui-monospace,monospace;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);font-weight:600;}
td.num{font-family:ui-monospace,monospace;white-space:nowrap;}
ol.steps{list-style:none;margin:0;padding:0;}
.step{padding:18px 0;border-bottom:1px solid var(--line);}
.step-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
.step-n{font-family:ui-monospace,monospace;font-weight:700;color:var(--accent);
  border:1.5px solid var(--accent);border-radius:8px;min-width:28px;height:28px;
  display:inline-flex;align-items:center;justify-content:center;font-size:14px;}
.instr{margin:10px 0 0;max-width:70ch;}
.meta{font-family:ui-monospace,monospace;font-size:13px;color:var(--muted);margin-top:8px;}
.cp{margin-top:10px;padding-left:12px;border-left:2px solid var(--line);}
.cp-name{color:var(--muted);font-size:14px;}
.cp-val{font-family:ui-monospace,monospace;font-weight:650;margin:0 8px;}
.sub{margin-top:8px;padding-left:12px;color:var(--ink);font-size:14.5px;}
.sub-n{font-family:ui-monospace,monospace;color:var(--muted);margin-right:6px;}
.pnote{font-family:ui-monospace,monospace;font-size:12.5px;color:var(--muted);
  margin-top:4px;line-height:1.5;max-width:78ch;}
.warn{color:var(--t-default_verify);font-size:13.5px;margin-top:8px;}
.panel{background:var(--surface-2);border:1px solid var(--line);border-radius:12px;padding:6px 16px 14px;}
.vflag{font-family:ui-monospace,monospace;font-size:11px;color:var(--t-default_verify);
  border:1px solid color-mix(in srgb,var(--t-default_verify) 45%,transparent);
  border-radius:999px;padding:0 6px;margin-left:4px;}
ul.oq{max-width:74ch;padding-left:18px;color:var(--muted);}
ul.oq li{margin:7px 0;}
footer{margin-top:40px;font-size:12.5px;color:var(--muted);font-family:ui-monospace,monospace;line-height:1.6;}
a{color:var(--accent);}
</style>
"""

HTML = f"""{CSS}
<title>gSTEP variant binding screen — provenance-tagged</title>
<div class="wrap">
  <p class="eyebrow">Reconstructed protocol · provenance-tagged</p>
  <h1>{e(proto['title'])}</h1>
  <p class="source">Source: <a href="https://doi.org/10.1021/acssynbio.0c00407" target="_blank" rel="noopener">Eason et&nbsp;al., ACS Synth. Biol. 2020 — 10.1021/acssynbio.0c00407</a></p>
  <p class="summary">{e(proto['summary'])}</p>
  <p class="dur">Estimated duration: {e(proto['estimated_duration'])}</p>

  <div class="callout">
    <b>How to read this.</b> Every value is tagged by how it was obtained. Nothing here is silently invented:
    values present in the paper are <b>stated</b>; gaps the paper left open are filled from <b>best practice</b>,
    your own <b>answers</b>, or flagged as a <b>default to verify</b>. This copy was reconstructed offline, so no
    values are <b>literature-grounded</b> — on a live run, Phase&nbsp;2 web search grounds the best-practice/verify
    items in real citations, and the host resolves each DOI/PMID before shipping.
  </div>

  <div class="strip">{strip}</div>
  <div class="legend">{legend}</div>

  <h2>Materials</h2>
  <div class="tablewrap"><table>
    <tr><th>Reagent</th><th>Amount</th><th>Grade / vendor</th><th>Provenance</th></tr>
    {mat_rows}
  </table></div>

  <h2>Equipment</h2>
  <ul>{"".join(f"<li>{e(x)}</li>" for x in proto.get("equipment", []))}</ul>

  <h2>Procedure</h2>
  <ol class="steps">{steps_html}
  </ol>

  <h2>Assumptions log — everything not stated in the source</h2>
  <div class="panel tablewrap"><table>
    <tr><th>Parameter</th><th>Value</th><th>Provenance</th><th>Basis</th></tr>
    {alog}
  </table></div>

  <h2>Open questions</h2>
  <ul class="oq">{oq}</ul>

  <footer>
    Built by Methods Gap-Filler as a hand-run demonstration (no API key in the sandbox).<br>
    Binding-assay methods reconstructed from Eason et&nbsp;al. 2020. Provenance verification runs live on your machine.
  </footer>
</div>
"""

out = sys.argv[1] if len(sys.argv) > 1 else "examples/binding_assay_eason2020.html"
open(out, "w").write(HTML)
print("wrote", out, len(HTML), "bytes")
