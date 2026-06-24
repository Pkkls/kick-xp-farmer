#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kick-xp-farmer  —  Accumule du XP Kick en idle
Mechanism: subscription Pusher private-livestream.{id} avec Bearer token
"""
import json, time, threading, urllib.parse, datetime, sys, os, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cffi_requests
import websocket

# ── Config ────────────────────────────────────────────────────────────────
CFG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    if not os.path.exists(CFG_FILE):
        print(f"[ERROR] {CFG_FILE} introuvable. Copie config.example.json vers config.json et remplis-le.")
        sys.exit(1)
    with open(CFG_FILE, encoding="utf-8") as f:
        return json.load(f)

CFG = load_config()
# Token: env XP_BEARER (multi-compte, passe par le launcher) sinon config.json (standalone).
_TOKEN = os.environ.get("XP_BEARER") or CFG.get("session_token", "")
if not _TOKEN or "VOTRE" in _TOKEN or "TON" in _TOKEN:
    print("[ERROR] No XP token (env XP_BEARER or config.json session_token).")
    sys.exit(1)
BEARER = urllib.parse.unquote(_TOKEN)

# Fallback slugs si followed-page ne donne rien. Ordre de priorite :
#   1. "slug_pool" dans config.json
#   2. following.json (genere par fetch_follows.py : TOUS tes follows)
#   3. liste par defaut en dur
def _load_slug_pool():
    if CFG.get("slug_pool"):
        return CFG["slug_pool"]
    follows_file = os.path.join(os.path.dirname(__file__), "following.json")
    if os.path.exists(follows_file):
        try:
            with open(follows_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return [
        "kaicenat", "xqc", "trainwreckstv", "adin", "filian", "nickeh30",
        "hasanabi", "destiny", "amouranth", "dankquan", "caseoh", "tarik",
        "symfuhny", "ronaldo", "bassem", "roshtein", "nolimitbro", "lacy",
        "jynxzi", "rober", "suspxct", "dimasf6", "xposed", "n3on",
        "fl0m", "lurkinlucas", "zuckles", "mikesmithtv", "thelegend27",
        "bbcjb", "tee",
    ]

FALLBACK_SLUGS = _load_slug_pool()

PUSHER_KEY  = "32cbd69e4b950bf97679"
PUSHER_WS   = f"wss://ws-us2.pusher.com/app/{PUSHER_KEY}?protocol=7&client=js&version=8.5.0&flash=false"
XP_INTERVAL = CFG.get("xp_poll_interval", 120)   # secondes entre polls XP
PING_INTERVAL = 30                                 # ping Pusher
LOG_FILE    = os.environ.get("XP_LOG_FILE") or CFG.get("log_file", "farmer.log")

# Statut structuré (lu par le launcher pour l'UI). Absent en standalone (lancer.bat).
STATUS_FILE = os.environ.get("XP_STATUS_FILE")
_status = {}
def write_status(**kw):
    if not STATUS_FILE:
        return
    _status.update(kw)
    _status["ts"] = time.time()
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_status, f)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("farmer")

# Fix encoding Windows console
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── HTTP session ──────────────────────────────────────────────────────────
SESSION = cffi_requests.Session(impersonate="chrome136")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://kick.com/",
    "Origin": "https://kick.com",
    "Authorization": f"Bearer {BEARER}",
}

def cf_warmup():
    try:
        r = SESSION.get("https://kick.com/", headers={**HEADERS, "Accept": "text/html"}, timeout=15)
        log.info(f"CF warmup: {r.status_code}")
    except Exception as e:
        log.warning(f"CF warmup error: {e}")

# ── API ────────────────────────────────────────────────────────────────────

def get_level():
    try:
        r = SESSION.get("https://web.kick.com/api/v1/gamification/user/level", headers=HEADERS, timeout=15)
        if r.status_code == 200:
            d = r.json().get("data", {})
            return d if d.get("level") is not None else None
    except Exception as e:
        log.debug(f"get_level error: {e}")
    return None

def _channel_details(slug):
    """Retourne les détails d'un channel si en live, sinon None."""
    try:
        r = SESSION.get(f"https://kick.com/api/v1/channels/{slug}", headers=HEADERS, timeout=8)
        if r.status_code == 200:
            d = r.json()
            ls = d.get("livestream")
            if ls:
                return {
                    "id": ls["id"],
                    "slug": slug,
                    "chatroom_id": (d.get("chatroom") or {}).get("id"),
                    "viewers": ls.get("viewer_count", 0),
                }
    except Exception:
        pass
    return None

