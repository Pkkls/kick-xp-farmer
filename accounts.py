#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accounts.py  —  Magasin multi-compte pour le launcher Kick.

Un compte = un login Kick = un export de cookies (qui contient le session_token).
Le secret d'un compte tient donc en UN fichier cookies. On stocke la liste dans
accounts.json et les cookies dans accounts/<id>.cookies.json (les deux gitignores).

Schema accounts.json : liste de
  {id, label, user, cookies_file, channel, enabled, created}

API utilisee par le launcher (launcher.py) et importable a la main.

# ponytail: tokens stockes EN CLAIR (gitignore + 127.0.0.1 only). Pour un produit
# vendable -> chiffrer au repos via DPAPI Windows (win32crypt.CryptProtectData,
# lie au compte utilisateur, zero gestion de cle) cote ecriture/lecture cookies
# ET cote points_farmer._bearer_from_cookies. Reporte: touche 3 fichiers.
"""
import os, re, json, time, hashlib, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
COOKIES_DIR = os.path.join(HERE, "accounts")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

try:
    from curl_cffi import requests as _cffi
    _HTTP = _cffi.Session(impersonate="chrome136")
except ImportError:
    _HTTP = None


# ── Persistance ────────────────────────────────────────────────────────────
def load_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        data = json.load(open(ACCOUNTS_FILE, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_accounts(accounts):
    tmp = ACCOUNTS_FILE + ".tmp"
    json.dump(accounts, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)  # ecriture atomique


def get(accounts, acc_id):
    return next((a for a in accounts if a.get("id") == acc_id), None)


# ── Cookies / token ────────────────────────────────────────────────────────
def bearer_of(account):
    """Token Bearer depuis le cookie session_token du compte (None si absent)."""
    try:
        for c in json.load(open(account["cookies_file"], encoding="utf-8")):
            if c.get("name") == "session_token" and c.get("value"):
                return urllib.parse.unquote(c["value"])
    except Exception:
        pass
    return None


def _identity(bearer):
    """(pseudo, avatar_url) du compte via /api/v1/user. On n'extrait QUE ces deux
    champs (la reponse contient aussi email/phone -> jamais stockes/exposes)."""
    if not (_HTTP and bearer):
        return "", None
    try:
        r = _HTTP.get("https://kick.com/api/v1/user", timeout=10, headers={
            "Authorization": f"Bearer {bearer}", "Accept": "application/json",
            "User-Agent": _UA, "Referer": "https://kick.com/", "Origin": "https://kick.com"})
        if r.status_code == 200:
            d = r.json()
            user = d.get("username") or (d.get("streamer_channel") or {}).get("slug") or ""
            return user, d.get("profilepic")
    except Exception:
        pass
    return "", None


def _new_id(cookies_list):
    # Id stable derive du session_token (un meme compte reimporte -> meme id).
    tok = next((c["value"] for c in cookies_list if c.get("name") == "session_token"), "")
    h = hashlib.sha1(tok.encode()).hexdigest()[:8] if tok else hashlib.sha1(str(time.time()).encode()).hexdigest()[:8]
    return "acc_" + h


# ── Operations ─────────────────────────────────────────────────────────────
def add_account(cookies_json, channel="", label=""):
    """Ajoute (ou met a jour) un compte depuis un export de cookies.

    cookies_json : liste de cookies (deja parsee) OU chaine JSON.
    Retourne (account, erreur|None).
    """
    if isinstance(cookies_json, str):
        try:
            cookies_json = json.loads(cookies_json)
        except json.JSONDecodeError:
            return None, "cookies JSON invalide"
    if not isinstance(cookies_json, list) or not cookies_json:
        return None, "cookies vides ou format inattendu"
    if not any(c.get("name") == "session_token" for c in cookies_json):
        return None, "aucun cookie 'session_token' dans l'export (compte non connecte ?)"

    os.makedirs(COOKIES_DIR, exist_ok=True)
    # Identite stable = pseudo Kick. Le session_token tourne : dedup sur son hash
    # creerait un doublon a chaque reimport. On dedup donc sur le 'user'.
    tok = next((urllib.parse.unquote(c["value"]) for c in cookies_json
                if c.get("name") == "session_token"), "")
    user, avatar = _identity(tok)
    token_id = _new_id(cookies_json)
    accounts = load_accounts()
    # dedup par pseudo (token a tourne) OU par id-token (meme token, entree legacy
    # sans champ 'user'). Evite les doublons dans les deux cas.
    existing = next((a for a in accounts if user and a.get("user") == user), None) \
        or get(accounts, token_id)
    acc_id = existing["id"] if existing else token_id
    cookies_file = os.path.join(COOKIES_DIR, f"{acc_id}.cookies.json")
    json.dump(cookies_json, open(cookies_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    account = existing or {"id": acc_id, "created": int(time.time())}
    account.update({"label": label or user or account.get("label") or acc_id,
                    "user": user or account.get("user", ""),
                    "avatar": avatar or account.get("avatar"),
                    "cookies_file": cookies_file,
                    "channel": channel or account.get("channel", ""),
                    "enabled": account.get("enabled", True)})
    if not existing:
        accounts.append(account)
    save_accounts(accounts)
    return account, None


def update_account(acc_id, **fields):
    accounts = load_accounts()
    a = get(accounts, acc_id)
    if not a:
        return None, "compte introuvable"
    for k in ("label", "channel", "enabled"):
        if k in fields and fields[k] is not None:
            a[k] = fields[k]
    save_accounts(accounts)
    return a, None


def remove_account(acc_id):
    accounts = load_accounts()
    a = get(accounts, acc_id)
    if not a:
        return False
    try:
        if os.path.exists(a["cookies_file"]):
            os.remove(a["cookies_file"])
    except OSError:
        pass
    save_accounts([x for x in accounts if x.get("id") != acc_id])
    return True


if __name__ == "__main__":
    # Debug rapide : liste les comptes connus.
    for a in load_accounts():
        print(f"{a['id']}  {a['label']:<20} channel={a.get('channel') or '-':<18} "
              f"enabled={a.get('enabled')}")
