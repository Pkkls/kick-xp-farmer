#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_follows.py — Recupere AUTOMATIQUEMENT la liste complete des chaines
suivies (live + offline) de n'importe quel utilisateur, via l'API Kick.

Aucune manip manuelle (pas de scroll / copier-coller) : on interroge
  GET https://kick.com/api/v2/channels/followed
en s'authentifiant avec le meme `session_token` que kick-xp-farmer
(cookie Kick, voir README). Marche pour n'importe quel compte du moment
qu'on fournit SON session_token.

Usage:
  python fetch_follows.py                       # lit session_token depuis config.json
  python fetch_follows.py -o following.json     # ecrit la liste
  python fetch_follows.py --write-config config.json   # injecte dans slug_pool
"""
import os, sys, json, time, argparse, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

# Reutilise curl_cffi (deja requis par le farmer) pour passer Cloudflare,
# avec repli sur urllib si indisponible.
try:
    from curl_cffi import requests as _cffi
    def _session():
        return _cffi.Session(impersonate="chrome136")
    _USE_CFFI = True
except ImportError:
    import urllib.request
    _USE_CFFI = False


def _load_token(cfg_file):
    if not os.path.exists(cfg_file):
        sys.exit(f"[err] {cfg_file} introuvable. Renseigne 'session_token' "
                 f"(voir config.example.json) ou passe --token.")
    cfg = json.load(open(cfg_file, encoding="utf-8"))
    tok = cfg.get("session_token", "")
    if not tok or "VOTRE" in tok or "TON" in tok:
        sys.exit("[err] session_token manquant dans config.json.")
    return urllib.parse.unquote(tok)


def _headers(bearer):
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://kick.com/following",
        "Origin": "https://kick.com",
        "Authorization": f"Bearer {bearer}",
    }


def _get_json(sess, url, headers, timeout=15):
    if _USE_CFFI:
        r = sess.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return None, r.status_code
        try:
            return r.json(), 200
        except Exception:
            return None, r.status_code
    else:
        import urllib.request, urllib.error
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace")), 200
        except urllib.error.HTTPError as e:
            return None, e.code
        except Exception:
            return None, 0


def _harvest_slugs(node, out, seen):
    """Parcourt recursivement le JSON et collecte les slugs de CHAINES.

    On retient un dict ssi il a une cle 'slug' ET un marqueur de chaine
    (user_id / username / followers_count / livestream / is_live / verified),
    pour eviter d'attraper des slugs de categories.
    """
    if isinstance(node, dict):
        slug = node.get("slug")
        markers = ("user_id", "username", "followers_count", "livestream",
                   "is_live", "verified", "playback_url", "user")
        if isinstance(slug, str) and slug and any(m in node for m in markers):
            key = slug.lower()
            if key not in seen:
                seen.add(key)
                out.append(slug)
        for v in node.values():
            _harvest_slugs(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            _harvest_slugs(v, out, seen)


def fetch_follows(bearer, max_pages=200, delay=0.8, verbose=True):
    """Renvoie la liste ordonnee unique des slugs suivis."""
    sess = _session() if _USE_CFFI else None
    headers = _headers(bearer)

    # Warmup Cloudflare (comme le farmer)
    if _USE_CFFI:
        try:
            sess.get("https://kick.com/", headers={**headers, "Accept": "text/html"}, timeout=15)
        except Exception:
            pass

    out, seen = [], set()
    # L'API a connu plusieurs schemas de pagination : on tente cursor puis page.
    for scheme in ("cursor", "page"):
        empty_streak = 0
        for i in range(max_pages):
            base = "https://kick.com/api/v2/channels/followed"
            url = f"{base}?{scheme}={i}"
            data, status = _get_json(sess, url, headers)
            if status == 401 or status == 403:
                sys.exit(f"[err] non authentifie (HTTP {status}). "
                         f"session_token invalide ou expire.")
            if data is None:
                # endpoint v2 indispo -> tente v1 une fois
                if i == 0 and scheme == "cursor":
                    data, status = _get_json(
                        sess, f"https://kick.com/api/v1/channels/followed?{scheme}={i}", headers)
                if data is None:
                    break
            before = len(out)
            _harvest_slugs(data, out, seen)
            gained = len(out) - before
            if verbose:
                sys.stderr.write(f"[{scheme} {i}] +{gained} (total {len(out)})\n")
            # Conditions d'arret
            if gained == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
            # Fin de pagination explicite
            if isinstance(data, dict):
                if data.get("next_cursor") in (None, "", 0) and "next_cursor" in data:
                    break
            time.sleep(delay)
        if out:
            break  # un schema a fonctionne, inutile d'essayer l'autre
    return out


def main():
    ap = argparse.ArgumentParser(description="Recupere les chaines suivies via l'API Kick")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--token", help="session_token (sinon lu depuis config.json)")
    ap.add_argument("-o", "--output", help="ecrit la liste JSON dans ce fichier")
    ap.add_argument("--write-config", metavar="CONFIG",
                    help="injecte la liste dans le 'slug_pool' du config.json donne")
    ap.add_argument("--plain", action="store_true")
    args = ap.parse_args()

    bearer = urllib.parse.unquote(args.token) if args.token else _load_token(args.config)
    slugs = fetch_follows(bearer)
    sys.stderr.write(f"[ok] {len(slugs)} chaines suivies recuperees.\n")

    if args.write_config:
        cfg = json.load(open(args.write_config, encoding="utf-8")) if os.path.exists(args.write_config) else {}
        cfg["slug_pool"] = slugs
        json.dump(cfg, open(args.write_config, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        sys.stderr.write(f"[ok] slug_pool ecrit dans {args.write_config}\n")
    elif args.output:
        json.dump(slugs, open(args.output, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        sys.stderr.write(f"[ok] ecrit dans {args.output}\n")
    elif args.plain:
        print("\n".join(slugs))
    else:
        print(json.dumps(slugs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
