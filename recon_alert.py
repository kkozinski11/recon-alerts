#!/usr/bin/env python3
"""
Hurricane Hunter alert script.

Watches two official National Hurricane Center (NHC) sources and pings your
phone through ntfy (free app, no account needed):

  1. Live flights  - new HDOB observations (sent every ~10 min by aircraft in
                     the air) in NHC's recon archive. New mission = takeoff alert.
                     No data for a while = mission-ended alert.
  2. Flight plans  - NHC's daily Recon "Plan of the Day". Alerts when a new
                     plan with scheduled flights is posted.
  3. Dropsondes    - surface pressure and wind from each new drop, bundled
                     into one notification per aircraft per check.

Setup:
  1. Install the ntfy app (iOS/Android) and subscribe to a unique topic name,
     e.g. "kelly-recon-8h3k2" (anyone who knows the name can read it, so make
     it hard to guess).
  2. Set NTFY_TOPIC below (or the NTFY_TOPIC environment variable).
  3. Run:  python3 recon_alert.py           (loops forever, checks every 10 min)
     or:   python3 recon_alert.py --once    (single check)
     or:   python3 recon_alert.py --for 50  (check every few min for 50 min; GitHub)
     Test: python3 recon_alert.py --test    (sends a test notification)

Only uses the Python standard library.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------- Settings ----------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-a-unique-topic")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# AHONT1 = Atlantic Air Force (53rd WRS), AHONT2 = Atlantic NOAA.
# Add "AHOPN1", "AHOPN2" for Eastern Pacific if you want those too.
FEEDS = ["AHONT1", "AHONT2"]

CHECK_EVERY_MIN = 5         # minutes between checks when looping
LOOKBACK_HOURS = 12         # NHC sometimes posts obs hours late; look back this far
ENDED_AFTER_MIN = 120       # no new obs for this long = mission over
WATCH_PLAN_OF_DAY = True

# Dropsondes: one notification per mission per check, with surface pressure + wind.
WATCH_DROPS = True
DROP_FEEDS = ["REPNT3"]     # Atlantic dropsondes (Air Force + NOAA)
LIVE_DROP_URL = "https://www.nhc.noaa.gov/text/MIAREPNT3.shtml"

STATE_FILE = os.environ.get("STATE_FILE", "recon_state.json")
UA = "recon-alert-script (personal use)"
ARCHIVE = "https://www.nhc.noaa.gov/archive/recon/{year}/{feed}/"
POD_URL = "https://www.nhc.noaa.gov/text/MIAREPRPD.shtml"
# ------------------------------------------


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def notify(title, message, priority="default", tags="airplane"):
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30)
    print(f"[notify] {title}: {message}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("active", {})
    state.setdefault("seen_files", [])
    state.setdefault("done", [])
    state.setdefault("pod_hash", None)
    return state


def save_state(state):
    state["seen_files"] = state["seen_files"][-2000:]  # keep file small
    state["done"] = state["done"][-200:]
    if "seen_drops" in state:
        state["seen_drops"] = state["seen_drops"][-2000:]
        state["drop_keys"] = state.get("drop_keys", [])[-2000:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def parse_hdob(text):
    """Pull aircraft, mission and storm from an HDOB message.
    Example line: 'AF305 0212A KATRINA     HDOB 15 20050828'"""
    m = re.search(r"^\s*((?:AF|NOAA)\d+)\s+(\S+)\s+(\S+)\s+HDOB\s+(\d+)", text, re.M)
    if not m:
        return None
    aircraft, mission, storm, obnum = m.groups()
    return {"aircraft": aircraft, "mission": mission, "storm": storm, "ob": int(obnum)}


def check_flights(state, now):
    for feed in FEEDS:
        url = ARCHIVE.format(year=now.year, feed=feed)
        try:
            listing = fetch(url)
        except Exception as e:
            print(f"[warn] {feed}: {e}")
            continue

        files = set(re.findall(rf'({feed}-K\w+\.(\d{{12}})\.txt)', listing))
        new = []
        for name, stamp in files:
            t = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            if now - t <= timedelta(hours=LOOKBACK_HOURS) and name not in state["seen_files"]:
                new.append((t, name))

        new.sort()
        state["seen_files"].extend(name for _, name in new)
        for t, name in new[-40:]:
            try:
                info = parse_hdob(fetch(url + name))
            except Exception as e:
                print(f"[warn] {name}: {e}")
                continue
            if not info:
                continue
            key = f'{info["aircraft"]}-{info["mission"]}'
            if key in state["done"]:
                continue  # late data from a mission we already closed out
            if key not in state["active"]:
                if now - t > timedelta(minutes=ENDED_AFTER_MIN):
                    state["done"].append(key)  # finished before we heard about it
                    continue
                who = "NOAA" if info["aircraft"].startswith("NOAA") else "Air Force"
                notify(
                    f"Hurricane Hunters airborne: {info['storm']}",
                    f"{who} {info['aircraft']} (mission {info['mission']}) is "
                    f"sending data from {info['storm']}. Latest obs {t:%H:%M} UTC.",
                    priority="high",
                )
                state["active"][key] = {**info, "last_obs": t.isoformat()}
            prev = datetime.fromisoformat(state["active"][key]["last_obs"])
            state["active"][key].update(last_obs=max(prev, t).isoformat(),
                                        last_heard=now.isoformat())

    # Missions that went quiet (judged by when we last received new data)
    for key, info in list(state["active"].items()):
        heard = datetime.fromisoformat(info.get("last_heard", info.get("last_seen")))
        if now - heard > timedelta(minutes=ENDED_AFTER_MIN):
            last = datetime.fromisoformat(info.get("last_obs", info.get("last_seen")))
            notify(
                f"Recon mission ended: {info['storm']}",
                f"{info['aircraft']} ({info['mission']}) last reported {last:%H:%M} UTC.",
                priority="low",
                tags="checkered_flag",
            )
            del state["active"][key]
            state["done"].append(key)


def _wind(group):
    """Decode a dddff wind group (knots). '21615' -> (215, 115)."""
    if not re.fullmatch(r"\d{5}", group or ""):
        return None
    d, f = int(group[:3]), int(group[3:])
    extra = d % 5
    return d - extra, f + 100 * extra


def parse_drop(text):
    """Pull surface pressure (mb) and the best near-surface wind from a TEMP DROP."""
    info = {"pressure": None, "wind": None, "wind_src": None,
            "aircraft": "?", "mission": "", "storm": "", "note": "", "ob": ""}

    m = re.search(r"61616\s+((?:AF|NOAA)\d+)\s+(\S+)\s+(.*?)\s+OB\s+(\d+)", text)
    if m:
        info["ob"] = m.group(4)
        info["aircraft"], info["mission"] = m.group(1), m.group(2)
        name = m.group(3).split()[0] if m.group(3).split() else ""
        info["storm"] = name if name.isalpha() and name not in ("SURV",) else ""

    tokens = text.split()
    if "XXAA" in tokens:                                   # Part A surface group
        g = tokens[tokens.index("XXAA") + 1:]
        if len(g) > 6 and g[4].startswith("99") and g[4][2:].isdigit():
            p = int(g[4][2:])
            info["pressure"] = p + 1000 if p < 100 else p
            w = _wind(g[6])
            if w:
                info["wind"], info["wind_src"] = w, "sfc"
    if info["pressure"] is None and "XXBB" in tokens:     # Part B fallback
        g = tokens[tokens.index("XXBB") + 1:]
        if len(g) > 4 and g[4].startswith("00") and g[4][2:].isdigit():
            p = int(g[4][2:])
            info["pressure"] = p + 1000 if p < 100 else p

    if info["wind"] is None:                               # remarks fallbacks
        for label, pat in (("lowest 150 m", r"WL150\s+(\d{5})"), ("boundary layer", r"MBL WND\s+(\d{5})")):
            m = re.search(pat, text)
            if m and _wind(m.group(1)):
                info["wind"], info["wind_src"] = _wind(m.group(1)), label
                break

    if re.search(r"62626\s+EYEWALL", text):
        info["note"] = "eyewall"
    elif re.search(r"62626\s+(?:EYE|CENTER)\b", text):
        info["note"] = "center"
    return info


def _header_time(text, now):
    """Turn a 'UZNT13 KNHC 091842' header (day/hour/min) into a datetime."""
    m = re.search(r"UZNT13\s+K\w+\s+(\d{2})(\d{2})(\d{2})", text)
    if not m:
        return now
    day, hh, mm = map(int, m.groups())
    try:
        t = now.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        t = now
    if t > now + timedelta(hours=1):                      # header from last month
        prev = now.replace(day=1) - timedelta(days=1)
        try:
            t = prev.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            t = now
    return t


def check_drops(state, now):
    first_run = "seen_drops" not in state
    state.setdefault("seen_drops", [])
    state.setdefault("drop_keys", [])
    candidates = []  # (time, message text, source)

    # Source 1: NHC archive (complete, but sometimes posted late)
    for feed in DROP_FEEDS:
        url = ARCHIVE.format(year=now.year, feed=feed)
        try:
            listing = fetch(url)
        except Exception as e:
            print(f"[warn] {feed}: {e}")
            continue
        found = set(re.findall(rf'({feed}-K\w+\.(\d{{12}})\.txt)', listing))
        new = []
        for name, stamp in found:
            t = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            if now - t <= timedelta(hours=LOOKBACK_HOURS) and name not in state["seen_drops"]:
                new.append((t, name))
        new.sort()
        print(f"[info] {feed} archive: {len(found)} files this year, {len(new)} new")
        state["seen_drops"].extend(name for _, name in new)
        for t, name in new[-40:]:
            try:
                candidates.append((t, fetch(url + name), "archive"))
            except Exception as e:
                print(f"[warn] {name}: {e}")

    # Source 2: NHC's live "latest dropsonde" page (fast, but only the newest few)
    try:
        page = re.sub(r"<[^>]+>", " ", fetch(LIVE_DROP_URL))
        msgs = [m for m in re.split(r"(?=UZNT13\s+K)", page) if m.startswith("UZNT13")]
        print(f"[info] live drop page: {len(msgs)} messages")
        for m in msgs:
            t = _header_time(m, now)
            if now - t <= timedelta(hours=LOOKBACK_HOURS):
                candidates.append((t, m, "live"))
    except Exception as e:
        print(f"[warn] live drop page: {e}")

    batches = {}
    for t, text, src in sorted(candidates, key=lambda c: c[0]):
        d = parse_drop(text)
        key = f"{d['aircraft']}-{d['mission']}-OB{d['ob']}-{t:%d%H}"
        if key in state["drop_keys"]:
            continue
        state["drop_keys"].append(key)
        if first_run:
            continue  # don't flood you with old drops the first time
        if d["pressure"] is None and d["wind"] is None:
            print(f"[info] skipped drop {key} ({src}): no surface pressure or wind")
            continue
        parts = [f"{t:%H:%M}Z"]
        parts.append(f"{d['pressure']} mb" if d["pressure"] else "pressure n/a")
        if d["wind"]:
            kt = d["wind"][1]
            parts.append(f"{kt} kt ({round(kt * 1.15078)} mph)"
                         + ("" if d["wind_src"] == "sfc" else f" [{d['wind_src']}]"))
        else:
            parts.append("wind n/a")
        if d["note"]:
            parts.append(d["note"])
        print(f"[info] new drop {key} ({src}): {', '.join(parts)}")
        batches.setdefault((d["storm"] or "Recon", d["aircraft"]), []).append(", ".join(parts))

    for (storm, aircraft), lines in batches.items():
        more = f"\n+{len(lines) - 10} more" if len(lines) > 10 else ""
        notify(f"Dropsonde{'s' if len(lines) > 1 else ''}: {storm} ({aircraft})",
               "\n".join(lines[-10:]) + more, tags="arrow_down")


def check_plan_of_day(state):
    try:
        html = fetch(POD_URL)
    except Exception as e:
        print(f"[warn] plan of day: {e}")
        return
    m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S | re.I)
    text = re.sub(r"<[^>]+>", "", m.group(1) if m else html).strip()
    h = hashlib.sha256(text.encode()).hexdigest()
    if h == state.get("pod_hash"):
        return
    first_run = state.get("pod_hash") is None
    state["pod_hash"] = h
    if first_run:
        return  # don't alert on whatever plan already exists at startup

    # Skip "nothing scheduled" plans
    no_flights = re.search(r"NEGATIVE RECONNAISSANCE|NO RECON", text, re.I) and not re.search(r"FLIGHT\s+(ONE|TWO|THREE)", text, re.I)
    if no_flights:
        return
    storms = sorted(set(re.findall(r"\d\.\s+(?:HURRICANE|TROPICAL STORM|TROPICAL DEPRESSION|POTENTIAL TROPICAL CYCLONE|SUSPECT AREA)[^\n]*", text)))
    summary = "; ".join(s.strip() for s in storms)[:300] or "New recon flights scheduled."
    notify("New Recon Plan of the Day", summary + "\n" + POD_URL, tags="calendar")


def run_once():
    state = load_state()
    now = datetime.now(timezone.utc)
    check_flights(state, now)
    if WATCH_PLAN_OF_DAY:
        check_plan_of_day(state)
    if WATCH_DROPS:
        check_drops(state, now)
    save_state(state)


def main():
    if NTFY_TOPIC.startswith("change-me"):
        sys.exit("Set NTFY_TOPIC first (edit the script or set the env variable).")
    if "--test" in sys.argv:
        notify("Recon alerts test", "If you see this, notifications work!")
        return
    if "--once" in sys.argv:
        run_once()
        return
    if "--for" in sys.argv:  # loop for N minutes, then exit (used by GitHub)
        stop = time.time() + float(sys.argv[sys.argv.index("--for") + 1]) * 60
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"[error] {e}")
            if time.time() + CHECK_EVERY_MIN * 60 > stop:
                return
            time.sleep(CHECK_EVERY_MIN * 60)
    print(f"Watching {', '.join(FEEDS)} every {CHECK_EVERY_MIN} min. Ctrl+C to stop.")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[error] {e}")
        time.sleep(CHECK_EVERY_MIN * 60)


if __name__ == "__main__":
    main()
