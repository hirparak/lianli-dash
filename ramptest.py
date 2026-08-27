#!/usr/bin/env python3
"""Work out which physical fans a CoolerControl device/channel actually drives.

`discover.py` tells you what channels exist; this tells you where they are, by
spinning one device up while the rest stay idle so you can hear and see which
radiator or case position responds. That mapping is what you then write into
`fan_banks` in config.json.

    ramptest.py list                       # devices with controllable fans
    ramptest.py save                       # snapshot settings -> /tmp/cc_restore.json
    ramptest.py ramp <device> [duty]       # spin one device's fans up
    ramptest.py restore                    # put everything back

`<device>` is a UID prefix or a case-insensitive device-name substring.

ALWAYS `save` before your first `ramp`, and `restore` when finished: ramping
switches channels to a fixed duty, and without the snapshot they stay there
instead of returning to their curve.

Pump channels are skipped. A pump wired to a fan header will happily obey a duty
change, and dropping your pump to 40% while a loop is under load is not a mistake
you want to make by accident. Channels are skipped when the device is a pump
(d5next) or the channel is a known non-fan header; add more with --exclude.
"""
import json
import sys

import requests
import urllib3

urllib3.disable_warnings()

CC = "https://localhost:11987"
AUTH = ("CCAdmin", "coolAdmin")
SNAP = "/tmp/cc_restore.json"

# Headers that are not case fans. The Octo's fan9 is its flow meter; a D5 Next's
# fan1 is the pump itself, and rigs often park a second pump on a spare header.
SKIP_CHANNELS = {"fan9"}
SKIP_DEVICES = ("d5next", "highflownext")


def session():
    s = requests.Session()
    # CoolerControl uses a self-signed cert on loopback and ships no CA to pin.
    s.verify = False
    s.post(f"{CC}/login", auth=AUTH, timeout=5)
    return s


def devices(s):
    return s.get(f"{CC}/devices", timeout=5).json()["devices"]


def pick(s, sel):
    """Resolve a UID prefix or name substring to exactly one device."""
    sel = sel.lower()
    devs = devices(s)
    hits = [d for d in devs if d["uid"].lower().startswith(sel)]
    if not hits:
        hits = [d for d in devs if sel in (d.get("name") or "").lower()]
    if not hits:
        sys.exit(f"no device matches {sel!r} — try: ramptest.py list")
    if len(hits) > 1:
        print("ambiguous; matches:", file=sys.stderr)
        for d in hits:
            print(f"  {d['uid'][:12]}  {d.get('name')}", file=sys.stderr)
        sys.exit("use a longer UID prefix")
    return hits[0]


def fan_channels(s, dev, extra_skip=()):
    name = (dev.get("name") or "").lower()
    if any(p in name for p in SKIP_DEVICES):
        sys.exit(f"{dev.get('name')} is a pump/flow device — refusing to ramp it")
    st = s.get(f"{CC}/devices/{dev['uid']}/settings", timeout=5).json()["settings"]
    return [c["channel_name"] for c in st
            if c["channel_name"].startswith("fan")
            and c["channel_name"] not in SKIP_CHANNELS
            and c["channel_name"] not in extra_skip]


def cmd_list(s):
    for d in devices(s):
        name = (d.get("name") or "")
        if any(p in name.lower() for p in SKIP_DEVICES):
            continue
        try:
            chans = fan_channels(s, d)
        except SystemExit:
            continue
        if chans:
            print(f"  {d['uid'][:12]}  {name[:28]:28s} {', '.join(chans)}")


def cmd_save(s):
    out = {}
    for d in devices(s):
        if any(p in (d.get("name") or "").lower() for p in SKIP_DEVICES):
            continue
        st = s.get(f"{CC}/devices/{d['uid']}/settings", timeout=5).json()["settings"]
        chans = {c["channel_name"]: {"speed_fixed": c.get("speed_fixed"),
                                     "profile_uid": c.get("profile_uid")}
                 for c in st if c["channel_name"].startswith("fan")}
        if chans:
            out[d["uid"]] = {"name": d.get("name"), "channels": chans}
    json.dump(out, open(SNAP, "w"), indent=1)
    for uid, v in out.items():
        print(f"{v['name']} ({uid[:12]}): {len(v['channels'])} channels")
    print(f"\nsaved -> {SNAP}")


def cmd_ramp(s, sel, duty, extra_skip):
    dev = pick(s, sel)
    chans = fan_channels(s, dev, extra_skip)
    if not chans:
        sys.exit("no rampable fan channels on that device")
    print(f"{dev.get('name')} ({dev['uid'][:12]}) -> {duty}%")
    for ch in chans:
        r = s.put(f"{CC}/devices/{dev['uid']}/settings/{ch}/manual",
                  json={"speed_fixed": duty}, timeout=5)
        print(f"  {ch:6s} HTTP {r.status_code}")
    print("\nlisten for which fans spun up, then: ramptest.py restore")


def cmd_restore(s):
    try:
        snap = json.load(open(SNAP))
    except OSError:
        sys.exit(f"no snapshot at {SNAP} — nothing to restore")
    for uid, v in snap.items():
        for ch, cs in v["channels"].items():
            if ch in SKIP_CHANNELS:
                continue
            if cs["profile_uid"]:
                r = s.put(f"{CC}/devices/{uid}/settings/{ch}/profile",
                          json={"profile_uid": cs["profile_uid"]}, timeout=5)
            elif cs["speed_fixed"] is not None:
                r = s.put(f"{CC}/devices/{uid}/settings/{ch}/manual",
                          json={"speed_fixed": cs["speed_fixed"]}, timeout=5)
            else:
                continue
            print(f"  restored {v.get('name')}/{ch}: HTTP {r.status_code}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    skip = ()
    if "--exclude" in argv:
        i = argv.index("--exclude")
        skip = tuple(argv[i + 1].split(","))
        del argv[i:i + 2]
    cmd = argv[0] if argv else "list"
    s = session()
    if cmd == "list":
        cmd_list(s)
    elif cmd == "save":
        cmd_save(s)
    elif cmd == "ramp":
        if len(argv) < 2:
            sys.exit("usage: ramptest.py ramp <device> [duty]")
        cmd_ramp(s, argv[1], int(argv[2]) if len(argv) > 2 else 60, skip)
    elif cmd == "restore":
        cmd_restore(s)
    else:
        sys.exit(__doc__)
