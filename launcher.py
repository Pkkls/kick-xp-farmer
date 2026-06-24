#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher.py  —  Launcher Kick Points MULTI-COMPTE.

Un user solo branche 1 compte et farm. Le meme outil scale a N comptes : chaque
compte = un worker points_farmer isole (son Chrome, ses cookies, son channel).
Le superviseur :
  - lance/arrete les workers selon les comptes actives,
  - plafonne le nombre de Chrome simultanes (max_concurrent) car chaque Chrome
    est lourd (lecture video reelle obligatoire pour crediter les points),
  - si tu as plus de comptes que de slots, fait TOURNER (rotation temporelle)
    pour que tous farment a tour de role,
  - relance automatiquement un worker qui crashe (backoff),
  - demarre les Chrome en escalier (1 par tick) pour ne pas saturer le CPU.

Dashboard web local : http://127.0.0.1:8780  (python launcher.py).

Tout est local. Les cookies/tokens restent sur la machine (accounts/, gitignore).
Stdlib (http.server) + curl_cffi (deja requis). Reutilise points_farmer.py.
"""
import os, re, sys, json, time, threading, subprocess, webbrowser, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

import accounts as acc

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
FARMER = os.path.join(HERE, "points_farmer.py")
XP_FARMER = os.path.join(HERE, "farmer.py")          # XP par compte : choisit seul un stream live
XP_STATE_FILE = os.path.join(HERE, "data", "xp_enabled.json")   # set persiste des comptes XP actifs
LOGS_DIR = os.path.join(HERE, "accounts")

PORT        = int(CFG.get("launcher_port", 8780))
MAX_CONC    = max(1, min(int(CFG.get("max_concurrent", 5)), 32))   # garde-fou footgun
STAGGER     = max(1, int(CFG.get("stagger_seconds", 4)))           # 1 nouveau Chrome / tick
ROTATE_MIN  = int(CFG.get("rotate_minutes", 30))                   # 0 = pas de rotation
BACKOFF     = max(3, int(CFG.get("restart_backoff", 10)))
POLL        = max(15, int(CFG.get("launcher_poll", 45)))           # poll soldes (s)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
SLUG_RE = re.compile(r"[a-z0-9_]{1,30}")

# ── Etat partage ───────────────────────────────────────────────────────────
_workers = {}      # acc_id -> {proc, job, started, restarts, backoff_until, channel, quarantined}
_points = {}       # acc_id -> {channel, start, points, ts}
# ponytail: _points est en RAM -> la colonne "session" repart de 0 au redemarrage
# du launcher. Pour un historique/graph de gains, persister un JSONL par compte
# (accounts/<id>.points.jsonl) dans poll_points et recharger au boot. Reporte (YAGNI v1).
_hist = {}         # acc_id -> deque[int]  (derniers soldes, pour la sparkline)
_events = deque(maxlen=80)   # flux d'activite (le plus recent en tete)
_login = {"active": False, "status": "idle", "msg": "", "label": ""}   # flux "Log in with Kick"
_xp_workers = {}   # acc_id -> {proc, job, started}  (un farmer.py XP par compte)
_wlock = threading.Lock()
_start_ts = time.time()
_shutdown = threading.Event()


def _emit(kind, label, msg):
    """Ajoute un evenement au flux d'activite du dashboard."""
    _events.appendleft({"ts": time.time(), "kind": kind, "label": label, "msg": msg})

QUARANTINE = max(2, int(CFG.get("quarantine_restarts", 8)))    # crash-loop -> on lache le slot
MEM_FLOOR  = int(CFG.get("mem_floor_mb", 1500)) * 1024 * 1024   # pas de demarrage sous ce seuil RAM
STAGGER_START = 1.5                                             # s entre 2 demarrages d'un meme tick

try:
    from curl_cffi import requests as _cffi
    _HTTP = _cffi.Session(impersonate="chrome136")
except ImportError:
    _HTTP = None

try:
    import psutil
except ImportError:
    psutil = None


