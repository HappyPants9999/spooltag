"""Server-rendered HTML for phones. No JS framework, no external assets."""
import time
from html import escape
from typing import Optional

import segno

from .clients import spool_label
from .store import Location, Printer

CSS = """
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1a;--dim:#6b6b66;--line:#e2e2dd;--ok:#1f7a3a;--bad:#b3261e;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--fg:#ecece8;--dim:#9a9a93;--line:#33332f;--ok:#5cc47a;--bad:#ff8a80;--accent:#8aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:680px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:.2em 0 .6em}h2{font-size:1.05rem;margin:1.4em 0 .5em;color:var(--dim);font-weight:600}
h3{font-size:1rem;margin:1em 0 .4em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:10px 0}
.big{font-size:1.6rem;font-weight:700;margin:.1em 0}
.dim{color:var(--dim)}.ok{color:var(--ok)}.bad{color:var(--bad)}
.sw{display:inline-block;width:1.1em;height:1.1em;border-radius:50%;border:1px solid var(--line);vertical-align:-.2em;margin-right:.4em}
.tools{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tool{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;min-height:96px}
.tool b{font-size:1.1rem}.tool small{display:block;color:var(--dim);overflow-wrap:anywhere}
ul.steps,ul.plain{list-style:none;padding:0;margin:.6em 0 0}ul.steps li,ul.plain li{margin:.25em 0}
nav{display:flex;gap:14px;margin-bottom:6px;flex-wrap:wrap}a{color:var(--accent)}
button{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}
input,select{font:inherit;width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
input[type=checkbox]{width:auto;margin-right:.4em}
label{display:block;margin:.5em 0 .2em;font-size:.9rem;color:var(--dim)}label.check{color:var(--fg)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}@media (max-width:420px){.grid2{grid-template-columns:1fr}}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between}
.tag{display:flex;gap:12px;align-items:center}button.copy{margin-top:6px}.tag svg{flex:none;width:96px;height:96px;background:#fff;border-radius:6px}
code{overflow-wrap:anywhere;font-size:.85rem}
details summary{cursor:pointer;color:var(--accent)}
table{border-collapse:collapse;width:100%;font-size:.9rem}td{padding:4px 6px;border-top:1px solid var(--line);vertical-align:top}
"""


def page(title: str, body: str, refresh: Optional[int] = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{escape(title)}</title><style>{CSS}</style></head><body><main>'
            f'<nav><a href="/">Status</a><a href="/tags">Tags</a><a href="/settings">Settings</a></nav>'
            f'{body}</main></body></html>')


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


def spool_line(spool: dict) -> str:
    fil = spool.get("filament") or {}
    rem = spool.get("remaining_weight")
    rem_txt = f' <span class="dim">{rem:.0f} g</span>' if isinstance(rem, (int, float)) else ""
    return f'<li>{swatch(fil.get("color_hex"))}#{spool["id"]} {escape(spool_label(spool))}{rem_txt}</li>'


def steps_list(steps: list[tuple[bool, str]]) -> str:
    items = "".join(f'<li class="{"ok" if ok else "bad"}">{"✓" if ok else "✗"} {escape(t)}</li>'
                    for ok, t in steps)
    return f'<ul class="steps">{items}</ul>'


def _wait(timeout: int) -> str:
    return f'Waiting {max(timeout // 60, 1)} min.'


def scanned_spool(spool: dict, timeout: int) -> str:
    return page("Scan a tool or location", f'<h1>Spool scanned</h1>{spool_card(spool)}'
                f'<div class="card"><div class="big">Now scan a tool or storage tag</div>'
                f'<div class="dim">{_wait(timeout)} Scanning another spool replaces this one.</div></div>')


def scanned_tool(target: str, current: Optional[dict], timeout: int) -> str:
    cur = ""
    if current and current.get("spool_id"):
        cur = (f'<div class="dim">Currently: {swatch(current.get("color"))}'
               f'#{current["spool_id"]} {escape(current.get("material") or "")} '
               f'{escape(current.get("name") or "")}</div>')
    return page("Scan a spool", f'<h1>{escape(target)} scanned</h1>'
                f'<div class="card"><div class="big">Now scan a spool tag</div>{cur}'
                f'<div class="dim">{_wait(timeout)}</div></div>')


