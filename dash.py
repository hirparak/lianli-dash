#!/usr/bin/env python3
"""System dashboard for the Lian Li 8.8" Universal Screen.

Renders a 1920x480 (landscape) or 480x1920 (portrait) frame and pushes it to the
lianli-daemon over its IPC socket. The panel is natively 480x1920 portrait; we compose in
the chosen logical orientation and rotate to fit, so remounting the screen vertically is a
one-line config change (ORIENTATION).

Data sources, all local:
  - nvidia-smi        : per-GPU util / VRAM / temp / power
  - /proc             : CPU utilisation + RAM
  - CoolerControl API : coolant temp, flow, pump + fan RPM (localhost:11987)
  - llama.cpp router  : loaded model + live tokens/sec (localhost:11434)
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib3
from dataclasses import dataclass, field
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

import paths

urllib3.disable_warnings()

# ── config ────────────────────────────────────────────────────────────────────────────
ORIENTATION = os.environ.get("DASH_ORIENTATION", "portrait")  # "portrait" | "landscape"
# Rotation applied to reach the panel's native 480x1920. 270 = correct for the current
# (upside-down-at-90) mounting; flip to 90 if the panel is remounted the other way.
ROTATE = int(os.environ.get("DASH_ROTATE", "270"))
# Always absolute — the collector runs as root (the daemon's IPC socket is
# root-owned) and under root "~" is /root, which would silently write frames
# somewhere the daemon's config does not point at. paths.py resolves this.
FRAME = paths.FRAME
SOCK = os.environ.get("DASH_SOCK", "/tmp/lianli-daemon.sock")
INTERVAL = float(os.environ.get("DASH_INTERVAL", "2.0"))

# Panel title. Defaults to this machine's hostname, so the dashboard names
# itself instead of carrying the original author's box around.
TITLE = os.environ.get("DASH_TITLE") or socket.gethostname().split(".")[0].upper()

CC = os.environ.get("DASH_CC", "https://localhost:11987")
# CoolerControl's own documented defaults; override if you changed them.
CC_AUTH = (os.environ.get("DASH_CC_USER", "CCAdmin"),
           os.environ.get("DASH_CC_PASS", "coolAdmin"))
LLAMA = os.environ.get("DASH_LLAMA", "http://localhost:11434")
# ComfyUI runs as a child of the cqul-studio daemon; the studio is the driver of
# record (it owns batch state), so it is probed first and ComfyUI's own endpoints
# are only consulted while the studio reports comfy_up.
STUDIO = os.environ.get("DASH_STUDIO", "http://127.0.0.1:8731")
COMFY = os.environ.get("DASH_COMFY", "http://127.0.0.1:8188")

# ── palette (matches the rig's white-idle / red-load RGB language) ────────────────────
BG = (9, 11, 14)
CARD = (18, 21, 27)
EDGE = (38, 43, 54)
FG = (232, 236, 243)
DIM = (128, 138, 155)
ACCENT = (235, 238, 245)
HOT = (232, 62, 58)
WARM = (232, 150, 50)
COOL = (86, 170, 235)
GOOD = (74, 200, 130)

FONT_DIR = "/usr/share/fonts/TTF"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # omarchy 4 (2026-08-17) dropped the "...NerdFontMono-*" file names
    name = "JetBrainsMonoNerdFont-%s.ttf" % ("Bold" if bold else "Regular")
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


# ── metrics ───────────────────────────────────────────────────────────────────────────
@dataclass
class Metrics:
    gpus: list = field(default_factory=list)
    cpu_pct: float = 0.0
    cpu_temp: float | None = None
    ram_used: float = 0.0
    ram_total: float = 0.0
    coolant: float | None = None
    flow: float | None = None
    pump_rpm: int | None = None
    pump_duty: float | None = None
    pump2_rpm: int | None = None
    pump2_duty: float | None = None
    fan_rpm_avg: int | None = None
    fan_count: int = 0
    banks: list = field(default_factory=list)
    model: str | None = None
    tps: float | None = None          # live decode rate (None unless generating)
    last_tps: float | None = None     # last completed request's rate
    busy: bool = False
    accept: float | None = None       # speculative-decode acceptance %
    ctx_used: int | None = None
    prefill: float | None = None
    depth: list = field(default_factory=list)
    total_tokens: int = 0
    llm_models: list = field(default_factory=list)
    comfy: dict = field(default_factory=dict)


_prev_cpu = None
_prev_llama = {}
_prev_prefill = {}


def read_cpu() -> tuple[float, float, float]:
    """Returns (cpu_pct, ram_used_gb, ram_total_gb)."""
    global _prev_cpu
    with open("/proc/stat") as f:
        parts = [float(x) for x in f.readline().split()[1:]]
    idle, total = parts[3] + parts[4], sum(parts)
    pct = 0.0
    if _prev_cpu:
        di, dt = idle - _prev_cpu[0], total - _prev_cpu[1]
        if dt > 0:
            pct = max(0.0, min(100.0, (1 - di / dt) * 100))
    _prev_cpu = (idle, total)

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = float(v.strip().split()[0]) / 1024 / 1024  # GiB
    total_g = mem.get("MemTotal", 0)
    used_g = total_g - mem.get("MemAvailable", 0)
    return pct, used_g, total_g


def read_gpus() -> list[dict]:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        return []
    gpus = []
    for line in out.splitlines():
        f_ = [x.strip() for x in line.split(",")]
        if len(f_) < 7:
            continue
        gpus.append(dict(idx=int(f_[0]), util=float(f_[1]), vram=float(f_[2]) / 1024,
                         vram_total=float(f_[3]) / 1024, temp=float(f_[4]),
                         power=float(f_[5]), power_cap=float(f_[6])))
    return gpus


# ── fan banks ─────────────────────────────────────────────────────────────────
# Which physical fan groups the FANS panel shows, and which CoolerControl channels
# feed each one. This is per-rig wiring, so it lives in config.json rather than in
# source. Shape:
#
#   "fan_columns": ["ICUE", "NOCTUA"],
#   "fan_banks": [
#     {"label": "TOP RAD",
#      "left":  {"device": "a1b2c3d4", "channels": ["port2", "port3"]},
#      "right": {"device": "e5f6a7b8", "channels": null}}
#   ]
#
# `device` matches a CoolerControl device UID prefix, or (if nothing matches) a
# case-insensitive substring of the device NAME — "octo" is fine with one Octo,
# but use UID prefixes once you have two, since names collide.
# `channels` null/absent means "every fan channel this device exposes".
# `left`/`right` may be null to leave that column empty.
#
# With no `fan_banks` configured, banks are discovered live: one per device that
# exposes spinning fans. Run `python3 discover.py` to list what CoolerControl sees
# and to write a starter config you can then edit.
DEFAULT_FAN_COLUMNS = ["FANS", ""]


def _cfg() -> dict:
    try:
        c = json.load(open(paths.CONFIG_FILE))
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def fan_columns() -> list:
    c = _cfg().get("fan_columns")
    if isinstance(c, list) and c:
        return [str(x) for x in (list(c) + ["", ""])[:2]]
    return list(DEFAULT_FAN_COLUMNS)


def bank_slug(label: str, seen: dict) -> str:
    """Metric-file stem for a bank. Both the collector (which writes
    /run/lianli-dash/<slug>_left) and the template builder (whose command sensors
    cat it) derive this from the same config, so they always agree. Deduped
    because two banks may share a first word."""
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "bank"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}{n}"


_cc_session = None


def cc_get() -> dict:
    """Coolant/flow/pump/fan summary from CoolerControl.

    Bound by DEVICE NAME, not by bare channel name — several devices expose a 'fan1'/'temp1'
    and guessing picks the wrong one (an early version read a 51°C VRM sensor as 'coolant').
    On this loop: highflownext.temp1 = water temp, highflownext.fan1 = flow, d5next.fan1 =
    pump RPM, and the iCUE hub's portN channels are the case/rad fans.
    """
    global _cc_session
    try:
        if _cc_session is None:
            s = requests.Session()
            s.verify = False
            s.post(f"{CC}/login", auth=CC_AUTH, timeout=4)
            _cc_session = s
        names = {d["uid"]: d.get("name", "") for d in
                 _cc_session.get(f"{CC}/devices", timeout=4).json().get("devices", [])}
        data = _cc_session.post(f"{CC}/status", json={}, timeout=4).json()
    except Exception:
        _cc_session = None
        return {}
    coolant = flow = pump = pump_duty = pump2 = pump2_duty = None
    per_chan: dict = {}
    dev_names: dict = {}
    for dev in data.get("devices", []):
        name = names.get(dev.get("uid"), "").lower()
        h = (dev.get("status_history") or [{}])[-1]
        chans = {c.get("name"): c for c in h.get("channels", [])}
        temps = {t.get("name"): t.get("temp") for t in h.get("temps", [])}
        if "highflownext" in name:
            coolant = temps.get("temp1", coolant)
            f1 = chans.get("fan1", {}).get("rpm")
            # The flow meter reports in DECILITRES/hour (hwmon labels it "Flow [dL/h]"),
            # so the raw 1255 is 125.5 L/h — divide by 10 for the human unit.
            flow = f1 / 10.0 if f1 is not None else flow
        elif "d5next" in name:
            # fan1 = the D5 Next's own pump; fan2 = the SECOND D5 (HEATKILLER), whose
            # PWM+tach live on the D5 Next's fan header since the 2026-08-23 rewire.
            pump = chans.get("fan1", {}).get("rpm", pump)
            pump_duty = chans.get("fan1", {}).get("duty", pump_duty)
            pump2 = chans.get("fan2", {}).get("rpm", pump2)
            pump2_duty = chans.get("fan2", {}).get("duty", pump2_duty)
            if coolant is None:
                coolant = temps.get("temp1")
        # Any channel reporting an RPM is a fan, whatever the device. Keyed by
        # (uid8, channel) — the device UID already identifies the device, so no
        # per-vendor branch is needed and unknown hardware works by default.
        uid = dev.get("uid", "")
        dev_names[uid[:8]] = names.get(dev.get("uid"), "") or uid[:8]
        for n, c in chans.items():
            if c.get("rpm") is None:
                continue
            # Aquacomputer exposes the Octo's flow-meter header as fan9, and the
            # D5 Next's pump as fan1 — neither is a case fan.
            if "octo" in name and n == "fan9":
                continue
            if "d5next" in name or "highflownext" in name:
                continue
            per_chan[(uid[:8], n)] = c

    def match_dev(sel):
        """Resolve a config `device` selector to uid8 keys: UID prefix first, then
        a device-name substring so hand-written configs can say "octo"."""
        sel = str(sel or "").lower()
        if not sel:
            return list(dev_names)
        hit = [u for u in dev_names if u.lower().startswith(sel)]
        return hit or [u for u, n in dev_names.items() if sel in (n or "").lower()]

    def side(spec):
        """Average rpm + fan count for one side of a bank, or None if absent."""
        if not spec:
            return None
        uids = match_dev(spec.get("device"))
        want = spec.get("channels")
        got = [c for k, c in per_chan.items()
               if k[0] in uids and (not want or k[1] in want)]
        rpms = [c["rpm"] for c in got]
        spin = [r for r in rpms if r > 0]
        return dict(n=len(rpms), spinning=len(spin),
                    avg=int(sum(spin) / len(spin)) if spin else 0)

    specs = _cfg().get("fan_banks")
    if not isinstance(specs, list) or not specs:
        # Nothing configured: one bank per device that actually has fans, so a
        # fresh install shows a populated FANS panel instead of an empty box.
        # Ranked by SPINNING fans, because only four rows fit and an arbitrary
        # cut would just as easily have picked the motherboard's empty headers or
        # a watercooled GPU's stopped fans over the radiator hubs.
        by_dev: dict = {}
        for (u, _ch), c in per_chan.items():
            tot, spin = by_dev.get(u, (0, 0))
            by_dev[u] = (tot + 1, spin + (1 if c["rpm"] > 0 else 0))
        order = sorted(by_dev, key=lambda u: (-by_dev[u][1], -by_dev[u][0], u))
        # Two Octos (or two hubs) share a device name, so tag collisions with the
        # UID prefix — otherwise the panel shows two identical "OCTO" rows.
        # Compare the TRUNCATED label, not the full name: "iCUE Link System Hub"
        # and "iCUE Link System Hub B65" differ but both cut to "ICUE LINK SY".
        # 11 chars is what fits the label column before it collides with the value.
        short = {u: (dev_names.get(u) or u).upper()[:11] for u in order}
        counts = list(short.values())
        specs = [{"label": (f"{short[u][:6].strip()} {u[:4].upper()}"
                            if counts.count(short[u]) > 1 else short[u]),
                  "left": {"device": u}, "right": None}
                 for u in order]
    seen: dict = {}
    banks = []
    for b in specs[:4]:                      # the panel has room for four rows
        if not isinstance(b, dict):
            continue
        label = str(b.get("label") or "FANS")
        banks.append(dict(label=label, slug=bank_slug(label, seen),
                          left=side(b.get("left")), right=side(b.get("right"))))
    all_rpm = [c["rpm"] for c in per_chan.values()]
    spinning = [r for r in all_rpm if r > 0]
    return dict(coolant=coolant, flow=flow, pump=pump, pump_duty=pump_duty,
                pump2=pump2, pump2_duty=pump2_duty, banks=banks,
                fan_avg=int(sum(spinning) / len(spinning)) if spinning else 0,
                fan_n=len(all_rpm))


def llama_get() -> dict:
    """All loaded models (the router runs --models-max concurrent children) with per-model
    live rates. Rates are deltas of each child's cumulative counters — see README on why the
    *_tokens_seconds gauges can't be used mid-request.

    Metrics are scraped from each child server DIRECTLY, never via the router's
    `/metrics?model=<id>`. A proxied scrape counts as activity against the model,
    so polling every 2s held every model resident forever — the router could never
    evict anything. `/v1/models` already hands us the child's port in status.args,
    and the child's own /metrics is byte-identical, so the proxy buys nothing.
    (`/v1/models` itself is fine to keep polling: it does not proxy to a child.)
    There is no aggregate /metrics — it errors with "model name is missing" — so
    per-model scraping is unavoidable; it just does not have to go through the router."""
    try:
        models = requests.get(f"{LLAMA}/v1/models", timeout=3).json().get("data", [])
    except Exception:
        return {}
    loaded = []
    for m in models:
        st = m.get("status")
        val = st.get("value") if isinstance(st, dict) else st
        if val and val != "unloaded":
            args = (st.get("args") or []) if isinstance(st, dict) else []
            port = None
            if "--port" in args:
                i = args.index("--port")
                if i + 1 < len(args):
                    port = args[i + 1]
            # "0" is the preset's auto-assign placeholder: the router rewrites it to
            # the real port once the child is up, so a 0 here means nothing to scrape.
            if port and port != "0":
                loaded.append((m["id"], port))
    out = {"models": []}
    global _prev_llama, _prev_prefill
    for name, port in loaded[:2]:
        try:
            # A model can be evicted between /v1/models and this scrape; the child is
            # then gone and this raises, which the caller treats as "no data" — the
            # same outcome as before, so a lost race is harmless.
            txt = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=3).text
        except Exception:
            continue
        m: dict[str, float] = {}
        for line in txt.splitlines():
            if line.startswith("#"):
                continue
            if "spec_decode_num_accepted_tokens_per_pos_total{position=" in line:
                try:
                    pos = int(line.split('position="')[1].split('"')[0])
                    m[f"spec_pos_{pos}"] = float(line.split()[-1])
                except (IndexError, ValueError):
                    pass
                continue
            if "{" in line:
                continue
            parts = line.split()
            if len(parts) == 2 and parts[0].startswith("llamacpp:"):
                try:
                    m[parts[0][9:]] = float(parts[1])
                except ValueError:
                    pass
        # live decode rate: delta of cumulative counters, tracked PER MODEL
        tok, sec = m.get("tokens_predicted_total"), m.get("tokens_predicted_seconds_total")
        live = None
        prev = _prev_llama.get(name)
        if prev and tok is not None and sec is not None:
            d_tok, d_sec = tok - prev[0], sec - prev[1]
            if d_tok > 0 and d_sec > 0:
                live = d_tok / d_sec
        if tok is not None and sec is not None:
            _prev_llama[name] = (tok, sec)
        # prefill rate, same treatment
        ptok, psec = m.get("prompt_tokens_total"), m.get("prompt_seconds_total")
        prefill = None
        pprev = _prev_prefill.get(name)
        if pprev and ptok is not None and psec is not None:
            d_t, d_s = ptok - pprev[0], psec - pprev[1]
            if d_t > 0 and d_s > 0:
                prefill = d_t / d_s
        if ptok is not None and psec is not None:
            _prev_prefill[name] = (ptok, psec)
        drafts = m.get("spec_decode_num_drafts_total") or 0
        depth = [(m.get(f"spec_pos_{i}", 0) / drafts * 100) if drafts else 0.0 for i in range(3)]
        drafted = m.get("spec_decode_num_draft_tokens_total")
        accepted = m.get("spec_decode_num_accepted_tokens_total")
        out["models"].append(dict(
            name=name,
            busy=bool(m.get("requests_processing", 0)),
            tps=live,
            last_tps=m.get("predicted_tokens_seconds"),
            prefill=prefill,
            accept=(accepted / drafted * 100) if drafted else None,
            ctx=int(m.get("n_tokens_max") or 0),
            total=int(m.get("tokens_predicted_total") or 0),
            depth=depth,
        ))
    # aggregates (what the single-model fields used to carry)
    ms = out["models"]
    first = ms[0] if ms else {}
    busy_ms = [x for x in ms if x["busy"] or x["tps"]]
    focus = busy_ms[0] if busy_ms else first
    out.update(
        model=focus.get("name"),
        busy=any(x["busy"] for x in ms),
        tps=sum(x["tps"] or 0 for x in ms) or None,
        last_tps=focus.get("last_tps"),
        prefill=sum(x["prefill"] or 0 for x in ms) or (focus.get("prefill")),
        accept=focus.get("accept"),
        ctx_used=focus.get("ctx", 0),
        total_tokens=sum(x["total"] for x in ms),
        depth=focus.get("depth", [0, 0, 0]),
    )
    return out


# ── ComfyUI (via the cqul-studio daemon) ─────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# tqdm frame: " 80%|████████  | 16/20 [22:49<05:42, 85.67s/it]" (postfix text may follow)
_TQDM_RE = re.compile(
    r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)\s*\[([\d:]+)<([\d:?]+),\s*([\d.?]+)\s*(s/it|it/s)")


def _hms_min(s: str) -> float | None:
    """tqdm time field -> minutes: '07:36' -> 7.6, '1:02:33' -> 62.55, '?' -> None."""
    if "?" in s:
        return None
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, mi, se = parts
    return h * 60 + mi + se / 60


def _comfy_progress() -> dict:
    """Newest tqdm frame from ComfyUI's captured stdout.

    ComfyUI 0.33 has NO structured HTTP progress route (progress messages are
    websocket-unicast to the submitting client_id, which nothing here holds), so
    the sampler step counter is scraped from /internal/logs/raw — a 300-entry
    deque of stdout writes where each of tqdm's \r updates lands as its own
    entry. Undocumented endpoint; every failure path returns {} (best-effort).
    """
    try:
        r = requests.get(f"{COMFY}/internal/logs/raw", timeout=2)
        entries = r.json().get("entries") or []
    except Exception:
        return {}
    for e in reversed(entries):
        m = None
        for frame in _ANSI_RE.sub("", e.get("m") or "").split("\r"):
            fm = _TQDM_RE.search(frame)
            if fm:
                m = fm  # last matching frame within the entry wins
        if not m:
            continue
        rate = None if m.group(6) == "?" else float(m.group(6))
        if rate is not None and m.group(7) == "it/s":
            rate = 1 / rate if rate else None
        try:
            age = time.time() - datetime.fromisoformat(e["t"]).timestamp()
        except Exception:
            age = 0.0
        return {
            "pct": float(m.group(1)),
            "step": int(m.group(2)),
            "steps": int(m.group(3)),
            "spi": rate,                       # seconds per iteration
            "eta_min": _hms_min(m.group(5)),
            "age": age,
        }
    return {}


def comfy_get() -> dict:
    """Studio batch state + ComfyUI queue + current-job tqdm progress.

    Degrades a level per failure instead of raising: studio down -> up False and
    everything zeroed; comfy down -> queue/progress zeroed but batch kept.
    """
    out = {"up": False, "comfy_up": False, "running": [], "job": None,
           "batch_done": 0, "batch_total": 0, "batch_failed": 0, "queue": 0,
           "pct": 0.0, "step": 0, "steps": 0, "spi": None, "eta_min": None}
    try:
        st = requests.get(f"{STUDIO}/status", timeout=2).json()
    except Exception:
        return out
    out["up"] = True
    out["comfy_up"] = bool(st.get("comfy_up"))
    out["running"] = st.get("running") or []
    batch = st.get("batch") or {}
    out["batch_done"] = int(batch.get("done") or 0)
    out["batch_total"] = int(batch.get("total") or 0)
    out["batch_failed"] = int(batch.get("failed") or 0)
    out["job"] = (out["running"][0] if out["running"] else None) or batch.get("current")
    if not out["comfy_up"]:
        return out
    try:
        q = requests.get(f"{COMFY}/prompt", timeout=2).json()
        out["queue"] = int(q.get("exec_info", {}).get("queue_remaining") or 0)
    except Exception:
        pass
    p = _comfy_progress()
    if p and out["running"]:
        # Staleness gates: a finished job leaves its 100% line in the log buffer
        # (zero it once it's >30s old — the next job's model-load window honestly
        # shows 0%), and a wedged/silent sampler shouldn't show frozen progress.
        # Mid-step ages up to ~90s are NORMAL at wan's ~85 s/it, hence the slack.
        done_line = p["step"] >= p["steps"] and p["age"] > 30
        wedged = p["age"] > max(240, 4 * (p["spi"] or 0))
        if not done_line and not wedged:
            out.update(pct=p["pct"], step=p["step"], steps=p["steps"],
                       spi=p["spi"], eta_min=p["eta_min"])
    return out


def collect() -> Metrics:
    m = Metrics()
    m.cpu_pct, m.ram_used, m.ram_total = read_cpu()
    m.gpus = read_gpus()
    cc = cc_get()
    m.coolant, m.flow = cc.get("coolant"), cc.get("flow")
    m.pump_rpm, m.fan_rpm_avg, m.fan_count = cc.get("pump"), cc.get("fan_avg"), cc.get("fan_n", 0)
    m.pump_duty = cc.get("pump_duty")
    m.banks = cc.get("banks", [])
    m.pump2_rpm, m.pump2_duty = cc.get("pump2"), cc.get("pump2_duty")
    ll = llama_get()
    m.model, m.tps, m.ctx_used = ll.get("model"), ll.get("tps"), ll.get("ctx")
    m.last_tps, m.busy, m.accept = ll.get("last_tps"), ll.get("busy", False), ll.get("accept")
    m.prefill, m.depth = ll.get("prefill"), ll.get("depth", [])
    m.total_tokens, m.ctx_used = ll.get("total_tokens", 0), ll.get("ctx_used", 0)
    m.llm_models = ll.get("models", [])
    m.comfy = comfy_get()
    for h in ("k10temp", "zenpower", "coretemp"):
        p = f"/sys/class/hwmon"
        try:
            for d in os.listdir(p):
                if open(f"{p}/{d}/name").read().strip() == h:
                    m.cpu_temp = int(open(f"{p}/{d}/temp1_input").read()) / 1000
                    break
        except Exception:
            pass
        if m.cpu_temp:
            break
    return m


# ── drawing helpers ───────────────────────────────────────────────────────────────────
def temp_color(t: float | None, warm=55, hot=75) -> tuple:
    if t is None:
        return DIM
    if t >= hot:
        return HOT
    if t >= warm:
        return WARM
    return COOL


def load_color(p: float) -> tuple:
    if p >= 70:
        return HOT
    if p >= 35:
        return WARM
    return ACCENT


def card(d: ImageDraw.ImageDraw, box, title: str, fs=15):
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=10, fill=CARD, outline=EDGE, width=1)
    if title:
        d.text((x0 + 14, y0 + 9), title, font=font(fs, True), fill=DIM)


def bar(d, x, y, w, h, frac, color, track=(30, 34, 43)):
    frac = max(0.0, min(1.0, frac))
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=track)
    if frac > 0.01:
        d.rounded_rectangle([x, y, x + max(h, w * frac), y + h], radius=h // 2, fill=color)


def gpu_card(d, box, g: dict):
    x0, y0, x1, y1 = box
    card(d, box, f"GPU {g['idx']}  ·  RTX PRO 6000")
    w = x1 - x0
    d.text((x1 - 14 - d.textlength(f"{g['temp']:.0f}°C", font=font(30, True)), y0 + 30),
           f"{g['temp']:.0f}°C", font=font(30, True), fill=temp_color(g["temp"], 60, 80))
    d.text((x0 + 14, y0 + 34), f"{g['util']:.0f}%", font=font(34, True), fill=load_color(g["util"]))
    bar(d, x0 + 14, y0 + 78, w - 28, 9, g["util"] / 100, load_color(g["util"]))
    vfrac = g["vram"] / g["vram_total"] if g["vram_total"] else 0
    d.text((x0 + 14, y0 + 96), f"VRAM {g['vram']:.1f}/{g['vram_total']:.0f}G",
           font=font(14), fill=DIM)
    bar(d, x0 + 14, y0 + 118, w - 28, 7, vfrac, COOL)
    d.text((x0 + 14, y0 + 132), f"{g['power']:.0f}W / {g['power_cap']:.0f}W",
           font=font(14), fill=DIM)


def cpu_card(d, box, m: Metrics):
    x0, y0, x1, y1 = box
    card(d, box, "CPU  ·  THREADRIPPER")
    w = x1 - x0
    d.text((x0 + 14, y0 + 34), f"{m.cpu_pct:.0f}%", font=font(34, True), fill=load_color(m.cpu_pct))
    if m.cpu_temp:
        s = f"{m.cpu_temp:.0f}°C"
        d.text((x1 - 14 - d.textlength(s, font=font(30, True)), y0 + 30), s,
               font=font(30, True), fill=temp_color(m.cpu_temp, 70, 85))
    bar(d, x0 + 14, y0 + 78, w - 28, 9, m.cpu_pct / 100, load_color(m.cpu_pct))
    rfrac = m.ram_used / m.ram_total if m.ram_total else 0
    d.text((x0 + 14, y0 + 96), f"RAM {m.ram_used:.0f}/{m.ram_total:.0f}G", font=font(14), fill=DIM)
    bar(d, x0 + 14, y0 + 118, w - 28, 7, rfrac, GOOD)


def cool_card(d, box, m: Metrics):
    x0, y0, x1, y1 = box
    card(d, box, "LOOP")
    w = x1 - x0
    items = [
        ("COOLANT", f"{m.coolant:.1f}°" if m.coolant is not None else "—",
         temp_color(m.coolant, 38, 45)),
        ("FLOW", f"{m.flow:.0f}" if m.flow is not None else "—", COOL),
        ("PUMP", f"{m.pump_duty:.0f}%" if m.pump_duty is not None else "—", FG),
    ]
    units = ["", "L/h", f"{m.pump_rpm or 0} rpm"]
    cw = (w - 28) / len(items)
    for i, ((label, val, col), unit) in enumerate(zip(items, units)):
        cx = x0 + 14 + i * cw
        d.text((cx, y0 + 34), label, font=font(13), fill=DIM)
        d.text((cx, y0 + 52), val, font=font(30, True), fill=col)
        if unit:
            d.text((cx, y0 + 88), unit, font=font(12), fill=DIM)


def fans_card(d, box, m: Metrics):
    """Per-radiator fan banks. Each rad is push-pull, so both sides are shown: the iCUE side
    and the Noctua side, with the airflow role that side plays on that rad."""
    x0, y0, x1, y1 = box
    card(d, box, "FANS")
    w = x1 - x0
    rows = m.banks or []
    if not rows:
        d.text((x0 + 14, y0 + 40), "no fan data", font=font(15), fill=DIM)
        return
    rh = (y1 - y0 - 40) / max(1, len(rows))
    for i, b in enumerate(rows):
        ry = y0 + 34 + i * rh
        d.text((x0 + 14, ry + 6), b["label"], font=font(15, True), fill=FG)
        # both sides, side by side: value + (spinning/total)
        sides = [("ICUE", b.get("icue")), ("NOCTUA", b.get("noctua"))]
        sx = x0 + 150
        for role, sd in sides:
            if not sd:
                continue
            rpm = sd["avg"]
            col = ACCENT if rpm else DIM
            txt = f"{rpm}" if rpm else "off"
            d.text((sx, ry + 2), role, font=font(10), fill=DIM)
            d.text((sx, ry + 14), txt, font=font(19, True), fill=col)
            d.text((sx + d.textlength(txt, font=font(19, True)) + 4, ry + 20),
                   f"×{sd['n']}", font=font(11), fill=DIM)
            sx += 130


def llm_card(d, box, m: Metrics):
    x0, y0, x1, y1 = box
    card(d, box, "LLAMA.CPP")
    w = x1 - x0
    if not m.model:
        d.text((x0 + 14, y0 + 44), "no model loaded", font=font(17), fill=DIM)
        return
    name = m.model if len(m.model) <= 24 else m.model[:23] + "…"
    d.text((x0 + 14, y0 + 32), name, font=font(19, True), fill=ACCENT)
    # state pill: generating (live) vs loaded-idle
    live = m.tps is not None and m.tps > 0
    label, col = ("GEN", GOOD) if (m.busy or live) else ("IDLE", DIM)
    tw = d.textlength(label, font=font(12, True))
    d.rounded_rectangle([x1 - 16 - tw - 12, y0 + 32, x1 - 14, y0 + 52], radius=6,
                        fill=(28, 60, 42) if label == "GEN" else (30, 34, 43))
    d.text((x1 - 16 - tw - 6, y0 + 35), label, font=font(12, True), fill=col)
    # throughput: live rate while generating, otherwise the last request's rate (greyed)
    if live:
        d.text((x0 + 14, y0 + 56), f"{m.tps:.0f}", font=font(34, True), fill=GOOD)
        off = d.textlength(f"{m.tps:.0f}", font=font(34, True))
        d.text((x0 + 18 + off, y0 + 72), "tok/s", font=font(14), fill=DIM)
    elif m.last_tps:
        d.text((x0 + 14, y0 + 58), f"{m.last_tps:.0f}", font=font(28, True), fill=DIM)
        off = d.textlength(f"{m.last_tps:.0f}", font=font(28, True))
        d.text((x0 + 18 + off, y0 + 70), "tok/s last", font=font(13), fill=DIM)
    if m.accept is not None:
        d.text((x0 + 14, y0 + 96), f"spec accept {m.accept:.0f}%", font=font(13), fill=DIM)


def render(m: Metrics) -> Image.Image:
    portrait = ORIENTATION == "portrait"
    W, H = (480, 1920) if portrait else (1920, 480)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    pad, gap = 12, 10
    # header
    d.text((pad + 4, pad + 2), TITLE, font=font(24, True), fill=ACCENT)
    clock = time.strftime("%H:%M")
    d.text((W - pad - 4 - d.textlength(clock, font=font(22, True)), pad + 4), clock,
           font=font(22, True), fill=DIM)
    top = pad + 38

    if portrait:
        # One column down the panel. 1920px is a lot of room, so the cards are generous:
        # two GPUs, CPU, loop, per-rad fan banks, then llama.cpp.
        x0, x1 = pad, W - pad
        avail = H - top - pad
        heights = [230, 230, 210, 130, 250, 190]     # gpu, gpu, cpu, loop, fans, llm
        spare = avail - sum(heights) - gap * (len(heights) - 1)
        if spare > 0:                                 # distribute slack so it fills the panel
            heights = [h + spare // len(heights) for h in heights]
        y = top
        for i, g in enumerate(m.gpus[:2]):
            gpu_card(d, (x0, y, x1, y + heights[i]), g)
            y += heights[i] + gap
        cpu_card(d, (x0, y, x1, y + heights[2]), m); y += heights[2] + gap
        cool_card(d, (x0, y, x1, y + heights[3]), m); y += heights[3] + gap
        fans_card(d, (x0, y, x1, y + heights[4]), m); y += heights[4] + gap
        llm_card(d, (x0, y, x1, y + heights[5]), m)
    else:
        # three columns: GPUs | CPU+loop | llm
        colw = (W - pad * 2 - gap * 2) / 3
        c0 = pad
        c1 = pad + colw + gap
        c2 = pad + 2 * (colw + gap)
        h = H - top - pad
        if m.gpus:
            gpu_card(d, (c0, top, c0 + colw, top + (h - gap) / 2), m.gpus[0])
        if len(m.gpus) > 1:
            gpu_card(d, (c0, top + (h + gap) / 2, c0 + colw, top + h), m.gpus[1])
        cpu_card(d, (c1, top, c1 + colw, top + (h - gap) / 2), m)
        cool_card(d, (c1, top + (h + gap) / 2, c1 + colw, top + h), m)
        llm_card(d, (c2, top, c2 + colw, top + (h - gap) / 2), m)
        fans_card(d, (c2, top + (h + gap) / 2, c2 + colw, top + h), m)
    return img


_flip = 0
_base_cfg = None


def _ipc(req: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15)
    s.connect(SOCK)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode().strip() or "{}")


_lcd_serial = None


def lcd_serial() -> str:
    """The panel's serial, resolved once and cached.

    Order: DASH_LCD_SERIAL, then `lcd_serial` in config.json, then ask the
    daemon — the first device reporting has_lcd is the panel. Auto-detection is
    what makes this work on someone else's machine; the serial is per-unit, so
    it can never be a useful default baked into source.
    """
    global _lcd_serial
    if _lcd_serial is not None:
        return _lcd_serial
    v = os.environ.get("DASH_LCD_SERIAL", "")
    if not v:
        try:
            cfg = json.load(open(paths.CONFIG_FILE))
            v = cfg.get("lcd_serial") or ""
        except (OSError, ValueError):
            v = ""
    if not v:
        try:
            devs = _ipc({"method": "ListDevices"}).get("data") or []
            v = next((d["device_id"] for d in devs if d.get("has_lcd")), "")
        except Exception:
            v = ""
    if not v:
        raise RuntimeError(
            "no LCD panel found. Is lianli-daemon running? Otherwise set "
            "DASH_LCD_SERIAL or `lcd_serial` in " + paths.CONFIG_FILE)
    _lcd_serial = v
    return v


def _base_config() -> dict:
    """The daemon's config with our LCD entry stripped, fetched once and reused."""
    global _base_cfg
    if _base_cfg is None:
        _base_cfg = _ipc({"method": "GetConfig"}).get("data") or {}
    cfg = dict(_base_cfg)
    cfg["lcds"] = []
    return cfg


