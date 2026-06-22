#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_follows.py — Nettoie un copier-coller de la page kick.com/following/channels.

Workflow manuel (marche sans authentification) :
  1. Va sur https://kick.com/following/channels
  2. Scrolle tout en bas jusqu'a ce que plus rien ne charge (lazy-load)
  3. Ctrl+A puis Ctrl+C, colle dans un fichier texte (ex: follows.txt)
  4. python parse_follows.py follows.txt

Le copier-coller contient :
  - des lignes parasites d'UI (Kick Logo, Search, Following, Categories, ...)
  - chaque chaine en double : pour les LIVE -> "NomLIVE" puis "Nom",
    pour les offline -> "Nom" puis "Nom".

Le script renvoie la liste unique et propre des chaines, prete a coller dans
le "slug_pool" de config.json (kick-xp-farmer).
"""
import sys, json, argparse, os

# Lignes d'interface a ignorer (identiques pour tout le monde)
PARASITES = {
    "kick logo", "search", "following", "recommended", "live channels",
    "categories", "channels", "followed channels", "following - channels on kick",
    "follow", "followed", "live", "browse", "home",
}


def parse_follows(text):
    """Retourne (slugs, stats). slugs = liste ordonnee unique de chaines."""
    raw = [l.strip() for l in text.splitlines()]
    lines = [l for l in raw if l and l.lower() not in PARASITES]

    result, seen = [], set()
    live = set()
    adjusted = []
    i, n = 0, len(lines)
    while i < n:
        a = lines[i]
        b = lines[i + 1] if i + 1 < n else None
        # Paire detectee : offline (a == b) ou live (a == b + "LIVE")
        if b is not None and (a == b or a == b + "LIVE"):
            name = b
            if a == b + "LIVE":
                live.add(b.lower())
            i += 2
        else:
            # Ligne orpheline : on retire un eventuel suffixe "LIVE"
            name = a[:-4] if a.endswith("LIVE") and len(a) > 4 else a
            if a.endswith("LIVE") and len(a) > 4:
                live.add(name.lower())
            i += 1
        # Les slugs Kick ne contiennent jamais d'espace : on normalise
        # (le nom affiche d'une chaine live peut contenir des espaces).
        slug = name.replace(" ", "")
        if not slug:
            continue
        key = slug.lower()
        if key not in seen:
            seen.add(key)
            result.append(slug)
            if " " in name:
                adjusted.append(f"{name!r} -> {slug!r}")

    stats = {
        "total": len(result),
        "live": len(live),
        "adjusted": adjusted,
    }
    return result, stats


def main():
    ap = argparse.ArgumentParser(description="Parse un dump kick.com/following/channels")
    ap.add_argument("file", nargs="?", help="fichier texte (sinon lit stdin)")
    ap.add_argument("-o", "--output", help="ecrit la liste JSON dans ce fichier")
    ap.add_argument("--write-config", metavar="CONFIG",
                    help="injecte la liste dans le 'slug_pool' du config.json donne")
    ap.add_argument("--plain", action="store_true", help="sortie texte (1 chaine/ligne)")
    args = ap.parse_args()

    text = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    slugs, stats = parse_follows(text)

    sys.stderr.write(
        f"[parse] {stats['total']} chaines uniques "
        f"({stats['live']} live au moment du copier-coller)\n")
    if stats["adjusted"]:
        sys.stderr.write(
            "[warn] noms avec espace normalises (slug = nom sans espace). "
            "Si une chaine ne repond pas, verifie son slug exact via fetch_follows.py : "
            + ", ".join(stats["adjusted"]) + "\n")

    if args.write_config:
        cfg = {}
        if os.path.exists(args.write_config):
            cfg = json.load(open(args.write_config, encoding="utf-8"))
        cfg["slug_pool"] = slugs
        json.dump(cfg, open(args.write_config, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        sys.stderr.write(f"[ok] slug_pool ({len(slugs)}) ecrit dans {args.write_config}\n")
        return

    if args.output:
        json.dump(slugs, open(args.output, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        sys.stderr.write(f"[ok] {len(slugs)} chaines ecrites dans {args.output}\n")
    elif args.plain:
        print("\n".join(slugs))
    else:
        print(json.dumps(slugs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
