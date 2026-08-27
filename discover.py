#!/usr/bin/env python3
"""List what CoolerControl sees, and optionally seed a `fan_banks` config.

The FANS panel needs to know which channels belong to which radiator or case
group — that is per-rig wiring nobody can guess for you. Run this to see every
device, channel and current RPM, then either edit `fan_banks` in config.json by
hand or start from `--write`.

    python3 discover.py            # list devices and channels
    python3 discover.py --write    # seed config.json with one bank per device

Identifying which physical fans a channel drives is easier if you make them
move: `ramptest.py` ramps one device at a time and restores the previous
settings afterwards.

Run this as your normal user — it writes your config, and the collector runs as
root only because the panel's IPC socket is root-owned.
"""
import json
import os
import sys

import requests
import urllib3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dash  # noqa: E402
import paths  # noqa: E402

urllib3.disable_warnings()


def fetch():
    s = requests.Session()
    # CoolerControl serves its API over HTTPS with a self-signed cert and ships no
    # CA to pin, and this only ever talks to loopback — same as dash.py does.
    s.verify = False
    s.post(f"{dash.CC}/login", auth=dash.CC_AUTH, timeout=4)
    names = {d["uid"]: d.get("name", "") for d in
             s.get(f"{dash.CC}/devices", timeout=4).json().get("devices", [])}
    data = s.post(f"{dash.CC}/status", json={}, timeout=4).json()
    out = []
    for dev in data.get("devices", []):
        uid = dev.get("uid", "")
        h = (dev.get("status_history") or [{}])[-1]
        fans = [(c.get("name"), c.get("rpm"), c.get("duty"))
                for c in h.get("channels", []) if c.get("rpm") is not None]
        temps = [(t.get("name"), t.get("temp")) for t in h.get("temps", [])]
        if fans or temps:
            out.append((uid, names.get(uid, ""), fans, temps))
    return out


def main():
    try:
        devs = fetch()
    except Exception as e:
        sys.exit(f"could not reach CoolerControl at {dash.CC}: {e}\n"
                 "Is coolercontrold running? Set DASH_CC_USER/DASH_CC_PASS if you "
                 "changed its login.")

    print(f"{'UID':10s} {'DEVICE':26s} CHANNELS")
    for uid, name, fans, temps in devs:
        spin = sum(1 for _, r, _ in fans if r and r > 0)
        print(f"{uid[:8]:10s} {name[:26]:26s} {len(fans)} fan ch "
              f"({spin} spinning){',  temps: ' + ', '.join(t for t, _ in temps) if temps else ''}")
        for ch, rpm, duty in fans:
            mark = "" if rpm else "   (stopped — unpopulated header?)"
            print(f"{'':10s}   {ch:8s} {str(rpm) + ' rpm':>10s}"
                  f"{'  ' + str(round(duty)) + '%' if duty is not None else ''}{mark}")

    if "--write" not in sys.argv:
        print("\nUse the 8-character UID as `device` in config.json's fan_banks.")
        print("Re-run with --write to seed a starter config.")
        return

    cfg = {}
    if os.path.exists(paths.CONFIG_FILE):
        try:
            cfg = json.load(open(paths.CONFIG_FILE)) or {}
        except ValueError:
            sys.exit(f"{paths.CONFIG_FILE} is not valid JSON — fix or move it first.")
    if cfg.get("fan_banks"):
        sys.exit("config.json already has fan_banks; edit it by hand rather than "
                 "letting this overwrite your wiring.")

    banks = []
    for uid, name, fans, _t in devs:
        if not any(r for _c, r, _d in fans):
            continue                       # no spinning fans: nothing to show
        banks.append({"label": (name or uid)[:12].upper(),
                      "left": {"device": uid[:8], "channels": None},
                      "right": None})
    if not banks:
        sys.exit("no devices with spinning fans found — nothing to seed.")

    cfg["fan_banks"] = banks[:4]           # the panel has room for four rows
    cfg.setdefault("fan_columns", ["FANS", ""])
    os.makedirs(os.path.dirname(paths.CONFIG_FILE), exist_ok=True)
    tmp = paths.CONFIG_FILE + ".tmp"
    json.dump(cfg, open(tmp, "w"), indent=2)
    os.replace(tmp, paths.CONFIG_FILE)
    print(f"\nwrote {len(cfg['fan_banks'])} bank(s) to {paths.CONFIG_FILE}")
    print("Edit the labels and split channels across left/right to taste.")


if __name__ == "__main__":
    main()
