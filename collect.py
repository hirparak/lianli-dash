#!/usr/bin/env python3
"""Metric collector for the template-based dashboard.

The daemon's template widgets pull values through `SensorSourceConfig::Command`, which runs a
shell command per sample. Spawning a Python process per widget per second would be silly, so
this single loop writes one tiny file per metric and the widgets just `cat` them — a ~1ms fork
instead of an interpreter start.

Values that have a NATIVE daemon sensor (cpu_usage, mem_usage, nvidia_gpu temp/usage, hwmon
coolant/pump) don't need a file; this covers the rest: VRAM, GPU power, flow in L/h, the
per-bank fan averages (the iCUE hub is a CoolerControl plugin, not hwmon) and llama.cpp.
"""
import json
import os
import time

from PIL import Image, ImageDraw, ImageFont

import dash  # reuse the collectors already written for the PIL renderer

def _it8689_path():
    for h in os.listdir("/sys/class/hwmon"):
        try:
            if open(f"/sys/class/hwmon/{h}/name").read().strip() == "it8689":
                return f"/sys/class/hwmon/{h}"
        except OSError:
            pass
    return None

_IT8689 = _it8689_path()


def _octo_a_path():
    """The top-bank Octo carries both thermistors (temp1=chassis, temp2=room);
    the front Octo has no temp2 file, which is what tells them apart."""
    for h in os.listdir("/sys/class/hwmon"):
        base = f"/sys/class/hwmon/{h}"
        try:
            if open(f"{base}/name").read().strip() != "octo":
                continue
            int(open(f"{base}/temp2_input").read())
            return base
        except (OSError, ValueError):
            pass
    return None

_OCTO_A = _octo_a_path()

# CPU package watts from RAPL energy counters (root-readable only, which the
# collector is). Watts = delta(energy_uj)/delta(t); the counter wraps at
# max_energy_range_uj, handled below.
_RAPL = "/sys/class/powercap/intel-rapl:0/energy_uj"
_rapl_last = None            # (energy_uj, monotonic)


def cpu_watts():
    global _rapl_last
    try:
        e = int(open(_RAPL).read())
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    prev = _rapl_last
    _rapl_last = (e, now)
    if prev is None or now <= prev[1]:
        return None
    de = e - prev[0]
    if de < 0:                          # counter wrapped
        try:
            de += int(open(
                "/sys/class/powercap/intel-rapl:0/max_energy_range_uj").read())
        except (OSError, ValueError):
            return None
    return de / (now - prev[1]) / 1e6
import ringbar
from build_template_native import build as build_tpl, bg_video_key, bg_path, _user_cfg
import paths

_last_rate = 0.0
_last_gen_at = 0.0
_slot_gen_at = {}
_slot_rate = {}
_installed_key = None   # (mode, labels...) of the currently installed template
_media_set = None       # (orient, brightness) SetLcdMedia was last sent with
_mode = "llama"         # which bottom-section layout is installed
_comfy_active_at = 0.0
_comfy_spi = 0.0        # last real s/it (holdover, same idiom as _slot_rate)
_hot_at = 0.0           # last tick inference was running (drives the fire background)
# Seconds the fire look outlives the last busy tick. Default matches the iCUE
# fans' status-mode EMA (~10-15s visible rain after load ends) so the whole
# case calms together — the original 45s left the dash burning long after the
# fans went white. Tunable per config.json `hot_hold_s`; each hot<->calm flip
# costs the panel daemon a ~2s background re-decode, so don't set it near zero.
HOT_HOLD_DEFAULT = 15.0


OUT = "/run/lianli-dash"

# Display config, written by the tray app (tray.py, runs as kieran):
#   type  — combined (auto llama/comfy switching, the classic dashboard),
#           system (no inference), comfy / llama (pure inference dashboards),
#           video (background loop fullscreen), off (black panel)
#   theme — presentation: palette/background/widget style (only "default" so far)
CONFIG_FILE = paths.CONFIG_FILE
TYPES = ("combined", "system", "comfy", "llama", "video", "off")
THEMES = ("default", "cardash", "cyberpunk", "steampunk", "gits")
# orientation -> the daemon's rotation (degrees); flipped covers either mount
ORIENTS = {"portrait": 0.0, "landscape": 90.0, "landscape-flipped": 270.0,
           "portrait-flipped": 180.0}