# ── Job Object Windows : aucun Chrome ne survit au launcher ─────────────────
# Chaque worker (python -> Chrome) est place dans un Job KILL_ON_JOB_CLOSE.
# Si le launcher meurt (meme kill -9 / crash / BSOD), l'OS ferme le handle et
# tue tout l'arbre -> zero orphelin. TerminateJobObject tue un worker
# instantanement, sans taskkill bloquant (ne gele plus le superviseur en rotation).
_WIN = sys.platform == "win32"
if _WIN:
    import ctypes
    from ctypes import wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _JOB_KILL_ON_CLOSE = 0x2000

    class _JOBLIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_void_p), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IOCOUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]

    class _EXTLIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBLIMIT), ("IoInfo", _IOCOUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _make_job(pid):
        try:
            job = _k32.CreateJobObjectW(None, None)
            info = _EXTLIMIT()
            info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
            _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            hp = _k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            _k32.AssignProcessToJobObject(job, hp)
            _k32.CloseHandle(hp)
            return job
        except Exception:
            return None
else:
    def _make_job(pid):
        return None


# ── Workers ────────────────────────────────────────────────────────────────
def _alive(w):
    return bool(w and w.get("proc") and w["proc"].poll() is None)


def _log_file(acc_id):
    return os.path.join(LOGS_DIR, f"{acc_id}.log")


def _mem_ok():
    if psutil is None:
        return True
    try:
        return psutil.virtual_memory().available > MEM_FLOOR
    except Exception:
        return True


def _kill(w):
    """Tue un worker + tout son arbre Chrome. Non bloquant (ne gele pas le lock)."""
    if not w:
        return
    job, proc = w.get("job"), w.get("proc")
    if _WIN and job:
        try:
            _k32.TerminateJobObject(job, 1)   # tue l'arbre, instantane
        finally:
            try:
                _k32.CloseHandle(job)
            except Exception:
                pass
        w["job"] = None
        return
    if proc and proc.poll() is None:
        if _WIN:
            subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(proc.pid)])  # detache, sans wait
        else:
            proc.terminate()


def _start(account):
    os.makedirs(LOGS_DIR, exist_ok=True)
    aid, channel = account["id"], account["channel"]
    env = dict(os.environ,
               POINTS_CHANNEL=channel,
               KICK_COOKIES_FILE=account["cookies_file"],
               POINTS_LOG_FILE=_log_file(aid))
    # stderr du worker -> son log, sinon une erreur fatale (Chrome absent, token
    # mort) part dans le vide et le dashboard ne peut rien expliquer.
    errf = open(_log_file(aid), "ab")
    proc = subprocess.Popen([sys.executable, "-u", FARMER], cwd=HERE, env=env,
                            stdout=subprocess.DEVNULL, stderr=errf)
    errf.close()
    prev = _workers.get(aid, {})
    _workers[aid] = {"proc": proc, "job": _make_job(proc.pid), "started": time.time(),
                     "restarts": prev.get("restarts", 0), "backoff_until": 0,
                     "channel": channel, "quarantined": False}
    # reset du compteur de session si on (re)demarre sur un autre channel
    p = _points.get(aid)
    if not p or p.get("channel") != channel:
        _points[aid] = {"channel": channel, "start": None, "points": None, "ts": 0}
    _emit("start", account.get("label", aid), f"farming {channel}")


def _stop(aid):
    w = _workers.pop(aid, None)
    if w:
        _kill(w)


# ── XP Farmer par compte (un farmer.py par compte, choisit seul un stream live) ─
# Chaque worker farme le XP du compte (token via env). farmer.py ecrit son etat
# (stream live selectionne, level, XP/min) dans accounts/<id>.xp.json -> l'UI sait
# exactement quel stream chaque compte regarde. Aucun choix de streamer (auto live).
def _xp_enabled_set():
    try:
        return set(json.load(open(XP_STATE_FILE, encoding="utf-8")))
    except Exception:
        return set()


def _xp_save_enabled(s):
    os.makedirs(os.path.dirname(XP_STATE_FILE), exist_ok=True)
    tmp = XP_STATE_FILE + ".tmp"
    json.dump(sorted(s), open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, XP_STATE_FILE)


def _xp_status_file(aid):
    return os.path.join(LOGS_DIR, f"{aid}.xp.json")


def _xp_read_status(aid):
    try:
        return json.load(open(_xp_status_file(aid), encoding="utf-8"))
    except Exception:
        return {}


def _xp_running(aid):
    w = _xp_workers.get(aid)
    p = w and w.get("proc")
    return bool(p and p.poll() is None)


