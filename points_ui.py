#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
points_ui  —  UI locale pour choisir SUR QUEL channel farmer les Channel Points.

L'XP est global (n'importe quel live), mais les points sont LIES AU CHANNEL.
Donc le seul vrai levier = choisir le bon channel. Cette UI liste tes channels
suivis (live en premier, avec solde de points) et lance points_farmer.py sur
celui que tu cliques. Un seul farm actif a la fois (chaque farm = un vrai Chrome).

Lance: python points_ui.py   ->  ouvre http://127.0.0.1:8770

Stdlib only (http.server) + curl_cffi (deja dependance). Le token/cookies restent
cote serveur — le navigateur ne les voit jamais.
"""
import json, os, re, sys, time, urllib.parse, subprocess, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests as cffi

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
BEARER = urllib.parse.unquote(CFG["session_token"])
LOG = os.path.join(HERE, CFG.get("points_log_file", "points.log"))
PORT = int(CFG.get("points_ui_port", 8770))

_S = cffi.Session(impersonate="chrome136")
_H = {"Accept": "application/json", "Referer": "https://kick.com/", "Origin": "https://kick.com",
      "Authorization": f"Bearer {BEARER}",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0 Safari/537.36"}

# Etat du worker (single-user local -> globals suffisent).
_proc = None          # subprocess.Popen du points_farmer
_slug = None          # channel actuellement farme
_cache = {"t": 0, "data": []}   # cache followed-page (10s) pour ne pas marteler l'API
_wlock = threading.Lock()       # serialise start/stop worker (ThreadingHTTPServer = multi-thread)
_clock = threading.Lock()       # garde le cache contre le stampede


def points_of(slug):
    try:
        r = _S.get(f"https://kick.com/api/v2/channels/{slug}/points", headers=_H, timeout=8)
        if r.status_code == 200:
            return (r.json() or {}).get("data", {}).get("points")
    except Exception:
        pass
    return None


def list_channels():
    """Channels suivis, live d'abord, avec solde de points pour les live."""
    with _clock:
        if time.time() - _cache["t"] < 10 and _cache["data"]:
            return _cache["data"]
        _cache["t"] = time.time()  # stamp avant le fetch -> bloque le stampede concurrent
    try:
        r = _S.get("https://kick.com/api/v2/channels/followed-page", headers=_H, timeout=12)
        chans = r.json().get("channels", []) if r.status_code == 200 else []
    except Exception:
        chans = []
    out = [{"slug": c.get("channel_slug"), "name": c.get("user_username"),
            "avatar": c.get("profile_picture"), "live": bool(c.get("is_live")),
            "viewers": c.get("viewer_count") or 0, "cat": c.get("category_name") or "",
            "points": None} for c in chans if c.get("channel_slug")]
    # Solde uniquement pour les live (peu nombreux) -> liste reactive.
    live = [c for c in out if c["live"]]
    if live:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for c, p in zip(live, ex.map(lambda c: points_of(c["slug"]), live)):
                c["points"] = p
    out.sort(key=lambda c: (not c["live"], -c["viewers"]))
    _cache.update(t=time.time(), data=out)
    return out


def last_log_line(slug=None):
    try:
        with open(LOG, "rb") as f:
            tail = f.read()[-4000:].decode("utf-8", "replace").strip().splitlines()
        for ln in reversed(tail):
            # points.log est partage entre channels -> apres un switch, ignore les
            # lignes de l'ancien channel pour ne pas afficher un solde trompeur.
            if slug and slug not in ln:
                continue
            if " pts (" in ln or "Solde" in ln or "offline" in ln or "Erreur" in ln:
                return ln
        return "" if slug else (tail[-1] if tail else "")
    except Exception:
        return ""


def _kill(proc):
    if not (proc and proc.poll() is None):
        return
    if sys.platform == "win32":
        # Tue l'arbre complet (python -> node driver Playwright -> chrome headless).
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=8)  # attend la mort reelle avant de rendre la main
    except Exception:
        pass


def stop_worker():
    global _proc, _slug
    with _wlock:
        _kill(_proc)
        _proc, _slug = None, None


def persist_channel(slug):
    # Ecrit le choix dans config.json -> UI et lancement direct de points_farmer.py
    # restent coherents (sinon le farmer standalone garde l'ancien points_channel).
    try:
        path = os.path.join(HERE, "config.json")
        cfg = json.load(open(path, encoding="utf-8"))
        cfg["points_channel"] = slug
        json.dump(cfg, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception:
        pass


def start_worker(slug):
    global _proc, _slug
    with _wlock:
        _kill(_proc)
        persist_channel(slug)
        env = dict(os.environ, POINTS_CHANNEL=slug)
        _proc = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "points_farmer.py")],
                                 cwd=HERE, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _slug = slug