def read_config() -> tuple[str, str, str, int]:
    try:
        import json as _json
        c = _json.load(open(CONFIG_FILE))
        t = c.get("type") if c.get("type") in TYPES else "combined"
        th = c.get("theme") if c.get("theme") in THEMES else "default"
        o = c.get("orient") if c.get("orient") in ORIENTS else "portrait"
        br = c.get("brightness")
        br = max(0, min(100, int(br))) if isinstance(br, (int, float)) else 100
        return t, th, o, br
    except Exception:
        return "combined", "default", "portrait", 100


def config_stamp() -> float:
    """mtime of the config file — the fast poll watches this for instant switches."""
    try:
        return os.path.getmtime(CONFIG_FILE)
    except OSError:
        return 0


def write(name: str, value) -> None:
    """Atomically write one metric file (widgets cat these)."""
    path = os.path.join(OUT, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{value}\n")
    os.replace(tmp, path)


# ── LED ring status accent ─────────────────────────────────────────────────
# The screen's 60-LED ring (0416:8050) only does Off/Static/Direct — no
# hardware effects — so a static colour + brightness carries the same status
# language as the case: dim white idle, orange stepping brighter with GPU
# load, red on the coolant alarm. Load signal = GPU 0 (the non-display card),
# matching coolerlink's status rain.
_ring_client = None     # persistent OpenRGB connection to the daemon
_ring_state = None      # ("alarm",) or (fill_steps, hot) — last frame sent
_ring_sent_at = 0.0


def update_ring(m, hot=False) -> None:
    """The ring is a vertical VRAM gauge: both long edges fill bottom->up with
    combined VRAM (used/total over both GPUs), white when calm, red while the
    rig is hot (same flag as the fire background), full-ring red on an alarm.

    Per-LED frames go over the daemon's OpenRGB server (the SetRgbEffect IPC
    flattens to one colour). State is quantised to the 24-LED edge resolution
    so a frame is only sent when something visibly changes, plus the usual
    5-min re-assert; write failures drop the connection and retry next tick.
    """
    global _ring_state, _ring_sent_at, _ring_client
    used = sum(g.get("vram") or 0 for g in m.gpus)
    total = sum(g.get("vram_total") or 0 for g in m.gpus)
    fill = (used / total) if total else 0.0
    alarm = (m.coolant is not None and m.coolant >= 50) or _ww_worst >= 2
    steps = round(max(0.0, min(1.0, fill)) * ringbar.EDGE)
    state = ("alarm",) if alarm else (steps, bool(hot))
    now = time.time()
    if state == _ring_state and now - _ring_sent_at < 300:
        return
    try:
        if _ring_client is None:
            _ring_client = ringbar.RingClient()
        if alarm:
            frame = ringbar.full_frame((255, 0, 0))
        else:
            colour = (255, 30, 10) if hot else (180, 180, 180)
            frame = ringbar.bar_frame(fill, colour)
        _ring_client.update(frame)
        _ring_state, _ring_sent_at = state, now
    except Exception as e:
        try:
            _ring_client.close()
        except Exception:
            pass
        _ring_client = None   # server may be restarting — reconnect next tick
        print("ring:", e, flush=True)


# ── WireView Pro II (wirewatch) ────────────────────────────────────────────
# wirewatch publishes per-card 12V-2x6 telemetry as JSON; nvidia index ->
# wirewatch label per /etc/wirewatch/config.toml (top card = index 1).
WIREWATCH_DIR = "/run/wirewatch"
WIREWATCH_LABELS = {0: "gpu-bottom", 1: "gpu-top"}
_WW_LEVEL = {"offline": -1, "ok": 0, "warn": 1, "crit": 2, "instant": 3}
_ww_worst = -1


def write_wirewatch() -> None:
    """gpu{i}_cable_w, gpu{i}_pins_a (worst pin), gpu{i}_pins_pct (spread,
    0 below wirewatch's 15 A floor), gpu{i}_pins_lvl (-1 offline .. 3)."""
    global _ww_worst
    worst = -1
    for i, label in WIREWATCH_LABELS.items():
        try:
            with open(os.path.join(WIREWATCH_DIR, f"{label}.json")) as f:
                d = json.load(f)
            if not d.get("online"):
                raise ValueError("offline")
            pins = [p["current_a"] for p in d["pins"]]
            spread = ((max(pins) - min(pins)) / (sum(pins) / 6) * 100
                      if d["current_a"] >= 15.0 else 0)
            lvl = _WW_LEVEL.get(d["level"], 0)
            write(f"gpu{i}_cable_w", f"{d['power_w']:.0f}")
            write(f"gpu{i}_pins_a", f"{max(pins):.1f}")
            write(f"gpu{i}_pins_pct", f"{spread:.0f}")
        except (OSError, ValueError, KeyError):
            lvl = -1
            write(f"gpu{i}_cable_w", "0")
            write(f"gpu{i}_pins_a", "0")
            write(f"gpu{i}_pins_pct", "0")
        write(f"gpu{i}_pins_lvl", str(lvl))
        worst = max(worst, lvl)
    _ww_worst = worst


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    os.chmod(OUT, 0o755)
    dash.read_cpu()  # prime the CPU delta
    while True:
        try:
            m = dash.collect()
            for g in m.gpus[:2]:
                i = g["idx"]
                write(f"gpu{i}_util", f"{g['util']:.0f}")
                write(f"gpu{i}_temp", f"{g['temp']:.0f}")
                write(f"gpu{i}_vram", f"{g['vram']:.1f}")
                write(f"gpu{i}_vram_pct", f"{g['vram'] / g['vram_total'] * 100:.0f}")
                write(f"gpu{i}_power", f"{g['power']:.0f}")
            # combined-GPU rollups for the llama page: average utilisation (both
            # cards share a split model, so avg tracks the workload), summed
            # watts, hottest temp
            gs = m.gpus[:2]
            if gs:
                write("gpus_util", f"{sum(g['util'] for g in gs) / len(gs):.0f}")
                write("gpus_power", f"{sum(g['power'] for g in gs):.0f}")
                write("gpus_temp", f"{max(g['temp'] for g in gs):.0f}")
            write_wirewatch()
            write("cpu_util", f"{m.cpu_pct:.0f}")
            write("cpu_temp", f"{m.cpu_temp:.0f}" if m.cpu_temp else "0")
            w = cpu_watts()
            write("cpu_watts", f"{w:.0f}" if w is not None else "0")
            write("ram_used", f"{m.ram_used:.0f}")
            write("ram_pct", f"{m.ram_used / m.ram_total * 100:.0f}" if m.ram_total else "0")
            write("coolant", f"{m.coolant:.1f}" if m.coolant is not None else "0")
            # Chassis + room thermistors moved to the top Octo in the 2026-08-23
            # rewire (temp1=chassis air, temp2=room, re-routed clear of exhaust).
            # it8689 temp1 (a mobo/VRM-adjacent sensor) stays as fallback only.
            wrote_case = False
            if _OCTO_A:
                try:
                    write("case_temp", f"{int(open(_OCTO_A + '/temp1_input').read()) / 1000:.1f}")
                    wrote_case = True
                    write("room_temp", f"{int(open(_OCTO_A + '/temp2_input').read()) / 1000:.1f}")
                except (OSError, ValueError):
                    pass
            if not wrote_case and _IT8689:
                try:
                    write("case_temp", f"{int(open(_IT8689 + '/temp1_input').read()) / 1000:.1f}")
                except (OSError, ValueError):
                    pass
            write("flow", f"{m.flow:.0f}" if m.flow is not None else "0")
            write("pump_duty", f"{m.pump_duty:.0f}" if m.pump_duty is not None else "0")
            write("pump_rpm", f"{m.pump_rpm or 0}")
            write("pump2_duty", f"{m.pump2_duty:.0f}" if m.pump2_duty is not None else "0")
            write("pump2_rpm", f"{m.pump2_rpm or 0}")
            # Bank slugs come from the same config the template builder reads, so
            # the files written here always match the command sensors that cat them.
            for b in m.banks:
                for side in ("left", "right"):
                    sd = b.get(side)
                    write(f"{b['slug']}_{side}", sd["avg"] if sd else 0)
            write("llm_model", m.model or "idle")
            # per-slot files: the router runs up to two concurrent children (--models-max 2)
            global _slot_gen_at, _slot_rate
            now = time.time()
            for i in range(2):
                slot = m.llm_models[i] if i < len(m.llm_models) else None
                if slot:
                    write(f"llm{i}_ctx", f"{(slot.get('ctx') or 0) / 1000:.0f}")
                    gen = slot["busy"] or bool(slot["tps"])
                    if gen:
                        _slot_gen_at[i] = now
                    rate = slot["tps"] or 0
                    if rate:
                        _slot_rate[i] = rate
                    write(f"llm{i}_tps", f"{(rate or _slot_rate.get(i, 0)):.0f}")
                    write(f"llm{i}_live", 2 if gen else (1 if now - _slot_gen_at.get(i, 0) < 120 else 0))
                else:
                    write(f"llm{i}_ctx", "0")
                    write(f"llm{i}_tps", "0")
                    write(f"llm{i}_live", "0")
            # ── ComfyUI metrics: written EVERY tick regardless of installed mode, so
            # whichever template is up always finds live, parseable files (and they all
            # exist before the first comfy-mode install — the numeric-only trap) ──
            global _comfy_active_at, _comfy_spi, _mode, _installed_key, _last_gen_at
            global _hot_at
            cf = m.comfy or {}
            comfy_running = bool(cf.get("comfy_up")) and (
                bool(cf.get("running")) or (cf.get("queue") or 0) > 0)
            if comfy_running:
                _comfy_active_at = now
            # 300s hold (vs llama's 120s): job-to-job gaps include ~1min of wan model
            # init plus VAE decode/save — a shorter hold would flap mid-batch.
            comfy_recent = (now - _comfy_active_at) < 300
            write("comfy_pct", f"{cf.get('pct') or 0:.0f}")
            write("comfy_step", f"{cf.get('step') or 0}")
            write("comfy_steps", f"{cf.get('steps') or 0}")
            spi = cf.get("spi")
            if spi:
                _comfy_spi = spi
            write("comfy_spi", f"{(spi or (_comfy_spi if comfy_recent else 0)):.1f}")
            write("comfy_eta_min", f"{cf.get('eta_min') or 0:.1f}")
            write("comfy_queue", f"{cf.get('queue') or 0}")
            bd, bt = cf.get("batch_done") or 0, cf.get("batch_total") or 0
            write("comfy_batch_done", f"{bd}")
            write("comfy_batch_total", f"{bt}")
            write("comfy_batch_pct", f"{bd / bt * 100:.0f}" if bt else "0")
            write("comfy_failed", f"{cf.get('batch_failed') or 0}")
            write("comfy_live", 2 if comfy_running else (1 if comfy_recent else 0))

            # ── mode FSM: which bottom-section layout should be installed ──
            generating = bool(m.busy) or bool(m.tps)
            if generating:
                _last_gen_at = now
            llama_recent = generating or (now - _last_gen_at) < 120
            # Sticky transitions, current mode wins ties. A live llama decode is never
            # yanked, but llama's idle-hold does NOT block comfy (a stray chat reply
            # shouldn't hide an hours-long render); both-idle keeps the last layout.
            if _mode == "llama" and comfy_running and not generating:
                _mode = "comfy"
            elif _mode == "comfy" and not comfy_recent and llama_recent:
                _mode = "llama"

            # ── fire background: EITHER engine working counts as "inference is
            # running". Igniting is immediate; cooling down waits HOT_HOLD past
            # the last busy tick, so the gaps between chat turns (or between comfy
            # jobs) don't strobe the panel between the calm and burning loops.
            # That hold is not just cosmetic: each swap makes the daemon re-decode
            # the whole loop to RGBA (~2-3s, measured in its journal), so back-to-
            # back turns should cost zero swaps, not one per turn.
            if generating or comfy_running:
                _hot_at = now
            try:
                hot_hold = float(_user_cfg().get("hot_hold_s", HOT_HOLD_DEFAULT))
            except (TypeError, ValueError):
                hot_hold = HOT_HOLD_DEFAULT
            hot = (now - _hot_at) < max(2.0, hot_hold)
            # Published for other rig consumers (wirewatch themes the WireView
            # displays fire/calm off this) so nobody re-derives the hold logic.
            write("hot", "1" if hot else "0")

            # Baked-label strings are template content (image widgets can't update,
            # value_text can't do strings), so a change of mode, model set, or comfy
            # job rebuilds + reinstalls the template. Rare (model switches / ~one
            # comfy job per half hour), so the one re-init flicker is acceptable —
            # and the changed body also busts the daemon's content-keyed asset cache.
            names = tuple((m.llm_models[i]["name"] if i < len(m.llm_models) else "—")
                          for i in range(2))
            job = cf.get("job") or "—"
            # The tray's dashboard type maps onto a template mode; "combined" follows
            # the auto FSM (which keeps tracking underneath so switching back to
            # Combined lands on whichever section is actually active).
            dtype, theme, orient, brightness = read_config()
            eff_mode = {"combined": _mode, "system": "system", "comfy": "comfy_full",
                        "llama": "llama_full", "video": "video", "off": "off"}[dtype]
            # The background contributes its resolved PATH and mtime, not the raw
            # `hot` flag: dropping/replacing/removing a loop then takes effect within
            # one tick, and — because a theme with no fire art resolves to the same
            # path either way — hot toggling only reinstalls when it truly changes
            # the picture. "off" draws no background at all, so it contributes none
            # (otherwise a dark panel would reload every time inference started).
            # Bank labels are baked into the template as text, so they belong in
            # the key: re-cabling fans (or a device dropping off the bus) must
            # redraw the FANS panel, not leave stale labels over new numbers.
            bank_sig = tuple((b["label"], b["slug"], bool(b.get("right")))
                             for b in m.banks)
            bgk = () if eff_mode == "off" else (bg_path(theme, hot),
                                                bg_video_key(theme, hot))
            key = (("comfy", job) if eff_mode in ("comfy", "comfy_full")
                   else (eff_mode,) + names) + (theme,) + bgk + (bank_sig,
                                                                    config_stamp())
            if key != _installed_key:
                try:
                    global _media_set
                    tpl = build_tpl(names, mode=eff_mode, comfy_job=job,
                                    theme=theme, orient=orient, hot=hot,
                                    banks=m.banks)
                    dash._ipc({"method": "SetLcdTemplates", "params": {"templates": [tpl]}})
                    # SetLcdMedia only ONCE per run: the config is byte-identical on
                    # every reinstall and the daemon persists it, while each IPC call
                    # triggers its own config reload — and two reloads seconds apart
                    # can race, tearing down the freshly started h264 stream without
                    # starting a new one (= frozen panel, seen 2026-08-17 19:53).
                    # SetLcdTemplates alone reloads and re-prepares the template.
                    if _media_set != (orient, brightness):
                        # fps pinned to the background-video rate so EVERY template
                        # (with or without a video widget) produces the same encoder
                        # geometry — the daemon's template hot-swap only keeps the
                        # pipeline when canvas/rotation/fps all match.
                        dash._ipc({"method": "SetLcdMedia", "params": {
                            "device_id": f"serial:{dash.lcd_serial()}",
                            "config": {"serial": dash.lcd_serial(), "type": "custom",
                                       "template_id": tpl["id"],
                                       "orientation": ORIENTS[orient],
                                       "brightness": brightness,
                                       "fps": 12.0,
                                       "update_interval_ms": 1000}}})
                        _media_set = (orient, brightness)
                    _installed_key = key
                    print(f"template reinstalled ({eff_mode}): {key}", flush=True)
                except Exception as e:
                    print("template reinstall failed:", e, flush=True)
            # Hold the last real rate when idle — a bare 0 tells you nothing, and a freshly
            # loaded model has no history at all (both the live delta and llama's own
            # last-request gauge read 0 until something has actually been generated).
            global _last_rate
            rate = m.tps or m.last_tps or 0
            if rate:
                _last_rate = rate
            write("llm_tps", f"{(rate or _last_rate):.0f}")
            # Liveness traffic light, so a held-over rate can't masquerade as a live one:
            #   2 = generating right now, 1 = generated in the last 2 min, 0 = stale
            # (generating / _last_gen_at were computed above, ahead of the mode FSM)
            state = 2 if generating else (1 if (now - _last_gen_at) < 120 else 0)
            write("llm_live", state)
            write("llm_accept", f"{m.accept:.0f}" if m.accept is not None else "0")
            write("llm_prefill", f"{m.prefill:.0f}" if m.prefill else "0")
            write("llm_total", f"{m.total_tokens/1000:.1f}")          # k tokens since load
            write("llm_ctx", f"{(m.ctx_used or 0)/1000:.1f}")         # k tokens of context
            for i, d in enumerate((m.depth or [0, 0, 0])[:3]):
                write(f"llm_depth{i}", f"{d:.0f}")
            # tokens per watt — what the 300W cap actually costs on a given model
            gw = sum(g["power"] for g in m.gpus) or 1
            write("llm_tokw", f"{((rate or _last_rate) / gw):.2f}")
            update_ring(m, hot)
            # Render the full dashboard with the PIL layout — the one confirmed to display
            # correctly oriented and full-size on the panel — into a fixed path. The template
            # shows it through a single image widget, so the daemon's autonomous renderer picks
            # up each new frame WITHOUT a config write (no reload, no flicker).
            img = dash.render(m)
            if img.size != (480, 1920):
                img = img.rotate(dash.ROTATE, expand=True)
            tmp = "/run/lianli-dash/dash.png.tmp"
            img.save(tmp, "PNG")
            os.replace(tmp, "/run/lianli-dash/dash.png")
        except Exception as e:
            print("collect:", e, flush=True)
        # Fast-poll the display config inside the 2s tick so tray switches feel
        # instant (~200ms to the reinstall, then the daemon's ~0.5s prepare).
        stamp = config_stamp()
        for _ in range(10):
            time.sleep(0.2)
            if config_stamp() != stamp:
                break


if __name__ == "__main__":
    main()
