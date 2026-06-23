#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard.py — Kick Points Dashboard

A clean terminal dashboard that lists the channels you follow ranked by the
loyalty points you have on each one (highest first). Built on top of the same
session_token used by the farmer.

    python dashboard.py            # fetch points + interactive dashboard
    python dashboard.py --once     # render once and exit (no menu)
    python dashboard.py --cached   # render from last saved snapshot (no network)
    python dashboard.py --live     # also fetch live status (extra requests)

Channels come from config.json "slug_pool", else following.json, else defaults.
Points are read from GET /api/v2/channels/{slug}/me (authenticated).
"""
import os, sys, json, time, argparse, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich import box
from rich.align import Align
from rich.text import Text

HERE         = os.path.dirname(os.path.abspath(__file__))
CFG_FILE     = os.path.join(HERE, "config.json")
FOLLOWS_FILE = os.path.join(HERE, "following.json")
DATA_DIR     = os.path.join(HERE, "data")
SNAPSHOT     = os.path.join(DATA_DIR, "points.json")

console = Console()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

# curl_cffi (Cloudflare bypass) with urllib fallback
try:
    from curl_cffi import requests as _cffi
    _USE_CFFI = True
except ImportError:
    _USE_CFFI = False


# ── Config / channel list ─────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CFG_FILE):
        return {}
    try:
        return json.load(open(CFG_FILE, encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_channels(cfg):
    if cfg.get("slug_pool"):
        return list(dict.fromkeys(cfg["slug_pool"]))
    if os.path.exists(FOLLOWS_FILE):
        try:
            data = json.load(open(FOLLOWS_FILE, encoding="utf-8"))
            if isinstance(data, list) and data:
                return list(dict.fromkeys(data))
        except json.JSONDecodeError:
            pass
    return ["kaicenat", "xqc", "trainwreckstv", "adin", "destiny"]


def get_bearer(cfg):
    tok = cfg.get("session_token", "")
    if not tok or "VOTRE" in tok or "TON" in tok:
        return None
    return urllib.parse.unquote(tok)


# ── Kick API ──────────────────────────────────────────────────────────────────

def _headers(bearer):
    h = {"User-Agent": UA, "Accept": "application/json",
         "Referer": "https://kick.com/", "Origin": "https://kick.com"}
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return h


class Blocked(Exception):
    pass


def _get(session, url, headers, timeout=12):
    if _USE_CFFI:
        r = session.get(url, headers=headers, timeout=timeout)
        return r.status_code, r.text
    else:
        import urllib.request, urllib.error
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return 0, ""


def fetch_channel(slug, bearer, session=None, want_live=False):
    """Returns {'slug','points','live'}. points=None if unknown."""
    # IMPORTANT: reutiliser UNE session partagee (passee par fetch_all). Creer une
    # session neuve par channel = autant de handshakes TLS en rafale -> Cloudflare
    # bloque (403 'security policy') et les points repassent en n/a.
    if session is None and _USE_CFFI:
        session = _cffi.Session(impersonate="chrome136")
    headers = _headers(bearer)
    rec = {"slug": slug, "points": None, "live": None}

    # Le solde de points est sur /points ({"data":{"points":N}}), PAS sur /me
    # (qui ne contient que subscription/following/leaderboards, aucun champ points).
    status, body = _get(session, f"https://kick.com/api/v2/channels/{slug}/points", headers)
    if status in (401, 403) and "security policy" in body.lower():
        raise Blocked()
    if status == 200:
        try:
            rec["points"] = (json.loads(body).get("data") or {}).get("points")
        except json.JSONDecodeError:
            pass

    if want_live:
        s2, b2 = _get(session, f"https://kick.com/api/v2/channels/{slug}", headers)
        if s2 == 200:
            try:
                rec["live"] = bool(json.loads(b2).get("livestream"))
            except json.JSONDecodeError:
                pass
    return rec


def fetch_all(channels, bearer, want_live, workers=8):
    results, blocked = [], False
    # Une seule session partagee + warmup Cloudflare (recupere les cookies __cf_bm),
    # comme le farmer. Evite les 403 'security policy' qui mettaient tout en n/a.
    sess = None
    if _USE_CFFI:
        sess = _cffi.Session(impersonate="chrome136")
        try:
            sess.get("https://kick.com/", headers={**_headers(bearer), "Accept": "text/html"}, timeout=15)
        except Exception:
            pass
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(), console=console, transient=True,
    ) as progress:
        task = progress.add_task("Fetching points…", total=len(channels))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_channel, c, bearer, sess, want_live): c for c in channels}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Blocked:
                    blocked = True
                    results.append({"slug": futs[fut], "points": None, "live": None})
                except Exception:
                    results.append({"slug": futs[fut], "points": None, "live": None})
                progress.advance(task)
    if blocked:
        console.print(Panel(
            "[bold red]Kick blocked some requests (Cloudflare 'security policy').[/]\n"
            "Run this from your own machine (residential IP) — same as the farmer.",
            border_style="red", title="[!] Blocked"))
    return results


# ── Rendering ─────────────────────────────────────────────────────────────────

MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}


def render(results, *, hide_zero=False, show_live=False):
    rows = sorted(results, key=lambda r: (r.get("points") or -1), reverse=True)
    if hide_zero:
        rows = [r for r in rows if (r.get("points") or 0) > 0]

    known = [r for r in rows if r.get("points") is not None]
    max_pts = max((r["points"] for r in known), default=0) or 1
    total_pts = sum(r["points"] for r in known)
    with_pts = sum(1 for r in known if r["points"] > 0)
    live_n = sum(1 for r in rows if r.get("live"))

    title = Text("  KICK POINTS DASHBOARD  ", style="bold white on dark_green")
    console.print(Align.center(title))
    console.print()

    table = Table(box=box.ROUNDED, header_style="bold cyan",
                  show_lines=False, expand=True, border_style="grey35")
    table.add_column("#", justify="right", style="grey62", width=4)
    table.add_column("Channel", style="bold", no_wrap=True)
    if show_live:
        table.add_column("", justify="center", width=4)
    table.add_column("Points", justify="right", style="gold1", width=12)
    table.add_column("", ratio=1)  # bar

    for i, r in enumerate(rows):
        pts = r.get("points")
        rank = MEDALS.get(i, str(i + 1))
        name = r["slug"]
        name_style = "bold yellow" if i < 3 else ("white" if (pts or 0) > 0 else "grey50")
        cells = [rank, Text(name, style=name_style)]
        if show_live:
            cells.append(Text("●", style="bold red") if r.get("live") else Text("·", style="grey42"))
        if pts is None:
            cells.append(Text("n/a", style="grey42"))
            cells.append(Text(""))
        else:
            cells.append(f"{pts:,}".replace(",", " "))
            blocks = int(round((pts / max_pts) * 28)) if max_pts else 0
            bar = Text("▇" * blocks, style="green" if i >= 3 else "yellow")
            cells.append(bar)
        table.add_row(*cells)

    console.print(table)
    summary = (f"[bold]{len(rows)}[/] channels   ·   "
               f"[gold1]{total_pts:,}".replace(",", " ") + "[/] total points   ·   "
               f"[green]{with_pts}[/] with points")
    if show_live:
        summary += f"   ·   [red]{live_n} live[/]"
    console.print(Panel(summary, border_style="grey35", box=box.ROUNDED))


# ── Snapshot ──────────────────────────────────────────────────────────────────

def save_snapshot(results):
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump({"ts": time.time(), "results": results},
              open(SNAPSHOT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_snapshot():
    if os.path.exists(SNAPSHOT):
        try:
            return json.load(open(SNAPSHOT, encoding="utf-8")).get("results", [])
        except json.JSONDecodeError:
            pass
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Kick Points Dashboard")
    ap.add_argument("--once", action="store_true", help="render once and exit")
    ap.add_argument("--cached", action="store_true", help="render last snapshot (no network)")
    ap.add_argument("--live", action="store_true", help="also fetch live status")
    ap.add_argument("--hide-zero", action="store_true", help="hide channels with 0 points")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    cfg = load_config()
    channels = load_channels(cfg)
    show_live = args.live

    if args.cached:
        results = load_snapshot()
        if not results:
            console.print("[yellow]No snapshot yet — run without --cached first.[/]")
            return
        render(results, hide_zero=args.hide_zero, show_live=any("live" in r and r["live"] is not None for r in results))
        return

    bearer = get_bearer(cfg)
    if not bearer:
        console.print(Panel(
            "[bold red]No session_token in config.json.[/]\n"
            "Add it (see config.example.json) to read your channel points.",
            border_style="red", title="[!] Missing token"))
        return

    def refresh(live):
        res = fetch_all(channels, bearer, live, workers=args.workers)
        save_snapshot(res)
        return res

    results = refresh(show_live)
    console.clear()
    render(results, hide_zero=args.hide_zero, show_live=show_live)

    if args.once:
        return

    # Interactive menu
    while True:
        console.print()
        console.print(
            "[bold cyan]r[/] refresh   "
            "[bold cyan]l[/] toggle live   "
            "[bold cyan]z[/] toggle hide-zero   "
            "[bold cyan]q[/] quit", justify="center")
        try:
            choice = console.input("[grey62]> [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if choice == "q":
            break
        elif choice == "r":
            results = refresh(show_live)
            console.clear(); render(results, hide_zero=args.hide_zero, show_live=show_live)
        elif choice == "l":
            show_live = not show_live
            results = refresh(show_live)
            console.clear(); render(results, hide_zero=args.hide_zero, show_live=show_live)
        elif choice == "z":
            args.hide_zero = not args.hide_zero
            console.clear(); render(results, hide_zero=args.hide_zero, show_live=show_live)
    console.print("[grey62]bye 👋[/]")


if __name__ == "__main__":
    main()
