#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
points_farmer  —  Collecte les Channel Points Kick en idle sur UNE chaine choisie.

POURQUOI un vrai navigateur (verifie empiriquement) :
  Kick ne credite les points QUE si un VRAI player lit le stream NON-MUTE.
  La presence WebSocket seule (ce que fait le xp-farmer) ne credite RIEN — teste
  13 min : 0 point. Seule la lecture video reelle credite (anti-AFK Kick).
  Donc on pilote un Chrome reel qui lit le flux ; le navigateur gere tout seul
  les handshakes WebSocket / watch-events. On garde juste video.muted=false.

CE QU'IL A FALLU pour que ca marche en headless :
  - VRAI Chrome (pas le Chromium bundle Playwright) -> passe l'anti-bot Cloudflare/Kasada.
  - --headless=new -> rend la video (l'ancien headless ne monte pas le player).
  - --mute-audio -> aucun son reel, mais video.muted reste false (Mux rapporte non-mute).
  - Injection du header 'Authorization: Bearer <session_token>' sur les requetes API
    (le front l'ajoute depuis le localStorage ; un contexte neuf ne l'a pas -> 403/anonyme).
  - Set complet de cookies (kick_cookies.json) + cookie de consentement.

Config (config.json) :
  "points_channel":       "iceposeidon"    # slug a farmer (OBLIGATOIRE, override env POINTS_CHANNEL)
  "points_headless":      true             # false = fenetre visible (debug)
  "points_poll_interval": 60               # secondes entre logs du solde
  "points_cookies_file":  "kick_cookies.json"
  "points_chrome_path":   "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
"""
import json, os, sys, time, logging, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))

# Mode multi-compte : le launcher passe channel / cookies / log par variables
# d'env, pour qu'un worker = un (compte, channel) totalement isole. Sans env,
# repli sur config.json (mode mono-compte historique, inchange).
SLUG = os.environ.get("POINTS_CHANNEL") or CFG.get("points_channel")
if not SLUG:
    sys.exit("[ERROR] 'points_channel' manquant (config.json ou env POINTS_CHANNEL).")
HEADLESS = CFG.get("points_headless", True)
POLL = int(os.environ.get("POINTS_POLL_INTERVAL") or CFG.get("points_poll_interval", 60))
COOKIES_FILE = os.environ.get("KICK_COOKIES_FILE") or \
    os.path.join(HERE, CFG.get("points_cookies_file", "kick_cookies.json"))
LOG_FILE = os.environ.get("POINTS_LOG_FILE") or \
    os.path.join(HERE, CFG.get("points_log_file", "points.log"))
CHROME = CFG.get("points_chrome_path", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def _bearer_from_cookies(path):
    # Le token est dans le cookie 'session_token' de l'export -> 1 secret/compte.
    try:
        for c in json.load(open(path, encoding="utf-8")):
            if c.get("name") == "session_token" and c.get("value"):
                return urllib.parse.unquote(c["value"])
    except Exception:
        pass
    return None


BEARER = _bearer_from_cookies(COOKIES_FILE)
# Repli sur config.json UNIQUEMENT en mono-compte. En multi-compte (launcher), un
# cookies casse ne doit PAS farmer avec le token d'un autre compte -> on echoue net.
if not BEARER and not os.environ.get("KICK_COOKIES_FILE"):
    BEARER = urllib.parse.unquote(CFG.get("session_token", ""))
if not BEARER:
    sys.exit("[ERROR] session_token introuvable dans les cookies du compte (export invalide/expire).")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("points")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}

# Garde la lecture non-mutee + lit l'etat du player et le solde de points.
KEEP_ALIVE_JS = """
async (slug) => {
  const v = document.querySelector('video');
  if (v) { try { v.muted = false; if (!v.volume) v.volume = 0.08;
                 if (v.paused) v.play().catch(()=>{}); } catch(e){} }
  let pts = null, err = null;
  try {
    const ac = new AbortController(); const to = setTimeout(()=>ac.abort(), 8000);
    const r = await fetch('/api/v2/channels/'+slug+'/points',
              {headers:{Accept:'application/json'}, credentials:'include', signal:ac.signal});
    clearTimeout(to);
    if (r.ok) pts = (await r.json())?.data?.points; else err = 'http'+r.status;
  } catch(e){ err = String(e).slice(0,30); }
  return { has: !!v, paused: v? v.paused : null, t: v? Math.round(v.currentTime) : null,
           muted: v? v.muted : null, points: pts, err };
}
"""


def load_cookies():
    raw = json.load(open(COOKIES_FILE, encoding="utf-8"))
    return [{"name": c["name"], "value": c["value"], "domain": c["domain"],
             "path": c.get("path", "/"), "secure": bool(c.get("secure", False)),
             "httpOnly": bool(c.get("httpOnly", False)),
             "sameSite": _SAMESITE.get(str(c.get("sameSite", "")).lower(), "Lax")} for c in raw]


def dismiss_consent(page):
    for lbl in ("Accept all", "Accept All", "I agree", "Got it"):
        try:
            btn = page.get_by_text(lbl, exact=False).first
            if btn and btn.is_visible():
                btn.click(timeout=2000)
                return
        except Exception:
            pass


def run():
    from playwright.sync_api import sync_playwright
    src = "env POINTS_CHANNEL (via UI)" if os.environ.get("POINTS_CHANNEL") else "config.json points_channel"
    log.info("=" * 55)
    log.info(f"points_farmer (Chrome) — chaine: {SLUG}  [source: {src}] | headless={HEADLESS}")
    log.info("=" * 55)
    cookies = load_cookies()
    args = ["--mute-audio", "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled", "--window-size=1300,800"]
    if HEADLESS:
        args.insert(0, "--headless=new")  # vrai navigateur, fenetre non rendue

    def _route(route):
        req = route.request
        if any(d in req.url for d in ("api.kick.com", "web.kick.com", "kick.com/api", "websockets.kick.com")):
            h = dict(req.headers); h["authorization"] = f"Bearer {BEARER}"
            route.continue_(headers=h)
        else:
            route.continue_()

    with sync_playwright() as pw:
        # Vrai Chrome requis (passe l'anti-bot). Fallback sur le canal "chrome" installe.
        if os.path.exists(CHROME):
            browser = pw.chromium.launch(headless=False, executable_path=CHROME, args=args)
        else:
            log.warning(f"Chrome introuvable a '{CHROME}' — fallback channel=chrome.")
            try:
                browser = pw.chromium.launch(headless=False, channel="chrome", args=args)
            except Exception as e:
                sys.exit(f"[ERROR] Google Chrome introuvable. Installe-le ou renseigne "
                         f"'points_chrome_path' dans config.json. ({str(e)[:80]})")

        def new_page():
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            ctx.add_cookies(cookies)
            ctx.route("**/*", _route)
            return ctx, ctx.new_page()

        ctx, page = new_page()
        start_pts = None
        last = None
        fails = 0
        while True:
            try:
                log.info(f"Chargement kick.com/{SLUG} ...")
                page.goto(f"https://kick.com/{SLUG}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                dismiss_consent(page)
                page.wait_for_timeout(4000)
                page.evaluate("""()=>{const v=document.querySelector('video');
                    if(v){v.muted=false;v.volume=0.08;v.play().catch(()=>{});}}""")
                offline_polls = 0
                while True:
                    st = page.evaluate(KEEP_ALIVE_JS, SLUG)
                    if not st["has"]:
                        offline_polls += 1
                        log.warning(f"Pas de player (chaine offline ?) [{offline_polls}]")
                        if offline_polls >= 3:
                            log.info("Chaine probablement offline — reload dans 3 min.")
                            time.sleep(180)
                            break
                        page.wait_for_timeout(15000)
                        continue
                    offline_polls = 0
                    if st["points"] is not None:
                        if start_pts is None:
                            start_pts = st["points"]
                            log.info(f"Solde initial {SLUG}: {start_pts} points")
                        tick = (st["points"] - last) if last is not None else 0
                        last = st["points"]
                        log.info(f"{SLUG}: {st['points']} pts (+{tick} | +{st['points']-start_pts} session) "
                                 f"| play={not st['paused']} muted={st['muted']} t={st['t']}s")
                    else:
                        log.warning(f"Solde illisible (err={st['err']}) | play={not st['paused']} t={st['t']}s")
                    if st["paused"]:
                        log.warning("Player en pause — relance.")
                    fails = 0
                    page.wait_for_timeout(POLL * 1000)
            except KeyboardInterrupt:
                break
            except Exception as e:
                fails += 1
                log.warning(f"Erreur [{fails}]: {str(e)[:120]} — reload dans 15s")
                time.sleep(15)
                if fails >= 3:  # page/contexte probablement morts -> on les recree
                    log.warning("Recreation du contexte navigateur.")
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    try:
                        ctx, page = new_page()
                        fails = 0
                    except Exception as e2:
                        log.error(f"Echec recreation contexte: {str(e2)[:120]}")
                        time.sleep(30)
        browser.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Arret.")