def _xp_launch(account):
    aid = account["id"]
    bearer = acc.bearer_of(account)
    if not bearer:
        _emit("xp", account.get("label", aid), "no token (re-import account)")
        return False
    os.makedirs(LOGS_DIR, exist_ok=True)
    env = dict(os.environ, XP_BEARER=bearer,
               XP_LOG_FILE=os.path.join(LOGS_DIR, f"{aid}.xp.log"),
               XP_STATUS_FILE=_xp_status_file(aid),
               XP_LABEL=account.get("label", aid))
    proc = subprocess.Popen([sys.executable, "-u", XP_FARMER], cwd=HERE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _xp_workers[aid] = {"proc": proc, "job": _make_job(proc.pid), "started": time.time()}
    return True


def _xp_stop(aid):
    w = _xp_workers.pop(aid, None)
    if w:
        _kill(w)


def _xp_set(aid, on):
    label = (acc.get(acc.load_accounts(), aid) or {}).get("label", aid)
    with _wlock:
        s = _xp_enabled_set()
        if on:
            s.add(aid)
            a = acc.get(acc.load_accounts(), aid)
            if a and not _xp_running(aid):
                _xp_launch(a)
        else:
            s.discard(aid)
            _xp_stop(aid)
        _xp_save_enabled(s)
    _emit("xp", label, "XP farming started" if on else "XP farming stopped")


def _xp_set_all(on):
    for a in acc.load_accounts():
        _xp_set(a["id"], on)


def _xp_state(accounts):
    """Etat XP par compte pour l'UI : on/running + statut (stream live, level, XP/min)."""
    enabled = _xp_enabled_set()
    rows, run_n = [], 0
    for a in accounts:
        aid = a["id"]
        on = aid in enabled
        run = _xp_running(aid)
        run_n += 1 if run else 0
        st = _xp_read_status(aid) if (on or run) else {}
        w = _xp_workers.get(aid, {})
        rows.append({
            "id": aid, "label": a.get("label", aid), "avatar": a.get("avatar"),
            "on": on, "running": run,
            "uptime": int(time.time() - w["started"]) if run and w.get("started") else 0,
            "status": st,
        })
    return {"accounts": rows, "enabled": len(enabled), "running": run_n}


def _window(enabled):
    """Sous-ensemble des comptes a faire tourner maintenant (rotation si besoin)."""
    ids = [a["id"] for a in enabled]
    if ROTATE_MIN <= 0:
        return set(ids[:MAX_CONC])           # pas de rotation : les MAX_CONC premiers
    if len(ids) <= MAX_CONC:
        return set(ids)
    # rotation : la fenetre glisse de MAX_CONC comptes tous les ROTATE_MIN min
    step = int((time.time() - _start_ts) // (ROTATE_MIN * 60))
    off = (step * MAX_CONC) % len(ids)
    return {ids[(off + i) % len(ids)] for i in range(MAX_CONC)}


def reconcile():
    """Aligne les workers sur l'etat voulu. Kills hors lock (non bloquant),
    remplit tous les slots libres en mini-escalier, garde-fou RAM, et met en
    quarantaine les comptes qui crashent en boucle (liberent leur slot)."""
    accounts = acc.load_accounts()
    enabled = [a for a in accounts if a.get("enabled") and a.get("channel")]
    desired = _window(enabled)
    by_id = {a["id"]: a for a in enabled}
    to_kill, to_start = [], []
    now = time.time()
    with _wlock:
        # 1) retire ceux qui ne doivent plus tourner (kill differe hors lock)
        for aid in [x for x in _workers if x not in desired]:
            to_kill.append(_workers.pop(aid))
        # 2) comptabilise les crashes -> backoff, ou quarantaine si crash-loop
        for aid, w in _workers.items():
            if not _alive(w) and aid in desired and not w.get("quarantined"):
                w["restarts"] += 1
                w["proc"] = None
                if w["restarts"] > QUARANTINE:
                    w["quarantined"] = True       # libere le slot, signale "en panne"
                    _emit("down", (by_id.get(aid) or {}).get("label", aid),
                          f"down after {w['restarts']} restarts")
                else:
                    w["backoff_until"] = now + min(BACKOFF * w["restarts"], 300)
        # 3) choisit les demarrages : tous les slots libres de la fenetre
        running = sum(1 for w in _workers.values() if _alive(w))
        for aid in desired:
            if running >= MAX_CONC:
                break
            w = _workers.get(aid)
            if _alive(w) or (w and (w.get("quarantined") or now < w.get("backoff_until", 0))):
                continue
            if aid in by_id:
                to_start.append(by_id[aid])
                running += 1
    # gros travail hors lock (kills non bloquants, demarrages en mini-escalier)
    for w in to_kill:
        _kill(w)
    for i, a in enumerate(to_start):
        if not _mem_ok():
            break
        with _wlock:
            _start(a)
        if i < len(to_start) - 1:
            _shutdown.wait(STAGGER_START)


def supervisor():
    while not _shutdown.wait(STAGGER):
        try:
            reconcile()
            # keepalive XP : relance le farmer.py d'un compte s'il a crash (loop de lancer.bat)
            enabled = _xp_enabled_set()
            if enabled:
                accs = {a["id"]: a for a in acc.load_accounts()}
                for aid in enabled:
                    if aid in accs and not _xp_running(aid):
                        with _wlock:
                            if aid in _xp_enabled_set() and not _xp_running(aid):
                                _xp_launch(accs[aid])
        except Exception:
            pass


# ── Soldes de points (par compte, via son propre token) ────────────────────
def _points_of(bearer, channel):
    if not (_HTTP and bearer and channel):
        return None
    try:
        r = _HTTP.get(f"https://kick.com/api/v2/channels/{channel}/points", timeout=8, headers={
            "Authorization": f"Bearer {bearer}", "Accept": "application/json",
            "User-Agent": UA, "Referer": "https://kick.com/", "Origin": "https://kick.com"})
        if r.status_code == 200:
            return (r.json() or {}).get("data", {}).get("points")
    except Exception:
        pass
    return None


def poll_points():
    while not _shutdown.wait(1):
        accounts = [a for a in acc.load_accounts() if a.get("enabled") and a.get("channel")]
        if accounts:
            def one(a):
                pts = _points_of(acc.bearer_of(a), a["channel"])
                if pts is None:
                    return
                aid = a["id"]
                rec = _points.setdefault(aid, {"channel": a["channel"], "start": None,
                                               "points": None, "ts": 0})
                prev = rec.get("points")
                if rec.get("channel") != a["channel"]:
                    rec.update(channel=a["channel"], start=None)
                    _hist.pop(aid, None)
                if rec["start"] is None:
                    rec["start"] = pts
                rec.update(points=pts, ts=time.time())
                _hist.setdefault(aid, deque(maxlen=30)).append(pts)
                if prev is not None and pts > prev:
                    _emit("points", a.get("label", aid), f"+{pts - prev} pts on {a['channel']}")
            with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as ex:
                list(ex.map(one, accounts))
        _shutdown.wait(POLL)


def last_log_line(acc_id, channel):
    try:
        with open(_log_file(acc_id), "rb") as f:
            tail = f.read()[-4000:].decode("utf-8", "replace").strip().splitlines()
        FAIL = ("403", "401", "token", "expir", "ERROR", "Chrome introuvable")
        for ln in reversed(tail):
            # les erreurs d'auth/lancement n'ont pas le slug -> on les laisse passer
            if channel and channel not in ln and not any(k in ln for k in FAIL):
                continue
            if (" pts (" in ln or "Solde" in ln or "offline" in ln or "Erreur" in ln
                    or any(k in ln for k in FAIL)):
                return ln
        return ""
    except Exception:
        return ""


# ── Vue d'etat pour le dashboard ───────────────────────────────────────────
def state():
    accounts = acc.load_accounts()
    desired = _window([a for a in accounts if a.get("enabled") and a.get("channel")])
    rows, run_n, pts_total, sess_total = [], 0, 0, 0
    for a in accounts:
        aid = a["id"]
        w = _workers.get(aid, {})
        running = _alive(w)
        run_n += 1 if running else 0
        p = _points.get(aid, {})
        pts = p.get("points")
        sess = (pts - p["start"]) if (pts is not None and p.get("start") is not None) else 0
        if pts:
            pts_total += pts
        sess_total += sess
        broken = bool(w.get("quarantined"))
        rows.append({
            "id": aid, "label": a.get("label", aid), "channel": a.get("channel", ""),
            "avatar": a.get("avatar"), "spark": list(_hist.get(aid, [])),
            "enabled": bool(a.get("enabled")), "running": running, "broken": broken,
            "queued": bool(a.get("enabled") and a.get("channel") and aid in desired
                           and not running and not broken),
            "no_channel": bool(a.get("enabled") and not a.get("channel")),
            "restarts": w.get("restarts", 0),
            "points": pts, "session": sess,
            "last_log": last_log_line(aid, a.get("channel", "")),
        })
    rows.sort(key=lambda r: (not r["running"], -(r["points"] or 0)))
    return {
        "accounts": rows,
        "totals": {"accounts": len(accounts), "running": run_n,
                   "points": pts_total, "session": sess_total},
        "config": {"max_concurrent": MAX_CONC, "rotate_minutes": ROTATE_MIN,
                   "slots_used": min(run_n, MAX_CONC)},
        "xp": _xp_state(accounts),
        "system": _system(),
        "events": [{"ts": e["ts"], "kind": e["kind"], "label": e["label"], "msg": e["msg"]}
                   for e in list(_events)[:40]],
    }


def _system():
    """RAM/CPU pour la jauge de ressources (psutil optionnel)."""
    if psutil is None:
        return None
    try:
        vm = psutil.virtual_memory()
        return {"ram_pct": round(vm.percent), "ram_used": round(vm.used / 1e9, 1),
                "ram_total": round(vm.total / 1e9, 1), "cpu_pct": round(psutil.cpu_percent(None))}
    except Exception:
        return None


# ── "Log in with Kick" : ouvre un vrai Chrome, l'utilisateur se connecte
#    lui-meme (on ne touche jamais au mot de passe), on capture les cookies. ──
def _login_flow(channel):
    from playwright.sync_api import sync_playwright
    args = ["--disable-blink-features=AutomationControlled", "--window-size=1180,820"]
    account = None
    try:
        with sync_playwright() as pw:
            if os.path.exists(CFG.get("points_chrome_path", "")):
                br = pw.chromium.launch(headless=False, executable_path=CFG["points_chrome_path"], args=args)
            else:
                br = pw.chromium.launch(headless=False, channel="chrome", args=args)
            ctx = br.new_context()
            pg = ctx.new_page()
            try:
                pg.goto("https://kick.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            _login.update(status="waiting", msg="Log into the account in the browser window…")
            deadline = time.time() + 240          # 4 min pour se connecter
            while time.time() < deadline and not _shutdown.is_set():
                try:
                    cookies = ctx.cookies()
                except Exception:
                    break                          # fenetre fermee par l'utilisateur
                if any(c.get("name") == "session_token" and c.get("value") for c in cookies):
                    time.sleep(2)                  # laisse les cookies anti-bot (Kasada) se poser
                    try:
                        cookies = ctx.cookies()
                    except Exception:
                        pass
                    norm = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                             "path": c.get("path", "/"), "secure": bool(c.get("secure")),
                             "httpOnly": bool(c.get("httpOnly")), "sameSite": c.get("sameSite", "Lax")}
                            for c in cookies]
                    account, err = acc.add_account(norm, channel=channel)
                    if not account:
                        _login.update(status="error", msg=err or "could not read the account")
                    break
                try:
                    pg.wait_for_timeout(1500)
                except Exception:
                    break
            try:
                br.close()
            except Exception:
                pass
    except Exception as e:
        _login.update(status="error", msg=str(e)[:140])
    finally:
        if account:
            _login.update(status="done", msg="Account added: " + account["label"], label=account["label"])
            _emit("add", account["label"], "added via Kick login")
        elif _login.get("status") not in ("error",):
            _login.update(status="error", msg="No login detected (timed out or window closed).")
        _login["active"] = False


def live_channels(account):
    """Channels live suivis par CE compte (pour le picker)."""
    b = acc.bearer_of(account)
    if not (_HTTP and b):
        return []
    try:
        r = _HTTP.get("https://kick.com/api/v2/channels/followed-page", timeout=10, headers={
            "Authorization": f"Bearer {b}", "Accept": "application/json", "User-Agent": UA,
            "Referer": "https://kick.com/", "Origin": "https://kick.com"})
        ch = r.json().get("channels", []) if r.status_code == 200 else []
    except Exception:
        ch = []
    live = [{"slug": c["channel_slug"], "name": c.get("user_username"),
             "viewers": c.get("viewer_count") or 0}
            for c in ch if c.get("is_live") and c.get("channel_slug")]
    live.sort(key=lambda c: -c["viewers"])
    return live


# ── Panel "Follows" : points par chaine sur TOUS les follows du compte ───────
# Generique: chaque compte voit SES propres follows (recuperes via son token) et
# SES propres points par chaine. Fetch en arriere-plan (490+ chaines), cache 5 min.
_follows = {}              # acc_id -> {status, items, total, with_pts, count, progress, ts}
_follows_lock = threading.Lock()


def _hdr(bearer):
    return {"Authorization": f"Bearer {bearer}", "Accept": "application/json",
            "User-Agent": UA, "Referer": "https://kick.com/", "Origin": "https://kick.com"}


def _follows_fetch(account):
    aid = account["id"]
    bearer = acc.bearer_of(account)
    try:
        sess = _cffi.Session(impersonate="chrome136") if "_cffi" in globals() and _cffi else None
        if sess is None:
            raise RuntimeError("curl_cffi requis pour le panel Follows")
        try:
            sess.get("https://kick.com/", headers={**_hdr(bearer), "Accept": "text/html"}, timeout=15)
        except Exception:
            pass

        # Pagination des follows du compte (l'API renvoie channel_slug + nextCursor).
        slugs, seen, cursor = [], set(), 0
        for _ in range(80):                                    # garde-fou
            r = sess.get(f"https://kick.com/api/v2/channels/followed?cursor={cursor}",
                         headers=_hdr(bearer), timeout=12)
            if r.status_code != 200:
                break
            d = r.json() or {}
            chans = d.get("channels", []) or []
            new = 0
            for c in chans:
                s = c.get("channel_slug")
                if s and s not in seen:
                    seen.add(s); slugs.append(s); new += 1
            nxt = d.get("nextCursor")
            if not chans or new == 0 or nxt in (None, "", 0):
                break
            cursor = nxt
            time.sleep(0.25)

        def one(slug):
            try:
                r = sess.get(f"https://kick.com/api/v2/channels/{slug}/points",
                             headers=_hdr(bearer), timeout=8)
                p = (r.json() or {}).get("data", {}).get("points") if r.status_code == 200 else None
            except Exception:
                p = None
            return {"slug": slug, "points": p}

        items, done = [], 0
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(one, s) for s in slugs]
            for f in as_completed(futs):
                items.append(f.result())
                done += 1
                cur = _follows.get(aid)
                if cur:
                    cur["progress"] = [done, len(slugs)]
        items.sort(key=lambda x: (x["points"] or -1), reverse=True)
        total = sum(i["points"] or 0 for i in items)
        with_pts = sum(1 for i in items if i["points"])
        _follows[aid] = {"status": "done", "items": items, "total": total, "with_pts": with_pts,
                         "count": len(items), "progress": [len(items), len(items)], "ts": time.time()}
    except BaseException as e:   # fetch_follows peut sys.exit (403) -> on capture aussi SystemExit
        _follows[aid] = {"status": "error", "msg": str(e)[:160] or "fetch failed",
                         "items": [], "total": 0, "ts": time.time()}


