# Kick Points Suite

> Multi-account **Kick Channel Points & XP** farmer with a local, real-time control-center dashboard.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
![Status](https://img.shields.io/badge/status-working-success)

Run one account from a single click, or scale to dozens — each account farms its own
channel in an isolated browser, supervised automatically. Everything runs locally;
tokens never leave your machine.

> [!WARNING]
> **Not affiliated with Kick. Read this first — it applies every time you run the app.**
> Automating Channel Points or XP, and running multiple accounts, may violate
> [Kick's Terms of Service](https://kick.com/terms-of-service) and can get accounts
> **permanently sanctioned or banned**. This is a personal / educational project provided
> as-is, with no warranty. **You alone are responsible for how you use it — use at your own
> risk.** The dashboard shows this same warning as a persistent banner on every screen, on purpose.

---

<img width="944" height="609" alt="image" src="https://github.com/user-attachments/assets/217a2129-140e-4d65-9733-0ac78a53b557" />
<img width="945" height="548" alt="image" src="https://github.com/user-attachments/assets/ef0e5b77-9269-43da-870b-6e2a62fb4c9b" />


## Why a real browser?

Two different reward systems on Kick, two different mechanisms:

| Reward | Scope | How it credits | Tool |
|--------|-------|----------------|------|
| **XP** | Account-wide | Presence on the private Pusher WebSocket of a live stream. **No browser needed** — lightweight, runs many accounts at once. | `farmer.py`, driven per-account by the launcher's **XP Farmer** tab |
| **Channel Points** | Per-channel | **Only credit with real, unmuted video playback** (anti-AFK gate, verified empirically). | `launcher.py`, one real Chrome per account |

WebSocket presence alone earns **zero** Channel Points. So the launcher drives a **real
Chrome** (via Playwright, `--headless=new`, audio muted at the OS level but `video.muted = false`)
that actually plays the stream. One account = one isolated worker process = one Chrome.

---

## Features

- **Multi-account supervisor** — concurrency cap, staggered startup, time-based rotation
  when you have more accounts than slots, crash auto-restart with backoff + quarantine,
  and a RAM guard before spawning new browsers.
- **Control-center dashboard** (`http://127.0.0.1:8780`) — account cards with live status,
  KPIs, activity feed, command palette (`Ctrl/Cmd+K`), bulk actions, sort/density, points
  sparklines, RAM/CPU meter.
- **XP Farmer tab** — farm Kick XP in the background on as many accounts as you like, no
  browser. Each account **auto-selects whichever channel you follow is live** (you never pick
  a streamer) and the card shows exactly which one it landed on, plus level, XP/min and ETA to
  the next level. Per-account Start/Stop, *Start all* / *Stop all*, state persists across
  restarts, crashed workers auto-restart.
- **One-click onboarding** — *Log in with Kick*: a browser opens, you log in normally
  (we never see your password), cookies are captured automatically. No extension, no copy-paste.
- **Follows panel** — total Channel Points an account has accumulated across **every channel
  it follows**, ranked.
- **Crash-safe** — Windows Job Object ensures no orphan Chrome survives, even if the launcher
  is force-killed.
- **Local-first** — dashboard binds to `127.0.0.1`, CSRF-guarded; cookies/tokens are gitignored.

---


## Requirements

- **Python 3.10+**
- **Google Chrome** installed (the real browser is required to pass anti-bot; the bundled
  Chromium is *not* used)
- A Kick account (more for scaling)

```bash
pip install -r requirements.txt
```

> No `playwright install` needed — the tools launch your installed Chrome, not bundled Chromium.

---

## Quick start

**Windows:** double-click **`START.bat`** — it installs dependencies on first run and opens
the dashboard.

**Any platform:**

```bash
pip install -r requirements.txt
python launcher.py            # opens http://127.0.0.1:8780
```

Then in the dashboard:

1. Click **+ Account → Log in with Kick**.
2. Log into the Kick account in the browser window that opens.
3. Pick a channel to farm (or set it later from the card / channel autocomplete).
4. Add more accounts to scale — the supervisor handles the rest.

---

## Tools

| Command | What it does |
|---------|--------------|
| `python launcher.py` | **Main app.** Multi-account points launcher + XP Farmer + web dashboard. |
| `python points_ui.py --menu` | Single-account console menu (pick a live channel, farm it). |
| `python points_ui.py` | Single-account web UI (`http://127.0.0.1:8770`). |
| `python dashboard.py` | Terminal points ranking across your follows (rich TUI). |
| `python farmer.py` | Standalone XP farmer for one account (WebSocket, no browser). The launcher runs one of these per account for the XP Farmer tab, passing the account's token via env. |
| `python fetch_follows.py -o following.json` | Export an account's full follow list via the API. |

---

## Configuration

Copy the template and fill it in:

```bash
cp config.example.json config.json
```

The launcher stores accounts separately (see *Onboarding*), so `config.json` mostly tunes
the supervisor. Key options:

| Key | Default | Description |
|-----|---------|-------------|
| `launcher_port` | `8780` | Dashboard port. |
| `max_concurrent` | `5` | Max simultaneous Chrome workers (each ≈ 300–500 MB). |
| `rotate_minutes` | `30` | Cycle accounts through slots every N minutes (`0` = off). |
| `stagger_seconds` | `4` | Spacing between worker starts (anti CPU-spike). |
| `restart_backoff` | `10` | Base backoff (s) before restarting a crashed worker. |
| `quarantine_restarts` | `8` | Crash-loop threshold; past it the slot is freed. |
| `mem_floor_mb` | `1500` | Don't start a worker below this free RAM (needs `psutil`). |
| `points_chrome_path` | Windows Chrome path | Real Chrome binary (falls back to `channel="chrome"`). |
| `points_headless` | `true` | Render the player offscreen (`--headless=new`). |

`session_token` and `slug_pool` in the template are only used by the standalone XP farmer
(`farmer.py`) and the legacy single-account flow.

---

## Onboarding & per-account data

Each account = one Kick login = one cookie set (containing its `session_token` plus the
anti-bot cookies). Two ways to add one:

1. **Log in with Kick** (recommended) — the launcher opens a real browser, you log in, it
   captures the cookies. Your password stays in the browser; only cookies are read.
2. **Paste cookies (advanced)** — for headless/remote setups. Export with the
   [Cookie-Editor](https://cookie-editor.com/) extension (Export → JSON) and paste into the
   *Advanced* section of the Add dialog.

Accounts are stored in `accounts.json` (+ `accounts/<id>.cookies.json`), deduplicated by Kick
username. **All of this is gitignored.**

---

## Project structure

```
launcher.py        Supervisor + control-center dashboard server (main app)
dashboard.html     Dashboard front-end (single-file SPA, served by launcher.py)
accounts.py        Multi-account store (import, dedup, persistence)
points_farmer.py   Per-account worker — drives one real Chrome on one channel
points_ui.py       Single-account UI (web + --menu console)
dashboard.py       Terminal points-ranking TUI
farmer.py          XP farmer worker (Pusher WebSocket) — per-account via launcher, or standalone
fetch_follows.py   Export an account's follows via the API
START.bat          One-click installer/launcher (Windows)
config.example.json  Config template
```

---

## How farming works (per worker)

1. Spawn `points_farmer.py` with the account's cookies + target channel via env vars.
2. Launch real Chrome (`--headless=new`, `--mute-audio`), inject the account cookies, and a
   `Bearer` token on Kick API/WS requests.
3. Navigate to `kick.com/<channel>`, start playback, keep `video.muted = false`.
4. The browser handles all the watch/WebSocket handshakes itself; the worker just keeps the
   player alive and reads the points balance.

The supervisor keeps the right set of workers running, restarts failures, and (optionally)
rotates accounts through a limited number of slots.

---

## XP Farmer (per account)

XP is separate from Channel Points: it credits from **WebSocket presence on a live stream**,
so no browser is needed and it's cheap to run on many accounts simultaneously.

In the dashboard's **XP Farmer** tab, toggle XP per account (or *Start all*). For each enabled
account the launcher spawns a `farmer.py` worker with that account's token, and:

1. The worker queries the account's **followed channels** and picks the live one with the most
   viewers — falling back to a configurable slug pool only if none of your follows are live.
   **You don't choose the streamer; selection is automatic.**
2. It subscribes to that stream's private Pusher WebSocket to register presence, and polls the
   gamification API to track level and XP gained.
3. The card shows the **currently selected stream**, viewer count, level, XP/min and ETA to the
   next level — so you always know exactly what each account is watching.
4. If the stream goes offline the worker rotates to another live channel automatically. Enabled
   accounts persist across launcher restarts, and crashed workers auto-restart.

`farmer.py` also runs **standalone** for a single account (`lancer.bat` / `python farmer.py`),
reading `session_token` from `config.json` — unchanged from the original tool.

---

## Security

- Dashboard binds to `127.0.0.1` only and rejects cross-site POSTs (Origin check).
- Tokens/cookies stay on disk locally and are gitignored — they are never sent anywhere except
  to Kick, and never exposed to the front-end.
- The front-end escapes all user-derived values and uses event delegation (no inline JS injection).

> [!NOTE]
> Cookies are stored in plaintext on disk (gitignored). At-rest encryption (Windows DPAPI) is a
> known follow-up — see the `ponytail:` note in `accounts.py`.

---

## Disclaimer

For personal and educational use only. This project automates viewer behavior (Channel Points
and XP) and is **not affiliated with or endorsed by Kick**. Automating rewards — especially
across multiple accounts — may breach [Kick's Terms of Service](https://kick.com/terms-of-service)
and can result in account sanctions or permanent bans. The software is provided **as-is, with no
warranty**. You are solely responsible for how you use it. If you don't accept that risk, don't
run it.