def location_contents(location: Location, spools: list[dict], error: Optional[str],
                      waiting: Optional[int] = None) -> str:
    head = ""
    if waiting is not None:
        head = (f'<div class="card"><div class="big">Scan a spool to store it here</div>'
                f'<div class="dim">{_wait(waiting)}</div></div>')
    if error:
        body = f'<div class="card bad">{escape(error)}</div>'
    elif spools:
        body = f'<div class="card"><ul class="plain">{"".join(spool_line(s) for s in spools)}</ul></div>'
    else:
        body = '<div class="card dim">No spools here.</div>'
    return page(location.name, f'<h1>{escape(location.name)}</h1>{head}<h2>In this location</h2>{body}')


def done(result, verb: str) -> str:
    head = (f'<h1 class="ok">✓ {escape(verb)}</h1>' if result.ok else
            f'<h1 class="bad">{escape(verb)} with problems</h1>')
    spool = spool_card(result.spool) if result.spool else ""
    return page(verb if result.ok else "Problem",
                f'{head}<div class="card"><div class="big">→ {escape(result.target)}</div>'
                f'{steps_list(result.steps)}</div>{spool}')


def message(title: str, text: str, bad: bool = False) -> str:
    return page(title, f'<h1 class="{"bad" if bad else ""}">{escape(title)}</h1>'
                f'<div class="card">{escape(text)}</div>')


def printer_grid(printer: Printer, lanes: dict[int, dict], error: Optional[str]) -> str:
    cards = []
    for t in range(printer.num_tools):
        lane = lanes.get(t)
        if lane and lane.get("spool_id"):
            body = (f'{swatch(lane.get("color"))}<b>T{t}</b>'
                    f'<small>#{lane["spool_id"]} {escape(lane.get("vendor_name") or "")} '
                    f'{escape(lane.get("material") or "")}</small>'
                    f'<small>{escape(lane.get("name") or "")}</small>'
                    f'<form method="post" action="/p/{escape(printer.slug)}/tools/{t}/clear" style="margin-top:6px">'
                    f'<button>Clear</button></form>')
        elif lane:
            body = (f'{swatch(lane.get("color"))}<b>T{t}</b>'
                    f'<small>{escape(lane.get("material") or "")} (no spool id)</small>')
        else:
            body = f'<b>T{t}</b><small>empty</small>'
        cards.append(f'<div class="tool">{body}</div>')
    err = f'<div class="card bad">{escape(error)}</div>' if error else ""
    return f'<h2>{escape(printer.name)}</h2>{err}<div class="tools">{"".join(cards)}</div>'


def status(grids: list[str], locations: list[tuple[Location, int]], pending_text: str, recent) -> str:
    pend = ""
    if pending_text:
        pend = (f'<div class="card">Waiting for a second scan after {escape(pending_text)}. '
                f'<form method="post" action="/pending/clear" style="display:inline">'
                f'<button>Cancel</button></form></div>')
    if not grids:
        grids = ['<div class="card">No printers yet. <a href="/settings">Add one in Settings</a>.</div>']
    locs = ""
    if locations:
        items = "".join(f'<li><a href="/locations/{escape(l.slug)}">{escape(l.name)}</a>'
                        f' <span class="dim">{n if n >= 0 else "?"} spool{"" if n == 1 else "s"}</span></li>'
                        for l, n in locations)
        locs = f'<h2>Storage</h2><div class="card"><ul class="plain">{items}</ul></div>'
    rows = "".join(
        f'<tr><td class="dim">{time.strftime("%m-%d %H:%M", time.localtime(ts))}</td>'
        f'<td>{escape(action)}</td><td>{escape(target or "")}</td>'
        f'<td>{"#" + str(sid) if sid else ""} {escape(detail or "")}</td></tr>'
        for ts, sid, target, action, detail in recent)
    log = f'<h2>Recent</h2><div class="card"><table>{rows}</table></div>' if rows else ""
    return page("Spooltag", f'{pend}{"".join(grids)}{locs}{log}', refresh=30)


