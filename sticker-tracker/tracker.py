#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cs2-sticker-tracker  —  Traque le prix de stickers CS2 sur le Steam Market
et envoie un rapport sur Telegram. Conçu pour tourner en autonomie via GitHub Actions.

Source de prix : Steam Community Market (API priceoverview, gratuite).
Notifications  : Bot Telegram (sendMessage).

Usage:
    python tracker.py            # run normal : fetch + rapport Telegram + historique
    python tracker.py --dry-run  # fetch + affichage console, sans Telegram ni écriture
    python tracker.py --no-telegram
"""
import os, sys, json, time, csv, argparse, datetime, urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE      = os.path.join(HERE, "config.json")
STICKERS_FILE = os.path.join(HERE, "stickers.json")
DATA_DIR      = os.path.join(HERE, "data")
HISTORY_FILE  = os.path.join(DATA_DIR, "history.json")
CSV_FILE      = os.path.join(DATA_DIR, "prices.csv")

# Steam currency codes (https://partner.steamgames.com)
CURRENCIES = {"USD": 1, "GBP": 2, "EUR": 3, "CHF": 4, "RUB": 5, "BRL": 7,
              "CAD": 20, "AUD": 21, "PLN": 6, "CNY": 23, "TRY": 17}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")


# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    """Config depuis config.json (si présent) surchargée par les variables d'env.

    Les secrets (token/chat_id) viennent en priorité de l'environnement, ce qui
    permet de les fournir via les GitHub Secrets sans jamais committer de token.
    """
    cfg = {
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "currency": "EUR",
        "alert_threshold_pct": 5.0,   # mouvement (%) à partir duquel on signale un sticker
        "send_mode": "always",        # "always" = rapport à chaque run, "on_change" = seulement si bouge
        "request_delay": 3.0,         # secondes entre 2 appels Steam (anti rate-limit)
        "max_retries": 4,
    }
    if os.path.exists(CFG_FILE):
        with open(CFG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # Surcharge par l'environnement (GitHub Secrets)
    cfg["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", cfg["telegram_bot_token"])
    cfg["telegram_chat_id"]   = os.environ.get("TELEGRAM_CHAT_ID",   cfg["telegram_chat_id"])
    if os.environ.get("STICKER_CURRENCY"):
        cfg["currency"] = os.environ["STICKER_CURRENCY"]
    return cfg


def load_stickers():
    with open(STICKERS_FILE, encoding="utf-8") as f:
        return json.load(f)


# ── Steam Market ────────────────────────────────────────────────────────────

def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def parse_money(s):
    """'25,19€' / '$25.19' / '1.234,56 €' -> float. None si vide/illisible."""
    if not s:
        return None
    keep = "".join(c for c in s if c.isdigit() or c in ",.")
    if not keep:
        return None
    # Si les deux séparateurs sont présents, le dernier est le séparateur décimal.
    if "," in keep and "." in keep:
        if keep.rfind(",") > keep.rfind("."):
            keep = keep.replace(".", "").replace(",", ".")
        else:
            keep = keep.replace(",", "")
    else:
        keep = keep.replace(",", ".")
    try:
        return round(float(keep), 2)
    except ValueError:
        return None


def steam_price(market_hash_name, currency_id, max_retries=4, base_delay=3.0):
    """Retourne {'lowest':float|None,'median':float|None,'volume':int} ou None."""
    q = urllib.parse.quote(market_hash_name)
    url = (f"https://steamcommunity.com/market/priceoverview/"
           f"?appid=730&currency={currency_id}&market_hash_name={q}")
    for attempt in range(max_retries):
        try:
            status, body = _http_get(url)
            if status == 200:
                d = json.loads(body)
                if d.get("success"):
                    vol = d.get("volume", "0").replace(",", "").replace(".", "")
                    return {
                        "lowest": parse_money(d.get("lowest_price")),
                        "median": parse_money(d.get("median_price")),
                        "volume": int(vol) if vol.isdigit() else 0,
                    }
            if status == 429:  # rate limited -> backoff exponentiel
                time.sleep(base_delay * (2 ** attempt) + 2)
                continue
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"  ! erreur fetch ({market_hash_name}): {e}")
            time.sleep(base_delay * (2 ** attempt))
    return None


# ── Historique ──────────────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return []


def save_history(history):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def append_csv(ts, prices):
    os.makedirs(DATA_DIR, exist_ok=True)
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "sticker", "price"])
        for name, price in prices.items():
            w.writerow([ts, name, price if price is not None else ""])


# ── Rapport ─────────────────────────────────────────────────────────────────

SYM = {"EUR": "€", "USD": "$", "GBP": "£", "BRL": "R$", "RUB": "₽"}


def fmt(v, sym):
    return f"{v:.2f}{sym}" if v is not None else "n/a"


def arrow(delta):
    if delta is None:
        return ""
    if delta > 0:
        return "🟢▲"
    if delta < 0:
        return "🔴▼"
    return "⚪="


def build_report(stickers, current, prev_snapshot, cfg):
    """Construit (texte_markdown, has_alert).

    current        : {name: {'lowest','median','volume'}}
    prev_snapshot  : {name: price} du run précédent (peut être {} au 1er run)
    """
    sym = SYM.get(cfg["currency"], cfg["currency"] + " ")
    thr = float(cfg.get("alert_threshold_pct", 5.0))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"🎯 *CS2 Sticker Tracker* — Cologne 2026 (Holo)", f"_{now}_", ""]
    total_now = 0.0
    total_ref = 0.0
    total_prev = 0.0
    has_alert = False
    movers = []

    for st in stickers:
        name = st["name"]
        qty  = st.get("qty", 1)
        cur  = current.get(name) or {}
        price = cur.get("lowest") or cur.get("median")
        ref   = st.get("ref_price")
        prev  = prev_snapshot.get(name)

        d_prev = (price - prev) if (price is not None and prev is not None) else None
        d_ref  = (price - ref)  if (price is not None and ref is not None) else None
        pct_prev = (d_prev / prev * 100) if (d_prev is not None and prev) else None

        if price is not None:
            total_now += price * qty
        if ref is not None:
            total_ref += ref * qty
        total_prev += (prev if prev is not None else (price or 0)) * qty

        tag = arrow(d_prev)
        extra = ""
        if pct_prev is not None and abs(pct_prev) >= thr:
            has_alert = True
            movers.append((name, pct_prev))
            extra = f"  *({pct_prev:+.1f}%)*"
        elif pct_prev is not None and pct_prev != 0:
            extra = f"  ({pct_prev:+.1f}%)"

        qty_str = f" ×{qty}" if qty != 1 else ""
        vol = cur.get("volume")
        vol_str = f"  · vol {vol}" if vol else ""
        lines.append(f"{tag} *{name}*{qty_str} : {fmt(price, sym)}{extra}{vol_str}")

    lines.append("")
    lines.append(f"💼 *Portefeuille* : {fmt(total_now, sym)}")
    if total_ref:
        d = total_now - total_ref
        pct = (d / total_ref * 100) if total_ref else 0
        lines.append(f"📈 vs départ : {d:+.2f}{sym} ({pct:+.1f}%)")
    if total_prev and abs(total_now - total_prev) >= 0.01:
        lines.append(f"⏱ vs run précédent : {total_now - total_prev:+.2f}{sym}")

    if movers:
        movers.sort(key=lambda x: abs(x[1]), reverse=True)
        top = ", ".join(f"{n} {p:+.1f}%" for n, p in movers[:5])
        lines.append("")
        lines.append(f"🚨 Mouvements ≥ {thr:.0f}% : {top}")

    return "\n".join(lines), has_alert


# ── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
            return ok
    except urllib.error.HTTPError as e:
        print(f"  ! Telegram HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    except Exception as e:
        print(f"  ! Telegram erreur: {e}")
    return False


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="CS2 sticker price tracker -> Telegram")
    ap.add_argument("--dry-run", action="store_true", help="pas d'écriture ni de Telegram")
    ap.add_argument("--no-telegram", action="store_true", help="ne pas envoyer sur Telegram")
    args = ap.parse_args()

    cfg = load_config()
    stickers = load_stickers()
    currency_id = CURRENCIES.get(cfg["currency"], 3)
    sym = SYM.get(cfg["currency"], cfg["currency"] + " ")

    print(f"== CS2 Sticker Tracker == {len(stickers)} stickers, devise {cfg['currency']}")

    current = {}
    prices_flat = {}
    for st in stickers:
        res = steam_price(st["market_hash_name"], currency_id,
                          int(cfg.get("max_retries", 4)), float(cfg.get("request_delay", 3.0)))
        current[st["name"]] = res or {}
        price = (res or {}).get("lowest") or (res or {}).get("median")
        prices_flat[st["name"]] = price
        print(f"  {st['name']:<16} {fmt(price, sym)}")
        time.sleep(float(cfg.get("request_delay", 3.0)))

    history = load_history()
    prev_snapshot = history[-1]["prices"] if history else {}

    text, has_alert = build_report(stickers, current, prev_snapshot, cfg)
    print("\n" + text + "\n")

    if args.dry_run:
        print("[dry-run] terminé (aucune écriture / aucun envoi).")
        return

    # Historique
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    history.append({"ts": ts, "prices": prices_flat})
    save_history(history)
    append_csv(ts, prices_flat)
    print(f"[ok] historique mis à jour ({len(history)} snapshots).")

    # Telegram
    send = not args.no_telegram
    if cfg.get("send_mode") == "on_change" and not has_alert and prev_snapshot:
        # Pas de mouvement notable -> on n'envoie pas en mode on_change
        send = False
        print("[info] send_mode=on_change et aucun mouvement notable : pas d'envoi.")
    if send:
        if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
            print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants : pas d'envoi.")
        elif send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], text):
            print("[ok] rapport envoyé sur Telegram.")
        else:
            print("[warn] échec d'envoi Telegram.")
            sys.exit(1)


if __name__ == "__main__":
    main()