def push(img: Image.Image):
    """Write the frame and tell the daemon to display it.

    The daemon only re-reads the image when the media CONFIG changes — re-sending the same
    path is a silent no-op (no 'Prepared image' log, panel keeps the old frame). So we
    alternate between two filenames to make every update a real config change.
    """
    global _flip
    os.makedirs(os.path.dirname(FRAME), exist_ok=True)
    out = img if img.size == (480, 1920) else img.rotate(ROTATE, expand=True)
    # A UNIQUE filename per push. The daemon reloads only when the media config CHANGES, so
    # reusing a path is a silent no-op. An alternating flag isn't enough either: a fresh
    # process starts the flag at 0 and reuses the same name as the last run, which is exactly
    # why one-shot pushes appeared to do nothing.
    _flip = (_flip + 1) % 1000
    path = FRAME.replace(".png", f"_{int(time.time())%10000}_{_flip}.png")
    tmp = path + ".tmp"
    out.save(tmp, "PNG")
    os.replace(tmp, path)
    # Push via SetConfig, NOT SetLcdMedia: SetLcdMedia appends a second entry whenever its
    # device_id() doesn't match an existing one, and duplicate LCD entries fight over the panel
    # (the stale one wins, so the display appears frozen). Replacing the whole config with
    # exactly one lcds entry is deterministic. The rest of the config is preserved.
    # drop the previous frame file so the directory doesn't grow
    prev = globals().get("_prev_path")
    if prev and prev != path and os.path.exists(prev):
        try:
            os.remove(prev)
        except OSError:
            pass
    globals()["_prev_path"] = path
    base = _base_config()
    base["lcds"] = [{"serial": lcd_serial(), "type": "image", "path": path,
                     "orientation": 0.0}]
    _ipc({"method": "SetConfig", "params": {"config": base}})


def main():
    read_cpu()  # prime the CPU delta
    while True:
        try:
            push(render(collect()))
        except Exception as e:  # keep the panel alive through transient errors
            print("dash:", e, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