def qr_svg(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=3, border=2, dark="#000", light="#fff")


def _tag_card(url: str, title: str, sub: str = "", q: str = "") -> str:
    qattr = f' data-q="{escape(q)}"' if q else ""
    return (f'<div class="card tag"{qattr}>{qr_svg(url)}<div><b>{title}</b>'
            f'{"<br><span class=dim>" + sub + "</span>" if sub else ""}<br><code>{escape(url)}</code>'
            f'<br><button class="copy" data-url="{escape(url)}">Copy URL</button></div></div>')


def tags(base: str, printers: list[Printer], locations: list[Location], spools: list[dict],
         error: Optional[str]) -> str:
    parts = ['<h1>Tag URLs</h1><div class="card"><b>Writing a tag</b><ol style="margin:.4em 0 0;padding-left:1.3em">'
             '<li>Tap <b>Copy URL</b> (on the phone), or scan the QR code with the phone\'s camera and copy the link.</li>'
             '<li>In NFC Tools: <b>Write → Add a record → Custom URL</b>, paste, OK.</li>'
             '<li>Tap <b>Write</b> and hold the phone to the tag.</li></ol>'
             '<p class="dim" style="margin:.5em 0 0">Tool and storage tags are written once; spool tags '
             'whenever a box gets a new spool.</p></div>']
    for p in printers:
        cards = "".join(_tag_card(f"{base}/t/{p.slug}/{t}", f"{escape(p.name)} T{t}") for t in range(p.num_tools))
        parts.append(f'<h2>{escape(p.name)}</h2><details open><summary>{p.num_tools} tool tag'
                     f'{"" if p.num_tools == 1 else "s"}</summary>{cards}</details>')
    if locations:
        cards = "".join(_tag_card(f"{base}/l/{l.slug}", escape(l.name)) for l in locations)
        parts.append(f'<h2>Storage</h2>{cards}')
    spool_cards = "".join(
        _tag_card(f"{base}/s/{s['id']}",
                  f'{swatch((s.get("filament") or {}).get("color_hex"))}#{s["id"]} {escape(spool_label(s))}',
                  escape(s.get("location") or ""),
                  (spool_label(s) + " " + str(s["id"]) + " " + (s.get("location") or "")).lower())
        for s in sorted(spools, key=lambda s: s["id"]))
    err = f'<div class="card bad">{escape(error)}</div>' if error else ""
    script = ("<script>document.getElementById('q').addEventListener('input',e=>{const v=e.target.value.toLowerCase();"
              "document.querySelectorAll('[data-q]').forEach(c=>c.style.display=c.dataset.q.includes(v)?'':'none')});"
              # navigator.clipboard needs HTTPS; on plain-http LAN pages fall back to execCommand.
              "function copyText(t){if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(t);"
              "const a=document.createElement('textarea');a.value=t;a.setAttribute('readonly','');a.style.position='fixed';"
              "a.style.opacity='0';document.body.appendChild(a);a.select();a.setSelectionRange(0,t.length);"
              "const ok=document.execCommand('copy');a.remove();return ok?Promise.resolve():Promise.reject()}"
              "document.querySelectorAll('button.copy').forEach(b=>b.addEventListener('click',()=>{"
              "copyText(b.dataset.url).then(()=>{b.textContent='Copied ✓';setTimeout(()=>b.textContent='Copy URL',1500)},"
              "()=>{b.textContent='Long-press the URL to copy'})}))</script>")
    parts.append(f'<h2>Spools</h2>{err}<input id="q" placeholder="Filter spools…">{spool_cards}{script}')
    return page("Tags", "".join(parts))