def status():
    global _slug
    alive = bool(_proc and _proc.poll() is None)
    if not alive and _slug:   # le worker est mort seul (crash) -> on nettoie l'etat
        _slug = None
    return {"farming": _slug, "last_log": last_log_line(_slug)}


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kick Points Farmer</title><style>
*{box-sizing:border-box}body{margin:0;background:#0e0e10;color:#efeff1;
font:14px/1.4 system-ui,Segoe UI,sans-serif}
header{padding:14px 20px;border-bottom:1px solid #2a2a2d;display:flex;
align-items:center;gap:14px;position:sticky;top:0;background:#0e0e10;z-index:2}
h1{font-size:16px;margin:0;font-weight:600}
#bar{margin-left:auto;font-size:13px;color:#adadb8}
#bar b{color:#53fc18}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
gap:12px;padding:18px}
.card{background:#18181b;border:1px solid #2a2a2d;border-radius:8px;padding:12px;
display:flex;gap:10px;align-items:center;cursor:pointer;transition:.12s}
.card:hover{border-color:#53fc18}
.card.active{border-color:#53fc18;box-shadow:0 0 0 1px #53fc18 inset}
.card.off{opacity:.5}
.av{width:42px;height:42px;border-radius:50%;object-fit:cover;flex:none;background:#2a2a2d}
.meta{min-width:0;flex:1}
.nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{font-size:12px;color:#adadb8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#53fc18;
margin-right:5px;vertical-align:middle}
.pts{color:#53fc18;font-variant-numeric:tabular-nums}
.row{padding:10px 18px;display:flex;gap:8px;align-items:center;border-bottom:1px solid #2a2a2d}
input{flex:1;background:#18181b;border:1px solid #2a2a2d;color:#efeff1;
padding:8px 10px;border-radius:6px;font:inherit}
button{background:#53fc18;color:#0e0e10;border:0;padding:8px 14px;border-radius:6px;
font-weight:700;cursor:pointer}button.ghost{background:#2a2a2d;color:#efeff1}
</style></head><body>
<header><h1>Kick Points Farmer</h1>
<div id="bar">chargement…</div></header>
<div class="row">
<input id="manual" placeholder="slug d'un channel (farm direct, meme hors suivis)…">
<button onclick="farmManual()">Farm</button>
<button class="ghost" onclick="stop()">Stop</button></div>
<div class="grid" id="grid"></div>
<script>
let active=null;
async function load(){
  const [ch,st]=await Promise.all([
    fetch('/api/channels').then(r=>r.json()),
    fetch('/api/status').then(r=>r.json())]);
  active=st.farming;
  document.getElementById('bar').innerHTML = active
    ? 'farm: <b>'+active+'</b> — '+(st.last_log||'…')
    : 'aucun farm actif';
  const g=document.getElementById('grid');
  g.innerHTML='';
  for(const c of ch){
    const d=document.createElement('div');
    d.className='card'+(c.slug===active?' active':'')+(c.live?'':' off');
    d.onclick=()=>farm(c.slug);
    d.innerHTML=`<img class="av" src="${c.avatar||''}" onerror="this.style.visibility='hidden'">
      <div class="meta"><div class="nm">${c.name||c.slug}</div>
      <div class="sub">${c.live?'<span class=dot></span>'+c.viewers.toLocaleString()+' • '+c.cat:'offline'}</div>
      <div class="sub pts">${c.points!=null?c.points.toLocaleString()+' pts':''}</div></div>`;
    g.appendChild(d);
  }
}
async function farm(slug){
  await fetch('/api/farm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slug})});
  active=slug; load();
}
function farmManual(){const v=document.getElementById('manual').value.trim();if(v)farm(v);}
async function stop(){await fetch('/api/stop',{method:'POST'});active=null;load();}
load();setInterval(load,5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/channels":
            self._send(200, json.dumps(list_channels()))
        elif self.path == "/api/status":
            self._send(200, json.dumps(status()))
        else:
            self._send(404, "{}")

    def _csrf_ok(self):
        # Bloque le CSRF: une page cross-site enverrait un Origin different du notre.
        origin = self.headers.get("Origin")
        return origin in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def do_POST(self):
        if not self._csrf_ok():
            return self._send(403, json.dumps({"error": "origin"}))
        if self.path == "/api/farm":
            slug = (self._body_json().get("slug") or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9_]{1,30}", slug):
                return self._send(400, json.dumps({"error": "slug invalide"}))
            start_worker(slug)
            self._send(200, json.dumps({"farming": slug}))
        elif self.path == "/api/stop":
            stop_worker()
            self._send(200, json.dumps({"farming": None}))
        else:
            self._send(404, "{}")

    def log_message(self, *a):
        pass  # silence


# ── Mode console (python points_ui.py --menu) ──────────────────────────────
FARMER = os.path.join(HERE, "points_farmer.py")


def _farm_foreground(slug):
    persist_channel(slug)
    env = dict(os.environ, POINTS_CHANNEL=slug)
    print(f"\n>>> Farm sur '{slug}' — Ctrl+C pour arreter et revenir au menu.\n")
    try:
        subprocess.run([sys.executable, "-u", FARMER], cwd=HERE, env=env)
    except KeyboardInterrupt:
        pass
    print("\n<<< Farm arrete.\n")


def console_menu():
    while True:
        print("=" * 48)
        print(" Kick Points Farmer")
        print("=" * 48)
        print("Recuperation des channels suivis...\n")
        live = [c for c in list_channels() if c["live"]]
        if not live:
            print("Aucun channel suivi en live actuellement.")
        for i, c in enumerate(live, 1):
            pts = f"{c['points']:>6} pts" if c["points"] is not None else "    ? pts"
            print(f"  {i:>2}) {c['slug']:<20} {pts}  {c['viewers']:>6} v  {c['cat']}")
        print("\n   m) slug manuel (n'importe quel channel)")
        print("   r) rafraichir la liste")
        print("   q) quitter")
        choix = input("\nChoix: ").strip().lower()
        if choix == "q":
            return
        elif choix == "r":
            continue
        elif choix == "m":
            slug = input("slug: ").strip().lower()
            if slug:
                _farm_foreground(slug)
        elif choix.isdigit() and 1 <= int(choix) <= len(live):
            _farm_foreground(live[int(choix) - 1]["slug"])
        else:
            print("Choix invalide.\n")


def serve_web():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Kick Points Farmer UI -> {url}  (Ctrl+C pour quitter)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_worker()
        srv.server_close()
        print("\nArret.")


if __name__ == "__main__":
    try:
        if "--menu" in sys.argv:
            console_menu()
        else:
            serve_web()
    except KeyboardInterrupt:
        print()
