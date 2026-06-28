"""Headless orchestrator for Kick Drops mining.

Port of the kick-drops-miner customtkinter App orchestration (queue, single
worker, offline channel-switching, cumulative global drops, auto-start, offline
retry monitor) with the tkinter UI stripped out. Selenium core is reused as-is.

Kick only allows ONE stream watched at a time, so the queue runs a single
StreamWorker and rotates through items. State is exposed via state() for the web
dashboard; actions return immediately and heavy work runs on background threads.
"""
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core.config import Config
from core.worker import StreamWorker
from core.api import (
    kick_is_live_by_api,
    kick_live_status_by_api,
    fetch_drops_campaigns_and_progress,
    fetch_live_streamers_by_category,
    fetch_kick_username,
    is_campaign_expired,
)
from core.browser import CookieManager
from utils.helpers import domain_from_url, cookie_file_for_domain, set_debug_config

SWITCH_DELAY = 8.0  # seconds to let a switched-to stream load before re-checking


class DropsManager:
    def __init__(self):
        self.cfg = Config()
        set_debug_config(self.cfg)
        self.workers = {}            # idx -> StreamWorker (0 or 1 entries, Kick limit)
        self.queue_running = False
        self.queue_current_idx = None
        self.status = ""
        self.runtime = {}            # idx -> {seconds, live, phase}
        self.lock = threading.RLock()
        self._cache = {"ts": 0.0, "campaigns": [], "progress": []}
        self._fetching = False
        self.username = None
        self._monitor_started = False
        self.live = {}  # url -> True/False/None(unknown), for non-running queue items

    # ---------------- lifecycle ----------------
    def start_monitor(self):
        if self._monitor_started:
            return
        self._monitor_started = True
        threading.Thread(target=self._offline_retry_monitor, daemon=True).start()
        threading.Thread(target=self._live_status_refresher, daemon=True).start()
        if self.cfg.auto_start and self.cfg.items:
            self.start_all()

    # ---------------- state ----------------
    def state(self):
        with self.lock:
            items = []
            for i, it in enumerate(self.cfg.items):
                rt = self.runtime.get(i, {})
                running = i in self.workers
                is_global = it.get("is_global_drop", False)
                if is_global:
                    disp = (it.get("cumulative_time", 0) + rt.get("seconds", 0)) // 60
                    disp_str = f"{disp}m"
                else:
                    disp_str = f"{rt.get('seconds', 0)}s"
                phase = rt.get("phase", "finished" if it.get("finished") else "idle")
                if running and phase in ("idle", "finished"):
                    phase = "live" if rt.get("live", True) else "paused"
                if running:
                    live = rt.get("live", True)
                else:
                    live = self.live.get(it["url"])  # True/False/None(unknown)
                items.append({
                    "idx": i,
                    "url": it["url"],
                    "username": it["url"].rstrip("/").split("/")[-1],
                    "minutes": it.get("minutes", 0),
                    "campaign_id": it.get("campaign_id"),
                    "is_global_drop": is_global,
                    "finished": bool(it.get("finished")),
                    "running": running,
                    "live": live,
                    "phase": phase,
                    "display": disp_str,
                    "channels_count": len(it.get("campaign_channels", []) or []),
                })
            return {
                "items": items,
                "queue_running": self.queue_running,
                "status": self.status,
                "username": self.username,
                "settings": {
                    "mute": self.cfg.mute,
                    "hide_player": self.cfg.hide_player,
                    "mini_player": self.cfg.mini_player,
                    "force_160p": self.cfg.force_160p,
                    "auto_start": self.cfg.auto_start,
                    "language": self.cfg.language,
                    "chromedriver_path": self.cfg.chromedriver_path or "",
                    "extension_path": self.cfg.extension_path or "",
                },
            }

    # ---------------- settings ----------------
    def set_setting(self, key, value):
        with self.lock:
            if key not in ("mute", "hide_player", "mini_player", "force_160p",
                           "auto_start", "language", "chromedriver_path",
                           "extension_path", "debug"):
                return False
            setattr(self.cfg, key, value)
            self.cfg.save()
            # mute/hide/mini apply live to running workers
            for w in list(self.workers.values()):
                try:
                    w.mute = self.cfg.mute
                    w.hide_player = self.cfg.hide_player
                    w.mini_player = self.cfg.mini_player
                    w.ensure_player_state()
                except Exception:
                    pass
            if key == "auto_start" and value and not self.queue_running and self.cfg.items:
                self.start_all()
            return True

    # ---------------- queue items ----------------
    def add_item(self, url, minutes=120, campaign_id=None, campaign_channels=None,
                 required_category_id=None, is_global_drop=False):
        with self.lock:
            if self._find_index(url) is not None:
                return False
            self.cfg.add(url, int(minutes), campaign_id, campaign_channels or [],
                         required_category_id, bool(is_global_drop))
            return True

    def remove_item(self, idx):
        with self.lock:
            if idx in self.workers:
                self.workers[idx].stop()
            if 0 <= idx < len(self.cfg.items):
                self.cfg.remove(idx)
                self._reindex()
            return True

    def remove_by_url(self, url):
        with self.lock:
            idx = self._find_index(url)
            if idx is None:
                return False
            return self.remove_item(idx)

    def clear_items(self):
        with self.lock:
            self.stop_all()
            self.cfg.items = []
            self.cfg.save()
            self.runtime.clear()
            return True

    # ---------------- start/stop ----------------
    def start_index(self, idx):
        threading.Thread(target=self._start_index, args=(idx,), daemon=True).start()

    def start_all(self):
        with self.lock:
            self.queue_running = True
            self.queue_current_idx = None
        threading.Thread(target=self._run_queue_from, args=(0,), daemon=True).start()

    def stop_index(self, idx):
        with self.lock:
            if idx in self.workers:
                self.workers[idx].stop()
            self.runtime.pop(idx, None)
            return True

    def stop_all(self):
        with self.lock:
            self.queue_running = False
            self.queue_current_idx = None
            for w in list(self.workers.values()):
                w.stop()
            self.runtime.clear()
            self.status = "stopped"
            return True

    # ---------------- core orchestration (port of App._start_index) ----------------
    def _start_index(self, idx):
        with self.lock:
            # Kick allows only one stream: stop any current worker first.
            if self.workers:
                for ridx, worker in list(self.workers.items()):
                    worker.stop()
                    del self.workers[ridx]
                    if ridx < len(self.cfg.items):
                        self.cfg.items[ridx]["finished"] = False
        time.sleep(2)

        if not (0 <= idx < len(self.cfg.items)):
            return
        item = self.cfg.items[idx]

        # If current channel offline, try a live alternative from same campaign.
        if not kick_is_live_by_api(item["url"]):
            if self._try_switch_channel(idx, item, schedule_retry=True):
                return
            self._set_phase(idx, "retry")
            self.status = f"offline, waiting to retry: {item['url']}"
            return

        domain = domain_from_url(item["url"])
        if not domain:
            self.status = f"invalid url: {item['url']}"
            return

        # Auto-import cookies if missing; skip item if unavailable (no popups headless).
        cookie_path = cookie_file_for_domain(domain)
        if not os.path.exists(cookie_path):
            try:
                if not CookieManager.import_from_browser(domain):
                    self.status = f"skipping {item['url']} - no cookies"
                    return
            except Exception:
                self.status = f"skipping {item['url']} - no cookies"
                return

        cumulative_cb = None
        if item.get("is_global_drop"):
            campaign_id = item.get("campaign_id")

            def cumulative_cb():
                if not campaign_id:
                    return 0
                return sum(o.get("cumulative_time", 0) for o in self.cfg.items
                           if o.get("campaign_id") == campaign_id)

        worker = StreamWorker(
            item["url"], item["minutes"],
            on_update=lambda s, live, i=idx: self._on_update(i, s, live),
            on_finish=lambda e, c, i=idx: self._on_finish(i, e, c),
            stop_event=threading.Event(),
            driver_path=self.cfg.chromedriver_path,
            extension_path=self.cfg.extension_path,
            hide_player=bool(self.cfg.hide_player),
            mute=bool(self.cfg.mute),
            mini_player=bool(self.cfg.mini_player),
            force_160p=bool(self.cfg.force_160p),
            required_category_id=item.get("required_category_id"),
            cumulative_time_callback=cumulative_cb,
        )
        with self.lock:
            self.workers[idx] = worker
            self.runtime[idx] = {"seconds": 0, "live": True, "phase": "live"}
        worker.start()
        self.status = f"playing: {item['url']}"

    def _run_queue_from(self, start_idx):
        with self.lock:
            if self.workers:
                return
            for i in range(start_idx, len(self.cfg.items)):
                if self.cfg.items[i].get("finished"):
                    continue
                self.queue_current_idx = i
                target = i
                break
            else:
                self.queue_running = False
                self.queue_current_idx = None
                self.status = "queue finished"
                return
        self._start_index(target)
        # If start_index didn't take (offline/no cookies), advance after a beat.
        with self.lock:
            took = target in self.workers
        if not took:
            time.sleep(3)
            if self.queue_running:
                self._run_queue_from(target + 1)

    # ---------------- channel switching ----------------
    def _try_switch_channel(self, idx, item, schedule_retry):
        channels = item.get("campaign_channels", []) or []
        if not channels:
            return False
        current = item["url"]
        tried = item.get("tried_channels", [])
        if current not in tried:
            tried.append(current)
        all_urls = [c.get("url") if isinstance(c, dict) else c for c in channels]
        all_urls = [u for u in all_urls if u]
        if current not in all_urls:
            all_urls.append(current)
        if len(tried) >= len(all_urls):
            tried.clear()
        for c in channels:
            alt = c.get("url") if isinstance(c, dict) else c
            if alt and alt != current and alt not in tried and kick_is_live_by_api(alt):
                with self.lock:
                    self.cfg.items[idx]["url"] = alt
                    tried.append(alt)
                    self.cfg.items[idx]["tried_channels"] = tried
                    self.cfg.save()
                self.status = f"switched to {alt.rstrip('/').split('/')[-1]} - loading..."
                if schedule_retry:
                    threading.Timer(SWITCH_DELAY, self._start_index, args=(idx,)).start()
                return True
        item["tried_channels"] = tried
        self.cfg.save()
        return False

    # ---------------- worker callbacks (port of App.on_worker_*) ----------------
    def _on_update(self, idx, seconds, live):
        with self.lock:
            self.runtime[idx] = {"seconds": seconds, "live": live,
                                 "phase": "live" if live else "paused"}

    def _on_finish(self, idx, elapsed, completed):
        with self.lock:
            if not (0 <= idx < len(self.cfg.items)):
                return
            worker = self.workers.get(idx)
            ended_offline = bool(worker and getattr(worker, "ended_because_offline", False))
            ended_wrong_cat = bool(worker and getattr(worker, "ended_because_wrong_category", False))
            self.workers.pop(idx, None)

            item = self.cfg.items[idx]
            is_global = item.get("is_global_drop", False)
            campaign_id = item.get("campaign_id")
            completed_value = completed

            if is_global and campaign_id:
                for o in self.cfg.items:
                    if o.get("campaign_id") == campaign_id:
                        o["cumulative_time"] = o.get("cumulative_time", 0) + elapsed
                self.cfg.save()
                target_min = item.get("minutes", 0)
                cum_min = item.get("cumulative_time", 0) // 60
                if target_min > 0 and cum_min >= target_min:
                    for o in self.cfg.items:
                        if o.get("campaign_id") == campaign_id:
                            o["finished"] = True
                    self.cfg.save()
                    completed_value = True
                else:
                    completed_value = False

            if completed_value:
                if not is_global:
                    item["finished"] = True
                item["tried_channels"] = []
                self.cfg.save()
                self.runtime[idx] = {"seconds": elapsed, "live": False, "phase": "finished"}
            elif ended_offline or ended_wrong_cat:
                switched = self._try_switch_channel(idx, item, schedule_retry=False)
                if switched and self.queue_running:
                    threading.Timer(SWITCH_DELAY, self._start_index, args=(idx,)).start()
                    return
                if not switched:
                    self.runtime[idx] = {"seconds": elapsed, "live": False, "phase": "retry"}
                    self.status = f"offline, waiting to retry: {item['url']}"

            continue_queue = self.queue_running and self.queue_current_idx == idx

        if continue_queue:
            self._run_queue_from(idx + 1)

    # ---------------- offline retry monitor ----------------
    def _offline_retry_monitor(self):
        while True:
            time.sleep(30)
            try:
                with self.lock:
                    if not self.queue_running or self.workers:
                        continue
                    candidate = None
                    for idx, item in enumerate(self.cfg.items):
                        if item.get("finished") or idx in self.workers:
                            continue
                        candidate = (idx, item["url"])
                        break
                if candidate and kick_is_live_by_api(candidate[1]):
                    self._start_index(candidate[0])
            except Exception as e:
                print(f"Drops monitor error: {e}")
                time.sleep(60)

    # ---------------- live status (queue column) ----------------
    def _live_status_refresher(self):
        while True:
            try:
                with self.lock:
                    urls = [it["url"] for i, it in enumerate(self.cfg.items)
                            if i not in self.workers and not it.get("finished")]
                for url in urls:
                    self.live[url] = kick_live_status_by_api(url)  # True/False/None
                    time.sleep(0.5)  # ponytail: gentle pacing, avoid rate-limit
            except Exception as e:
                print(f"Drops live refresher error: {e}")
            time.sleep(45)

    # ---------------- campaigns / progress ----------------
    def get_campaigns(self, refresh=False, max_age=120):
        if refresh or (time.time() - self._cache["ts"] > max_age):
            if not self._fetching:
                threading.Thread(target=self._fetch_campaigns, daemon=True).start()
        return {
            "campaigns": self._cache["campaigns"],
            "progress": self._cache["progress"],
            "ts": self._cache["ts"],
            "loading": self._fetching,
        }

    def _fetch_campaigns(self):
        self._fetching = True
        driver = None
        try:
            res = fetch_drops_campaigns_and_progress()
            driver = res.get("driver")
            campaigns = [c for c in res.get("campaigns", []) if not is_campaign_expired(c)]
            self._cache = {"ts": time.time(), "campaigns": campaigns,
                           "progress": res.get("progress", [])}
            self.username = fetch_kick_username() or self.username
        except Exception as e:
            print(f"Drops fetch error: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            self._fetching = False

    def find_streamers(self, category_id, limit=24):
        try:
            return fetch_live_streamers_by_category(category_id, limit=limit)
        except Exception as e:
            print(f"find_streamers error: {e}")
            return []

    def find_and_add(self, campaign, minutes=120, limit=24):
        """Category-based campaign: find live streamers in its category and queue
        them as cumulative global drops locked to that category."""
        cid = campaign.get("category_id")
        if not cid:
            return {"ok": False, "added": 0, "error": "no category_id"}
        streamers = self.find_streamers(cid, limit=limit)
        chans = [{"url": s["url"], "username": s.get("username")} for s in streamers]
        added = 0
        for ch in chans:
            ok = self.add_item(
                ch["url"], minutes,
                campaign_id=campaign.get("id"),
                campaign_channels=chans,
                required_category_id=cid,
                is_global_drop=True,
            )
            if ok:
                added += 1
        return {"ok": True, "added": added, "found": len(streamers)}

    # ---------------- campaign channel helpers ----------------
    def add_channel(self, url, minutes=120, campaign=None, is_global_drop=False):
        campaign = campaign or {}
        return self.add_item(
            url, minutes,
            campaign_id=campaign.get("id"),
            campaign_channels=campaign.get("channels", []),
            required_category_id=campaign.get("category_id") if is_global_drop else None,
            is_global_drop=is_global_drop,
        )

    def add_all_campaign_channels(self, campaign, minutes=120, is_global_drop=False):
        added = 0
        for ch in campaign.get("channels", []) or []:
            url = ch.get("url") if isinstance(ch, dict) else ch
            if url and self.add_channel(url, minutes, campaign, is_global_drop):
                added += 1
        return added

    def remove_campaign_channels(self, campaign_id):
        with self.lock:
            keep = [it for it in self.cfg.items if it.get("campaign_id") != campaign_id]
            removed = len(self.cfg.items) - len(keep)
            for i, it in enumerate(self.cfg.items):
                if it.get("campaign_id") == campaign_id and i in self.workers:
                    self.workers[i].stop()
            self.cfg.items = keep
            self.cfg.save()
            self._reindex()
            return removed

    # ---------------- internals ----------------
    def _find_index(self, url):
        for i, it in enumerate(self.cfg.items):
            if it["url"] == url:
                return i
        return None

    def _set_phase(self, idx, phase):
        with self.lock:
            rt = self.runtime.get(idx, {"seconds": 0, "live": False})
            rt["phase"] = phase
            self.runtime[idx] = rt

    def _reindex(self):
        # Worker idx keys become stale after list mutation; safest to stop+clear.
        for w in list(self.workers.values()):
            w.stop()
        self.workers.clear()
        self.runtime.clear()
        if self.queue_running:
            self.queue_current_idx = None


# Singleton used by the launcher.
MANAGER = DropsManager()


if __name__ == "__main__":
    # ponytail: smoke test — no Selenium, just queue bookkeeping.
    m = DropsManager()
    m.cfg.items = []
    assert m.add_item("https://kick.com/foo", 120) is True
    assert m.add_item("https://kick.com/foo", 120) is False  # dup rejected
    st = m.state()
    assert len(st["items"]) == 1 and st["items"][0]["username"] == "foo"
    assert m.remove_by_url("https://kick.com/foo") is True
    assert m.state()["items"] == []
    print("manager smoke test OK")