def settings(printers: list[Printer], locations: list[Location], notice: str = "", error: str = "") -> str:
    msg = (f'<div class="card bad">{escape(error)}</div>' if error else
           f'<div class="card ok">{escape(notice)}</div>' if notice else "")

    def printer_form(p: Optional[Printer]) -> str:
        new = p is None
        p = p or Printer("", "", "", "", 1, False, False)
        slug_field = ('<label>Short id for tag URLs (optional)</label>'
                      '<input name="slug" placeholder="made from the name, e.g. v0" pattern="[a-z0-9][a-z0-9-]*">'
                      if new else f'<input type="hidden" name="slug" value="{escape(p.slug)}">'
                      '<input type="hidden" name="mode" value="edit">')
        chk = lambda on: " checked" if on else ""  # noqa: E731
        return (f'<form method="post" action="/settings/printers">'
                f'<div class="grid2"><div><label>Name</label><input name="name" required value="{escape(p.name)}"'
                f' placeholder="Voron V0"></div><div><label>Tools</label><input name="num_tools" type="number"'
                f' min="1" max="32" required value="{p.num_tools}"></div></div>'
                f'<label>Moonraker URL</label><input name="moonraker_url" required value="{escape(p.moonraker_url)}"'
                f' placeholder="http://10.10.2.50:7125">'
                f'<label>Moonraker API key (only if this server isn\'t a trusted client)</label>'
                f'<input name="api_key" value="{escape(p.api_key)}">{slug_field}'
                f'<label class="check"><input type="checkbox" name="save_variables"{chk(p.save_variables)}>'
                f'SAVE_VARIABLE t&lt;n&gt;__spool_id (needs [save_variables])</label>'
                f'<label class="check"><input type="checkbox" name="macro_variable"{chk(p.macro_variable)}>'
                f'SET_GCODE_VARIABLE on T&lt;n&gt; macros (needs variable_spool_id)</label>'
                f'<div style="margin-top:10px"><button class="primary">{"Add printer" if new else "Save"}</button></div>'
                f'</form>')

    plist = "".join(
        f'<div class="card"><div class="row"><div><b>{escape(p.name)}</b> <span class="dim">'
        f'{p.num_tools} tool{"" if p.num_tools == 1 else "s"} · tags /t/{escape(p.slug)}/&lt;n&gt;</span></div>'
        f'<form method="post" action="/settings/printers/{escape(p.slug)}/delete" '
        f'onsubmit="return confirm(\'Remove {escape(p.name)}? Its tool tags stop working.\')">'
        f'<button class="danger">Remove</button></form></div>'
        f'<details><summary>Edit</summary>{printer_form(p)}</details></div>'
        for p in printers)
    llist = "".join(
        f'<li class="row"><span>{escape(l.name)} <span class="dim">/l/{escape(l.slug)}</span></span>'
        f'<form method="post" action="/settings/locations/{escape(l.slug)}/delete" '
        f'onsubmit="return confirm(\'Remove {escape(l.name)}? Its tag stops working.\')">'
        f'<button class="danger">Remove</button></form></li>'
        for l in locations)
    return page("Settings", f'<h1>Settings</h1>{msg}'
                f'<h2>Printers</h2>{plist}<div class="card"><h3>Add a printer</h3>{printer_form(None)}</div>'
                f'<h2>Storage locations</h2>'
                f'<p class="dim">The name is written to Spoolman as the spool\'s location, exactly as typed.</p>'
                f'{"<div class=card><ul class=plain>" + llist + "</ul></div>" if llist else ""}'
                f'<div class="card"><h3>Add a location</h3><form method="post" action="/settings/locations">'
                f'<label>Name</label><input name="name" required placeholder="Dry box 1">'
                f'<label>Short id for the tag URL (optional)</label>'
                f'<input name="slug" placeholder="made from the name, e.g. dry-box-1" pattern="[a-z0-9][a-z0-9-]*">'
                f'<div style="margin-top:10px"><button class="primary">Add location</button></div></form></div>'
                f'<p class="dim">Short ids are fixed once created because they\'re written onto tags. '
                f'To change one, remove it, add it again and rewrite the tag.</p>')