def find_live_stream(exclude_slug=None):
    """Cherche un stream live via les channels suivis, puis fallback slug pool."""
    # 1) Channels suivis — un seul appel, liste directe avec is_live
    try:
        r = SESSION.get("https://kick.com/api/v2/channels/followed-page", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            channels = r.json().get("channels", [])
            live = [c for c in channels if isinstance(c, dict) and c.get("is_live")
                    and c.get("channel_slug") != exclude_slug]
            if live:
                # Trie par viewers décroissant, prend le premier
                live.sort(key=lambda c: c.get("viewer_count", 0), reverse=True)
                slug = live[0]["channel_slug"]
                stream = _channel_details(slug)
                if stream:
                    return stream
    except Exception as e:
        log.debug(f"followed-page error: {e}")

    # 2) Fallback: pool custom configurée dans config.json
    pool = [s for s in FALLBACK_SLUGS if s != exclude_slug]
    if not pool:
        return None
    log.debug(f"Fallback: scan {len(pool)} slugs...")
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_channel_details, s): s for s in pool}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                for f in futures:
                    f.cancel()
                return result
    return None

def pusher_auth(socket_id, channel_name):
    payload = json.dumps({"socket_id": socket_id, "channel_name": channel_name}).encode()
    try:
        r = SESSION.post(
            "https://kick.com/broadcasting/auth",
            data=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            result = r.json()
            if "auth" in result:
                return result["auth"]
        log.debug(f"pusher_auth({channel_name}): {r.status_code}")
    except Exception as e:
        log.debug(f"pusher_auth error: {e}")
    return None

# ── Farmer state ──────────────────────────────────────────────────────────

class Farmer:
    def __init__(self):
        self.ws = None
        self.socket_id = None
        self.stream = None
        self.start_xp = None
        self.total_xp_gained = 0
        self.session_start = datetime.datetime.now()
        self.running = True
        self._lock = threading.Lock()
        self._xp_thread = None
        self._ping_thread = None

    # ── XP tracking ────────────────────────────────────────────────────

    def poll_xp(self):
        last_xp = self.start_xp
        poll_n = 0
        while self.running:
            time.sleep(XP_INTERVAL)
            if not self.running:
                break
            poll_n += 1
            lvl = get_level()
            if lvl:
                prog    = lvl.get("progress_xp", 0)
                to_next = lvl.get("xp_to_next_level", 0)
                level   = lvl.get("level", "?")
                tick    = prog - last_xp if last_xp is not None else 0
                since_start = prog - self.start_xp if self.start_xp is not None else 0
                elapsed_min = max((datetime.datetime.now() - self.session_start).seconds / 60, 0.1)
                rate    = since_start / elapsed_min  # XP/min
                eta_min = round(to_next / rate) if rate > 0 else "?"

                self.total_xp_gained += tick
                last_xp = prog

                write_status(level=level, progress_xp=prog, to_next=to_next,
                             since_start=since_start, xp_per_min=round(rate, 1),
                             eta_min=eta_min)
                log.info(
                    f"+{tick} XP  |  {to_next} XP restants  |  "
                    f"~{eta_min} min  (L{level} -> L{level+1 if isinstance(level,int) else '?'}  "
                    f"|  +{since_start} total  |  {rate:.1f} XP/min)"
                )
            else:
                write_status(state="error")
                log.warning(f"[XP #{poll_n}] Pas de donnees (token expire?)")

    # ── Pusher ─────────────────────────────────────────────────────────

    def ping_loop(self):
        while self.running and self.ws and self.ws.keep_running:
            time.sleep(PING_INTERVAL)
            try:
                if self.ws and self.ws.keep_running:
                    self.ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
            except Exception:
                break

    def subscribe(self, ws, stream):
        """Subscribe to private-livestream.{id} for the given stream."""
        channel = f"private-livestream.{stream['id']}"
        auth = pusher_auth(self.socket_id, channel)
        if auth:
            ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": auth, "channel": channel}}))
            log.info(f"Subscribe auth OK: {channel}")
        else:
            log.warning(f"Subscribe auth FAILED for {channel} — stream peut-etre hors ligne")
            return False
        # Public chatroom aussi
        if stream.get("chatroom_id"):
            pub_ch = f"chatroom.{stream['chatroom_id']}"
            ws.send(json.dumps({"event": "pusher:subscribe", "data": {"channel": pub_ch}}))
        return True

    def on_open(self, ws):
        log.info("WS: connecte")
        self.ws = ws

    def on_message(self, ws, message):
        try:
            msg = json.loads(message)
            event = msg.get("event", "")
            raw = msg.get("data", {})
            data = json.loads(raw) if isinstance(raw, str) else raw

            if event == "pusher:connection_established":
                self.socket_id = data.get("socket_id")
                log.info(f"WS: socket_id={self.socket_id}")
                if self.stream and self.socket_id:
                    self.subscribe(ws, self.stream)
                threading.Thread(target=self.ping_loop, daemon=True).start()

            elif "subscription_succeeded" in event:
                ch = msg.get("channel", "?")
                log.info(f"WS: subscription OK: {ch}")

            elif event == "pusher:error":
                log.warning(f"WS: erreur Pusher: {data}")
                # Stream probablement hors ligne — reconnect avec autre stream
                threading.Thread(target=self._handle_stream_offline, daemon=True).start()

        except Exception as e:
            log.debug(f"WS on_message error: {e}")

    def on_error(self, ws, error):
        log.warning(f"WS error: {error}")

    def on_close(self, ws, code, msg_close):
        log.info(f"WS ferme: code={code}")
        self.ws = None

    def _handle_stream_offline(self):
        """Le stream est hors ligne. Cherche un autre et reconnecte."""
        time.sleep(5)
        if not self.running:
            return
        log.info("Stream hors ligne, rotation...")
        # Exclut le slug actuel pour ne pas re-essayer immediatement
        current_slug = self.stream.get("slug") if self.stream else None
        new_stream = find_live_stream(exclude_slug=current_slug)
        if new_stream:
            write_status(state="watching", stream=new_stream["slug"], viewers=new_stream.get("viewers", 0))
            log.info(f"Nouveau stream: {new_stream['slug']} (id={new_stream['id']})")
            self.stream = new_stream
            # Ferme WS actuel et reconnecte
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
        else:
            log.warning("Aucun stream live trouve. Retry dans 5 min.")
            time.sleep(300)

    # ── Main loop ──────────────────────────────────────────────────────

    def run(self):
        log.info("=" * 55)
        log.info("kick-xp-farmer v1.0 — Production")
        log.info("=" * 55)

        cf_warmup()

        # Niveau initial
        lvl = get_level()
        if lvl:
            self.start_xp = lvl.get("progress_xp", 0)
            level = lvl.get("level", "?")
            to_next = lvl.get("xp_to_next_level", 0)
            total = self.start_xp + to_next
            pct = round(self.start_xp / total * 100, 1) if total else 0
            write_status(state="starting", level=level, progress_xp=self.start_xp, to_next=to_next)
            log.info(f"Niveau initial: L{level} | {self.start_xp}/{total} XP ({pct}%) | {to_next} XP restants")
        else:
            log.error("Impossible de recuperer le niveau. Verifie config.json (session_token).")
            sys.exit(1)

        # Poller XP en background
        self._xp_thread = threading.Thread(target=self.poll_xp, daemon=True)
        self._xp_thread.start()

        # Boucle principale: WS reconnect automatique
        while self.running:
            # Trouve un stream live
            log.info("Recherche stream live...")
            write_status(state="searching", stream=None, viewers=None)
            self.stream = find_live_stream()
            if not self.stream:
                write_status(state="offline")
                log.warning("Aucun stream live. Retry dans 5 min.")
                time.sleep(300)
                continue

            write_status(state="watching", stream=self.stream["slug"], viewers=self.stream.get("viewers", 0))
            log.info(f"Stream: {self.stream['slug']} (id={self.stream['id']}, {self.stream['viewers']} viewers)")

            # Visite la page pour simuler presence
            try:
                SESSION.get(
                    f"https://kick.com/{self.stream['slug']}",
                    headers={**HEADERS, "Accept": "text/html"},
                    timeout=10,
                )
            except Exception:
                pass

            # Connexion WS
            ws_app = websocket.WebSocketApp(
                PUSHER_WS,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                header={"Origin": "https://kick.com", "User-Agent": HEADERS["User-Agent"]},
            )
            ws_app.run_forever(ping_interval=0)

            if not self.running:
                break

            log.info("WS deconnecte. Reconnexion dans 10s...")
            time.sleep(10)

        # Bilan final
        elapsed = (datetime.datetime.now() - self.session_start)
        log.info("=" * 55)
        log.info(f"BILAN FINAL")
        log.info(f"  Duree: {elapsed}")
        log.info(f"  XP total gagne: +{self.total_xp_gained}")
        log.info("=" * 55)

    def stop(self):
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

# ── Entry point ────────────────────────────────────────────────────────────

farmer = None

def handle_signal(sig, frame):
    log.info("Arret demande (SIGINT)...")
    if farmer:
        farmer.stop()

signal.signal(signal.SIGINT, handle_signal)

if __name__ == "__main__":
    farmer = Farmer()
    try:
        farmer.run()
    except KeyboardInterrupt:
        farmer.stop()
