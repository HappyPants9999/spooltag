"""Server-rendered HTML for phones. No JS framework, no external assets."""
import time
from html import escape
from typing import Optional

import segno

from .clients import spool_label

CSS = """
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1a;--dim:#6b6b66;--line:#e2e2dd;--ok:#1f7a3a;--bad:#b3261e;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--fg:#ecece8;--dim:#9a9a93;--line:#33332f;--ok:#5cc47a;--bad:#ff8a80;--accent:#8aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:680px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:.2em 0 .6em}h2{font-size:1.05rem;margin:1.4em 0 .5em;color:var(--dim);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:10px 0}
.big{font-size:1.6rem;font-weight:700;margin:.1em 0}
.dim{color:var(--dim)}.ok{color:var(--ok)}.bad{color:var(--bad)}
.sw{display:inline-block;width:1.1em;height:1.1em;border-radius:50%;border:1px solid var(--line);vertical-align:-.2em;margin-right:.4em}
.tools{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tool{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;min-height:96px}
.tool b{font-size:1.1rem}.tool small{display:block;color:var(--dim);overflow-wrap:anywhere}
ul.steps{list-style:none;padding:0;margin:.6em 0 0}ul.steps li{margin:.2em 0}
nav{display:flex;gap:14px;margin-bottom:6px}a{color:var(--accent)}
button{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input{font:inherit;width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.tag{display:flex;gap:12px;align-items:center}.tag svg{flex:none;width:96px;height:96px;background:#fff;border-radius:6px}
code{overflow-wrap:anywhere;font-size:.85rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}td{padding:4px 6px;border-top:1px solid var(--line);vertical-align:top}
"""


def page(title: str, body: str, refresh: Optional[int] = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{escape(title)}</title><style>{CSS}</style></head><body><main>'
            f'<nav><a href="/">Tools</a><a href="/tags">Tags</a></nav>{body}</main></body></html>')


def swatch(color: Optional[str]) -> str:
    if not color:
        return ""
    c = "#" + color.lstrip("#")[:8]
    return f'<span class="sw" style="background:{escape(c)}"></span>'


def spool_card(spool: dict) -> str:
    fil = spool.get("filament") or {}
    rem = spool.get("remaining_weight")
    rem_txt = f"{rem:.0f} g left" if isinstance(rem, (int, float)) else ""
    loc = spool.get("location") or "no location"
    return (f'<div class="card"><div class="dim">Spool #{spool["id"]}</div>'
            f'<div class="big">{swatch(fil.get("color_hex"))}{escape(spool_label(spool))}</div>'
            f'<div class="dim">{escape(rem_txt)} · {escape(loc)}</div></div>')


def steps_list(steps: list[tuple[bool, str]]) -> str:
    items = "".join(f'<li class="{"ok" if ok else "bad"}">{"✓" if ok else "✗"} {escape(t)}</li>'
                    for ok, t in steps)
    return f'<ul class="steps">{items}</ul>'


def scanned_spool(spool: dict, timeout: int) -> str:
    return page("Scan a tool", f'<h1>Spool scanned</h1>{spool_card(spool)}'
                f'<div class="card"><div class="big">Now scan a tool tag</div>'
                f'<div class="dim">Waiting {timeout // 60 or 1} min. Scanning another spool replaces this one.</div></div>')


def scanned_tool(tool: int, current: Optional[dict], timeout: int) -> str:
    cur = ""
    if current and current.get("spool_id"):
        cur = (f'<div class="dim">Currently: {swatch(current.get("color"))}'
               f'#{current["spool_id"]} {escape(current.get("material") or "")} '
               f'{escape(current.get("name") or "")}</div>')
    return page("Scan a spool", f'<h1>Tool T{tool} scanned</h1>'
                f'<div class="card"><div class="big">Now scan a spool tag</div>{cur}'
                f'<div class="dim">Waiting {timeout // 60 or 1} min.</div></div>')


def assigned(result) -> str:
    head = ('<h1 class="ok">✓ Assigned</h1>' if result.ok else
            '<h1 class="bad">Assigned with problems</h1>')
    spool = spool_card(result.spool) if result.spool else ""
    return page("Assigned" if result.ok else "Problem",
                f'{head}<div class="card"><div class="big">→ T{result.tool}</div>'
                f'{steps_list(result.steps)}</div>{spool}')


