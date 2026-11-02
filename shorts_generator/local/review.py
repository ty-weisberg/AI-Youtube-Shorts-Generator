"""Clip review viewer: a small local web page to approve planned clips.

    python main.py review work/<video>

Opens http://127.0.0.1:8765 showing every clip in highlights.json with a
player that plays just that moment, a strip of preview frames, the transcript
lines, and the picker's reasons. Approve / Skip / a note are saved straight
back into highlights.json, so `python main.py render work/<video>` renders only
what was approved. Listens on localhost only; stdlib only.
"""
import json
import mimetypes
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict

from .workflow import FRAME_INTERVAL, STATUSES, _read_json, load_highlights, save_highlights, source_path

_lock = threading.Lock()


def _clip_view(clip: Dict, transcript: Dict, frame_interval: float, frame_count: int) -> Dict:
    start, end = float(clip["start_time"]), float(clip["end_time"])
    first = int(start // frame_interval) + 1
    last = min(frame_count, int(end // frame_interval) + 1)
    lines = [
        {"t": s["start"], "text": s["text"]}
        for s in transcript.get("segments", [])
        if s["end"] > start and s["start"] < end
    ]
    return {
        **clip,
        "frames": [{"file": f"{n:06d}.jpg", "t": (n - 1) * frame_interval} for n in range(first, last + 1)],
        "lines": lines,
    }


def _make_handler(work: Path):
    src = source_path(work)
    meta = _read_json(work / "source.json")
    frame_interval = float(meta.get("frame_interval", FRAME_INTERVAL))
    frames_dir = work / "frames"
    transcript = _read_json(work / "transcript.json")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: Dict = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/data":
                with _lock:
                    data = load_highlights(work)
                count = len(list(frames_dir.glob("*.jpg")))
                return self._json(200, {
                    "work": str(work),
                    "source": os.path.basename(src),
                    "clips": [_clip_view(c, transcript, frame_interval, count) for c in data["clips"]],
                })
            m = re.fullmatch(r"/frames/(\d{6}\.jpg)", path)
            if m:
                f = frames_dir / m.group(1)
                if f.exists():
                    return self._send(200, f.read_bytes(), "image/jpeg")
                return self._send(404, b"", "text/plain")
            if path == "/video":
                return self._video()
            self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        def _video(self):
            size = os.path.getsize(src)
            ctype = mimetypes.guess_type(src)[0] or "video/mp4"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            length = end - start + 1
            self.send_response(206 if m else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if m:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(src, "rb") as f:
                f.seek(start)
                left = length
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser cancelled to seek elsewhere

        def do_POST(self):
            m = re.fullmatch(r"/api/clip/([A-Za-z0-9_-]+)", self.path)
            if not m:
                return self._send(404, b"", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            with _lock:
                data = load_highlights(work)
                clip = next((c for c in data["clips"] if c["id"] == m.group(1)), None)
                if clip is None:
                    return self._json(404, {"error": "no such clip"})
                if "status" in body:
                    if body["status"] not in STATUSES:
                        return self._json(400, {"error": f"status must be one of {STATUSES}"})
                    clip["status"] = body["status"]
                if "note" in body:
                    clip["note"] = str(body["note"])[:2000]
                save_highlights(work, data)
            self._json(200, {"ok": True, "status": clip["status"], "note": clip["note"]})

    return Handler


def serve(work: Path, port: int = 8765, open_browser: bool = True) -> None:
    work = Path(work)
    load_highlights(work)  # fail early with the format hint if missing
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(work))
    url = f"http://127.0.0.1:{port}/"
    print(f"[review] {url}  (Ctrl+C to stop; choices save to {work / 'highlights.json'})", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--ok:#1f7a4d;--okbg:#e3f3ea;--skip:#8a8a84;--skipbg:#efefeb;--warn:#9a5b00;--warnbg:#fff2db;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecece8;--muted:#a3a39c;--line:#33332f;--ok:#5fd19a;--okbg:#18392a;--skip:#8f8f88;--skipbg:#2a2a27;--warn:#f0b357;--warnbg:#3a2c12;--accent:#7d9cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:14px 20px;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:18px;margin:0}.muted{color:var(--muted)}.count{font-variant-numeric:tabular-nums}
main{max-width:1100px;margin:0 auto;padding:20px;display:grid;gap:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:16px}
.card.approved{border-color:var(--ok);box-shadow:inset 4px 0 0 var(--ok)}.card.skipped{opacity:.6}
@media (max-width:760px){.card{grid-template-columns:1fr}}
video{width:100%;border-radius:8px;background:#000;aspect-ratio:16/9}
h2{font-size:17px;margin:0 0 4px}.meta{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;margin-left:6px;vertical-align:2px}
.b-pending{background:var(--skipbg);color:var(--muted)}.b-approved{background:var(--okbg);color:var(--ok)}.b-skipped{background:var(--skipbg);color:var(--skip)}
.flag{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:6px 10px;font-size:13px;margin-top:8px}
.why{margin:10px 0 0}.hook{font-style:italic;color:var(--muted);margin-top:4px}
.frames{display:flex;gap:4px;overflow-x:auto;margin-top:8px;padding-bottom:4px}.frames img{height:54px;border-radius:4px;cursor:pointer;flex:none}
.lines{max-height:170px;overflow:auto;font-size:13px;border-top:1px solid var(--line);margin-top:10px;padding-top:8px}
.lines div{padding:2px 0;cursor:pointer}.lines div:hover{color:var(--accent)}.lines b{font-weight:500;color:var(--muted);font-variant-numeric:tabular-nums;margin-right:6px}
.actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:7px 14px;cursor:pointer}
button.approve{border-color:var(--ok);color:var(--ok)}button.approve.on{background:var(--ok);color:var(--card)}
button.skip.on{background:var(--skip);color:var(--card);border-color:var(--skip)}
textarea{width:100%;font:inherit;font-size:14px;border:1px solid var(--line);border-radius:8px;padding:8px;background:var(--bg);color:var(--ink);margin-top:8px;resize:vertical;min-height:38px}
.saved{font-size:12px;color:var(--ok);height:16px}
</style></head><body>
<header><h1>Clip review</h1><span class="muted" id="src"></span><span class="count" id="counts"></span>
<span class="muted" style="margin-left:auto">Approve the clips to make, then tell Claude you're done.</span></header>
<main id="list"><p class="muted">Loading...</p></main>
<script>
const fmt=t=>{t=Math.max(0,t);const m=Math.floor(t/60),s=(t%60).toFixed(1).padStart(4,'0');return `${m}:${s}`};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let clips=[];
async function load(){const r=await fetch('/api/data');const d=await r.json();clips=d.clips;
 document.getElementById('src').textContent=d.source;render();}
function counts(){const n=s=>clips.filter(c=>c.status===s).length;
 document.getElementById('counts').textContent=`${n('approved')} approved · ${n('skipped')} skipped · ${n('pending')} to review`;}
function render(){const list=document.getElementById('list');list.innerHTML='';
 if(!clips.length){list.innerHTML='<p class="muted">No clips in highlights.json yet.</p>';return}
 clips.forEach(c=>{const el=document.createElement('section');el.className='card '+c.status;el.id='clip-'+c.id;
  const dur=c.end_time-c.start_time;
  el.innerHTML=`<div><video preload="metadata" controls src="/video#t=${c.start_time},${c.end_time}"></video>
   <div class="frames">${c.frames.map(f=>`<img loading="lazy" src="/frames/${f.file}" title="${fmt(f.t)}" data-t="${f.t}">`).join('')}</div></div>
   <div><h2>${esc(c.title)}<span class="badge b-${c.status}">${c.status}</span></h2>
   <div class="meta">${fmt(c.start_time)} to ${fmt(c.end_time)} · ${dur.toFixed(0)}s${c.score!=null?` · score ${esc(c.score)}`:''}</div>
   ${c.hook_sentence?`<div class="hook">"${esc(c.hook_sentence)}"</div>`:''}
   <p class="why">${esc(c.virality_reason)}</p>
   ${(c.flags||[]).map(f=>`<div class="flag">⚠ ${esc(f)}</div>`).join('')}
   <div class="lines">${c.lines.map(l=>`<div data-t="${l.t}"><b>${fmt(l.t)}</b>${esc(l.text)}</div>`).join('')}</div>
   <div class="actions"><button class="approve ${c.status==='approved'?'on':''}">Approve</button>
   <button class="skip ${c.status==='skipped'?'on':''}">Skip</button><button class="play">Play clip</button></div>
   <textarea placeholder="Note for Claude, e.g. start 5s earlier, end after the cuffs">${esc(c.note)}</textarea><div class="saved"></div></div>`;
  const v=el.querySelector('video');
  const seek=t=>{v.currentTime=Math.max(c.start_time,Math.min(t,c.end_time));v.play()};
  v.addEventListener('timeupdate',()=>{if(v.currentTime>=c.end_time)v.pause()});
  el.querySelector('.play').onclick=()=>seek(c.start_time);
  el.querySelectorAll('[data-t]').forEach(n=>n.onclick=()=>seek(+n.dataset.t));
  el.querySelector('.approve').onclick=()=>save(c,{status:c.status==='approved'?'pending':'approved'});
  el.querySelector('.skip').onclick=()=>save(c,{status:c.status==='skipped'?'pending':'skipped'});
  const ta=el.querySelector('textarea');ta.onblur=()=>{if(ta.value!==c.note)save(c,{note:ta.value},true)};
  list.appendChild(el);});counts();}
async function save(c,body,quiet){const r=await fetch('/api/clip/'+encodeURIComponent(c.id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 if(!r.ok){alert('Could not save: '+(await r.text()));return}const d=await r.json();c.status=d.status;c.note=d.note;
 if(quiet){const s=document.querySelector('#clip-'+c.id+' .saved');s.textContent='Note saved';setTimeout(()=>s.textContent='',1500);return}
 const el=document.getElementById('clip-'+c.id);el.className='card '+c.status;
 const b=el.querySelector('.badge');b.className='badge b-'+c.status;b.textContent=c.status;
 el.querySelector('.approve').classList.toggle('on',c.status==='approved');
 el.querySelector('.skip').classList.toggle('on',c.status==='skipped');counts();}
load();
</script></body></html>
"""