# ── Dashboard ──────────────────────────────────────────────────────────────
PAGE = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read()


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def _json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def _csrf_ok(self):
        # Exige un Origin local explicite. L'UI legitime (fetch same-origin) en
        # envoie toujours un sur POST; on refuse l'absence d'Origin (cross-site simple).
        return self.headers.get("Origin") in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, json.dumps(state()))
        if self.path == "/api/account/login":
            return self._send(200, json.dumps({k: _login.get(k) for k in ("active", "status", "msg", "label")}))
        if self.path.startswith("/api/channels"):
            qs = urllib.parse.urlparse(self.path).query
            aid = urllib.parse.parse_qs(qs).get("id", [""])[0]
            a = acc.get(acc.load_accounts(), aid)
            return self._send(200, json.dumps(live_channels(a) if a else []))
        if self.path.startswith("/api/follows"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            aid = qs.get("id", [""])[0]
            force = qs.get("refresh", [""])[0] == "1"
            a = acc.get(acc.load_accounts(), aid)
            if not a:
                return self._send(404, json.dumps({"error": "unknown account"}))
            with _follows_lock:
                cur = _follows.get(aid)
                if cur and cur.get("status") == "done" and not force and time.time() - cur["ts"] < 300:
                    return self._send(200, json.dumps(cur))
                if not cur or cur.get("status") != "loading":
                    _follows[aid] = {"status": "loading", "items": [], "total": 0,
                                     "progress": [0, 0], "ts": time.time()}
                    threading.Thread(target=_follows_fetch, args=(a,), daemon=True).start()
            return self._send(200, json.dumps(_follows.get(aid, {"status": "loading"})))
        self._send(404, "{}")

    def do_POST(self):
        if not self._csrf_ok():
            return self._send(403, json.dumps({"error": "origin"}))
        body = self._json()
        if self.path == "/api/account/add":
            channel = (body.get("channel") or "").strip().lower()
            if channel and not SLUG_RE.fullmatch(channel):
                return self._send(400, json.dumps({"error": "slug channel invalide"}))
            account, err = acc.add_account(body.get("cookies", ""), channel)
            if account:
                _emit("add", account["label"], "account added")
            return self._send(200 if account else 400,
                              json.dumps({"id": account["id"], "label": account["label"]} if account
                                         else {"error": err}))
        if self.path == "/api/account/login":
            channel = (body.get("channel") or "").strip().lower()
            if channel and not SLUG_RE.fullmatch(channel):
                return self._send(400, json.dumps({"error": "invalid channel slug"}))
            if _login.get("active"):
                return self._send(200, json.dumps({"started": False, "busy": True}))
            _login.update(active=True, status="opening", msg="Opening browser…", label="")
            threading.Thread(target=_login_flow, args=(channel,), daemon=True).start()
            return self._send(200, json.dumps({"started": True}))
        if self.path == "/api/account/update":
            ch = body.get("channel")
            if ch is not None:
                ch = ch.strip().lower()
                if ch and not SLUG_RE.fullmatch(ch):
                    return self._send(400, json.dumps({"error": "slug invalide"}))
            a, err = acc.update_account(body.get("id"), label=body.get("label"),
                                        channel=ch, enabled=body.get("enabled"))
            if a and body.get("enabled") is not None:
                _emit("toggle", a["label"], "enabled" if body["enabled"] else "disabled")
            elif a and ch is not None:
                _emit("channel", a["label"], f"channel -> {ch or '(none)'}")
            return self._send(200 if a else 404, json.dumps({"ok": bool(a), "error": err}))
        if self.path == "/api/account/remove":
            aid = body.get("id")
            lbl = (acc.get(acc.load_accounts(), aid) or {}).get("label", aid)
            # tout SOUS lock : stop puis suppression du compte, sinon un tick
            # reconcile() (qui prend le meme lock) pourrait relancer le worker
            # entre le stop et la disparition du compte de accounts.json (TOCTOU).
            with _wlock:
                _stop(aid)
                ok = acc.remove_account(aid)
            if ok:
                _emit("remove", lbl, "account removed")
            return self._send(200, json.dumps({"ok": ok}))
        if self.path == "/api/control":
            action = body.get("action")
            accs = acc.load_accounts()
            if action == "stop_all":
                for a in accs:
                    a["enabled"] = False
                _emit("control", "All", "stopped all accounts")
            elif action == "start_all":
                for a in accs:
                    a["enabled"] = True
                _emit("control", "All", "started all accounts")
            acc.save_accounts(accs)
            return self._send(200, json.dumps({"ok": True}))
        if self.path == "/api/xp":
            action = body.get("action")
            if action in ("start_all", "stop_all"):
                _xp_set_all(action == "start_all")
            else:
                aid = body.get("id")
                if not aid:
                    return self._send(400, json.dumps({"error": "id required"}))
                _xp_set(aid, action == "start")
            return self._send(200, json.dumps({"ok": True}))
        self._send(404, "{}")

    def log_message(self, *a):
        pass


def main():
    threading.Thread(target=supervisor, daemon=True).start()
    threading.Thread(target=poll_points, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    n = len(acc.load_accounts())
    print(f"Kick Points Launcher -> {url}   ({n} compte(s), max {MAX_CONC} simultanes)")
    print("Ctrl+C pour tout arreter.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown.set()
        with _wlock:
            for aid in list(_xp_workers):
                _xp_stop(aid)
            for aid in list(_workers):
                _stop(aid)
        srv.server_close()
        print("\nArret (tous les workers stoppes).")


if __name__ == "__main__":
    main()