def message(title: str, text: str, bad: bool = False) -> str:
    return page(title, f'<h1 class="{"bad" if bad else ""}">{escape(title)}</h1>'
                f'<div class="card">{escape(text)}</div>')


def status(num_tools: int, lanes: dict[int, dict], error: Optional[str], pending, recent) -> str:
    cards = []
    for t in range(num_tools):
        lane = lanes.get(t)
        if lane and lane.get("spool_id"):
            body = (f'{swatch(lane.get("color"))}<b>T{t}</b>'
                    f'<small>#{lane["spool_id"]} {escape(lane.get("vendor_name") or "")} '
                    f'{escape(lane.get("material") or "")}</small>'
                    f'<small>{escape(lane.get("name") or "")}</small>'
                    f'<form method="post" action="/tools/{t}/clear" style="margin-top:6px">'
                    f'<button>Clear</button></form>')
        elif lane:
            body = (f'{swatch(lane.get("color"))}<b>T{t}</b>'
                    f'<small>{escape(lane.get("material") or "")} (no spool id)</small>')
        else:
            body = f'<b>T{t}</b><small>empty</small>'
        cards.append(f'<div class="tool">{body}</div>')
    err = f'<div class="card bad">{escape(error)}</div>' if error else ""
    pend = ""
    if pending:
        what = f"spool #{pending.value}" if pending.kind == "spool" else f"tool T{pending.value}"
        pend = (f'<div class="card">Waiting for a {"tool" if pending.kind == "spool" else "spool"} '
                f'scan after {escape(what)}. <form method="post" action="/pending/clear" '
                f'style="display:inline"><button>Cancel</button></form></div>')
    rows = "".join(
        f'<tr><td class="dim">{time.strftime("%m-%d %H:%M", time.localtime(ts))}</td>'
        f'<td>{escape(action)}</td><td>{"T" + str(tool) if tool is not None else ""}</td>'
        f'<td>{"#" + str(sid) if sid else ""} {escape(detail or "")}</td></tr>'
        for ts, sid, tool, action, detail in recent)
    log = f'<h2>Recent</h2><div class="card"><table>{rows}</table></div>' if rows else ""
    return page("Spooltag", f'<h1>Tools</h1>{err}{pend}<div class="tools">{"".join(cards)}</div>{log}',
                refresh=30)


def qr_svg(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=3, border=2, dark="#000", light="#fff")


def tags(base: str, num_tools: int, spools: list[dict], error: Optional[str]) -> str:
    tool_cards = "".join(
        f'<div class="card tag">{qr_svg(f"{base}/t/{t}")}<div><b>T{t}</b><br>'
        f'<code>{escape(base)}/t/{t}</code></div></div>' for t in range(num_tools))
    spool_cards = "".join(
        f'<div class="card tag" data-q="{escape((spool_label(s) + " " + str(s["id"]) + " " + (s.get("location") or "")).lower())}">'
        f'{qr_svg(f"{base}/s/{s["id"]}")}<div>{swatch((s.get("filament") or {}).get("color_hex"))}'
        f'<b>#{s["id"]} {escape(spool_label(s))}</b><br><span class="dim">{escape(s.get("location") or "")}</span><br>'
        f'<code>{escape(base)}/s/{s["id"]}</code></div></div>'
        for s in sorted(spools, key=lambda s: s["id"]))
    err = f'<div class="card bad">{escape(error)}</div>' if error else ""
    script = ("<script>document.getElementById('q').addEventListener('input',e=>{const v=e.target.value.toLowerCase();"
              "document.querySelectorAll('[data-q]').forEach(c=>c.style.display=c.dataset.q.includes(v)?'':'none')})</script>")
    return page("Tags", '<h1>Tag URLs</h1><p class="dim">Write these as a URL record with NFC Tools. '
                'Tool tags are written once; spool tags whenever a box gets a new spool.</p>'
                f'<h2>Tools</h2>{tool_cards}<h2>Spools</h2>{err}'
                f'<input id="q" placeholder="Filter spools…">{spool_cards}{script}')
