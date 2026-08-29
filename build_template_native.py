#!/usr/bin/env python3
"""Generate + install the dashboard as a lianli-daemon TEMPLATE.

Why a template instead of pushing PNGs: every image update goes through the daemon's config,
and any config change triggers a full reload that re-attaches and re-initialises the panel —
a visible flicker on every frame. Template media (MediaType::Custom) is rendered by the
daemon's own autonomous renderer thread, which updates the frame WITHOUT touching config, so
there's no reload and no flicker.

Values come from native daemon sensors where they exist (cpu_usage, mem_usage, nvidia_gpu,
hwmon) and from `cat /run/lianli-dash/<metric>` files written by collect.py for the rest.

Portrait layouts are built from a SECTION REGISTRY (`PAGES`): every page is an ordered list
of sections with a uniform `fn(ctx, y) -> next_y` contract. config.json's `pages` map lets
the settings app show/hide and reorder sections per page; `flex` sections absorb whatever
height the fixed sections leave over, so hiding a section grows the graphs instead of
leaving a void. Landscape layouts are fixed compositions (columns, not stacks) and are not
configurable.

usage: build_template.py [install] [--mode M] [--theme T] [--orient O] [--job L] [names...]
"""
import functools
import json
import os
import sys
from collections import namedtuple

import paths
import dash  # for _ipc / LCD_SERIAL

# Compose NATIVELY PORTRAIT to match the panel (480x1920). Composing 1920x480 and relying on
# `rotated` scaled the landscape canvas down to fit the panel's 480px width — everything came
# out 4x too small AND still landscape on a vertically-mounted screen.
W, H = 480, 1920
TPL_ID = "lianli-dash"
# Absolute path: template-relative names only resolve for templates that BUNDLE their font
# (the built-ins ship a .ttf in their asset folder). Ours uses a system font.
# NB: the omarchy 4 update (2026-08-17) removed the "...NerdFontMono-*" file names;
# only the "...NerdFont-*" variants ship now.
FONT = {"path": "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf"}

# ── palette engine: every layout draws from these module globals; _apply_theme()
# swaps them per theme at the top of build(), so one layout re-skins for free ──
BASE_PALETTE = dict(
    FG=[232, 236, 243, 255], DIM=[128, 138, 155, 255],
    COOL=[86, 170, 235, 255], GOOD=[74, 200, 130, 255],
    WARN=[232, 150, 50, 255], HOT=[232, 62, 58, 255],
    NEEDLE=[225, 45, 50, 255], FACE=[14, 15, 18, 210],
    BARBG=[30, 34, 43, 255])

THEME_STYLE = {
    "default": {},
    "cardash": {},   # base palette; the layout itself carries the style
    "cyberpunk": dict(FG=[225, 245, 255, 255], DIM=[95, 125, 165, 255],
                      COOL=[0, 229, 255, 255], GOOD=[57, 255, 120, 255],
                      WARN=[255, 45, 170, 255], HOT=[255, 55, 95, 255],
                      NEEDLE=[255, 45, 170, 255], FACE=[10, 8, 20, 215],
                      BARBG=[20, 18, 40, 255]),
    "steampunk": dict(FG=[236, 224, 196, 255], DIM=[158, 130, 92, 255],
                      COOL=[110, 160, 140, 255], GOOD=[130, 180, 120, 255],
                      WARN=[214, 122, 50, 255], HOT=[196, 60, 38, 255],
                      NEEDLE=[218, 160, 60, 255], FACE=[26, 18, 11, 225],
                      BARBG=[40, 30, 20, 255]),
    "gits": dict(FG=[205, 240, 225, 255], DIM=[85, 135, 120, 255],
                 COOL=[0, 216, 180, 255], GOOD=[110, 250, 190, 255],
                 WARN=[255, 140, 45, 255], HOT=[255, 80, 60, 255],
                 NEEDLE=[0, 216, 180, 255], FACE=[4, 18, 15, 215],
                 BARBG=[10, 32, 28, 255]),
}

# config.json theme_colors keys -> palette slots (the settings app's colour pickers)
USER_COLOR_SLOTS = (("accent", "NEEDLE"), ("good", "GOOD"),
                    ("warn", "WARN"), ("hot", "HOT"))


def _apply_theme(theme, user=None):
    """Set the palette globals for `theme`, with optional per-theme user colour
    overrides (from config.json `theme_colors`) layered on top. The derived
    range/lamp tables are rebuilt from the merged values."""
    global FG, DIM, COOL, GOOD, WARN, HOT, RED, CARBON, BARBG
    global RANGES_LOAD, RANGES_TEMP, RANGES_CPU, LAMP
    p = {**BASE_PALETTE, **THEME_STYLE.get(theme, {})}
    for src, dst in USER_COLOR_SLOTS:
        v = (user or {}).get(src)
        if (isinstance(v, list) and len(v) == 4
                and all(isinstance(c, int) and 0 <= c <= 255 for c in v)):
            p[dst] = v
    FG, DIM, COOL, GOOD = p["FG"], p["DIM"], p["COOL"], p["GOOD"]
    WARN, HOT, RED, CARBON, BARBG = (p["WARN"], p["HOT"], p["NEEDLE"],
                                     p["FACE"], p["BARBG"])
    RANGES_LOAD = [{"max": 40.0, "color": FG[:3], "alpha": 245},
                   {"max": 75.0, "color": WARN[:3], "alpha": 245},
                   {"max": None, "color": HOT[:3], "alpha": 255}]
    RANGES_TEMP = [{"max": 55.0, "color": COOL[:3], "alpha": 245},
                   {"max": 75.0, "color": WARN[:3], "alpha": 245},
                   {"max": None, "color": HOT[:3], "alpha": 255}]
    # Threadripper (TRX50, Tjmax 95): 70-85 under load is by design, so the
    # generic 55/75 bands coloured a healthy 77 as hot. Water-cooled GPUs and
    # coolant keep the generic bands — 75 genuinely IS hot for those.
    RANGES_CPU = [{"max": 70.0, "color": COOL[:3], "alpha": 245},
                  {"max": 85.0, "color": WARN[:3], "alpha": 245},
                  {"max": None, "color": HOT[:3], "alpha": 255}]
    LAMP = [{"max": 0.5, "color": [70, 76, 90], "alpha": 255},
            {"max": 1.5, "color": WARN[:3], "alpha": 255},
            {"max": None, "color": GOOD[:3], "alpha": 255}]


_apply_theme("default")

# ── optional video background (the L-Connect party trick, but UNDER the widgets) ──────
# Drop a loop at BG_VIDEO (use ./set-bg-video.sh to transcode any clip) and the template
# gains a full-bleed Video widget as its bottom layer. The daemon pre-decodes the WHOLE
# clip to RGBA at widget size (480x1920 = 3.5MiB/frame — RAM is length×fps×3.5MiB, so
# keep loops short; set-bg-video.sh caps at 8s/12fps ≈ 340MiB) and loops it autonomously.
# The widget's fps also becomes the template's composite rate (the daemon takes the max
# widget fps), so the stat widgets keep their own 1s update cadence while the compositor
# runs fast enough for smooth video. opacity < 1 blends toward the near-black template
# background = a readability scrim for free. Delete the file to go back to static.
BG_VIDEO = paths.BG_VIDEO
BG_VIDEO_FPS = float(os.environ.get("DASH_BG_VIDEO_FPS", "12"))
BG_VIDEO_OPACITY = float(os.environ.get("DASH_BG_VIDEO_OPACITY", "0.35"))
# The fire loop is scrimmed less hard than the idle one — at the idle 0.35 the
# burn reads as merely warm; +0.10 makes it properly angry while the dim section
# headers still hold up (compared side by side at 0.35/0.45/0.55).
BG_HOT_BOOST = float(os.environ.get("DASH_BG_HOT_BOOST", "0.10"))

# Per-theme backgrounds: each theme may carry its own loop; missing file = flat
# colour. The default theme keeps the classic BG_VIDEO path so set-bg-video.sh
# behaviour is unchanged.
BG_DIR = paths.BG_DIR
THEME_BG = {
    "default": BG_VIDEO,
    "cardash": os.path.join(BG_DIR, "cardash.mp4"),
    "cyberpunk": os.path.join(BG_DIR, "cyberpunk.mp4"),
    "steampunk": os.path.join(BG_DIR, "steampunk.mp4"),
    "gits": os.path.join(BG_DIR, "gits.mp4"),
}

# "Inference is running" background: the SAME SCENE on fire, so the swap reads as
# the dashboard igniting rather than as a different wallpaper. Purely additive —
# a theme with no _fire loop on disk just keeps its idle loop while hot.
THEME_BG_FIRE = {t: os.path.join(BG_DIR, f"{t}_fire.mp4") for t in THEME_BG}


def bg_path(theme="default", hot=False):
    """The loop to play for this theme in this state, falling back to the idle
    loop when the theme has no fire art installed."""
    idle = THEME_BG.get(theme, BG_VIDEO)
    if hot:
        fire = THEME_BG_FIRE.get(theme, "")
        if fire and os.path.exists(fire):
            return fire
    return idle


# Shared with collect.py / tray.py / settings.py — the user-tweakable knobs live
# in the same config.json the tray writes; the collector's reinstall key includes
# the file's mtime, so settings changes land on the panel within a second.
CONFIG_FILE = paths.CONFIG_FILE


def _user_cfg() -> dict:
    try:
        c = json.load(open(CONFIG_FILE))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def bg_video_key(theme="default", hot=False):
    """Reinstall-key component: mtime of the theme's background video, 0 if
    absent. Lets the collector hot-swap a background within a tick."""
    try:
        return os.path.getmtime(bg_path(theme, hot))
    except OSError:
        return 0


def cat(metric: str) -> dict:
    return {"type": "command", "cmd": f"cat /run/lianli-dash/{metric}"}


def w(id_, kind, x, y, width, height, **extra):
    """Build a widget from TOP-LEFT coordinates.

    The daemon anchors widgets by their CENTRE (confirmed by the built-in neon-us88, where a
    value and the gauge it sits inside share identical x/y), so we convert here — that keeps
    the layout below readable in normal top-left terms.
    """
    d = {"id": id_, "kind": kind,
         "x": float(x) + float(width) / 2.0,
         "y": float(y) + float(height) / 2.0,
         "width": float(width), "height": float(height)}
    d.update(extra)
    return d


def label(id_, text, x, y, width, height, size=20, color=None, align="left"):
    return w(id_, {"type": "label", "text": text, "font": FONT, "font_size": float(size),
                   "color": color or DIM, "align": align, "letter_spacing": 1.0},
             x, y, width, height)


def value(id_, source, x, y, width, height, size=44, unit="", fmt="{:.0}",
          ranges=None, color=None, align="left", vmin=0.0, vmax=100.0):
    return w(id_, {"type": "value_text", "source": source, "format": fmt, "unit": unit,
                   "font": FONT, "font_size": float(size), "color": color or FG,
                   "align": align, "value_min": vmin, "value_max": vmax,
                   "ranges": ranges or [], "letter_spacing": 0.0},
             x, y, width, height, update_interval_ms=1000)


def bar(id_, source, x, y, width, height, vmax=100.0, ranges=None):
    return w(id_, {"type": "horizontal_bar", "source": source, "value_min": 0.0,
                   "value_max": vmax, "background_color": BARBG,
                   "corner_radius": 4.0, "ranges": ranges or RANGES_LOAD},
             x, y, width, height, update_interval_ms=1000)


def vbar(id_, source, x, y, width, height, vmax=100.0, ranges=None):
    return w(id_, {"type": "vertical_bar", "source": source, "value_min": 0.0,
                   "value_max": vmax, "background_color": BARBG,
                   "corner_radius": 4.0, "ranges": ranges or RANGES_LOAD},
             x, y, width, height, update_interval_ms=1000)


def spark(id_, source, x, y, width, height, vmax=100.0, color=None):
    c = color or COOL
    return w(id_, {"type": "sparkline", "source": source, "value_min": 0.0, "value_max": vmax,
                   "auto_range": False, "history_length": 90, "line_width": 2.0,
                   "line_color": c, "fill_color": c[:3] + [70],
                   "background_color": [30, 30, 30, 0], "ranges": [],
                   "border_color": [80, 90, 110, 0], "border_width": 0.0,
                   "corner_radius": 0.0, "padding": 2.0, "show_points": False,
                   "point_radius": 2.0, "show_baseline": False, "baseline_value": 0.0,
                   "baseline_color": [140, 140, 160, 160], "baseline_width": 1.0},
             x, y, width, height, update_interval_ms=1000)


def _dial(ws, id_, title, source, x, y, size, vmax, unit="", redline=0.8,
          vmin=0.0, value_size=30, fmt="{:.0}", ticks=9, arc=None,
          title_size=17):
    """One analogue gauge: speedometer + watch-style title in the upper face +
    digital value in the lower face. Palette comes from the theme globals."""
    arc = arc or [{"max": vmin + (vmax - vmin) * redline,
                   "color": [235, 238, 245], "alpha": 60},
                  {"max": None, "color": RED[:3], "alpha": 200}]
    ws.append(w(id_, {"type": "speedometer", "source": source,
                      "value_min": vmin, "value_max": vmax,
                      "start_angle": 135.0, "sweep_angle": 270.0,
                      "needle_color": RED, "tick_color": [235, 238, 245, 200],
                      "tick_count": ticks, "background_color": CARBON,
                      "ranges": arc, "show_gauge": True, "show_needle": True,
                      "needle_width": 4.0, "needle_length_pct": 0.8},
                x, y, size, size, update_interval_ms=1000))
    ws.append(label(id_ + "t", title, x, y + size * 0.24, size, 24,
                    size=title_size, color=FG, align="center"))
    ws.append(value(id_ + "v", source, x, y + size * 0.64, size, 32,
                    size=value_size, unit=unit, align="center", vmax=vmax,
                    vmin=vmin, fmt=fmt))


def _mini_dials(ws, xs, y, S, minis):
    row_y = y
    for i, (lbl, metric, vmax_) in enumerate(minis):
        if i and i % 3 == 0:
            row_y += S + 8
        _dial(ws, f"dm{i}", lbl, cat(metric), xs[i % 3], row_y, S, vmax_,
              value_size=18, ticks=7, title_size=11)
    return row_y + S + 10


def fan_minis(banks):
    """Mini dials for the cluster themes: one per bank side, pump last. Derived
    from the same configured banks as the portrait FANS panel."""
    cols = dash.fan_columns()
    out = []
    for b in banks or []:
        for i, side in enumerate(("left", "right")):
            if not b.get(side):
                continue
            # A dial per bank SIDE, so the two sides need distinguishing: fold in
            # the column name ("TOP ICUE" / "TOP NOCT"), else both read "TOP RAD".
            stem = b["label"].split()[0][:5]
            col = (cols[i] or "").strip()[:4]
            out.append(((f"{stem} {col}" if col else b["label"][:10]),
                        f"{b['slug']}_{side}", 3000.0))
    out.append(("PUMP", "pump_rpm", 6000.0))
    return tuple(out[:6])


# ── system page v3 (2026-08-29, Kieran's layout): per engine a HUGE centred
# percentage flanked by two vertical memory bars, watts+temp side by side
# beneath; then large coolant (small flow under) beside large chassis (small
# room under); thin separators so nothing blends. No sparklines. ─────────────

def s_sys_sep(ctx, y):
    ctx.ws.append(label(f"{ctx.sid}sep", "─" * 30, ctx.PAD, y, ctx.CW, 16,
                        size=12, color=DIM, align="center"))
    return y + 26


def s_sys_engine(ctx, y, title="CPU", util="cpu_util", watts="cpu_watts",
                 temp="cpu_temp", mem="ram_pct", memlbl="RAM", wmax=500.0,
                 tranges=None):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    BARW, BARH = 44, 300
    # flanking memory bars, one each side of the big number
    ws.append(vbar(f"{ctx.sid}bl", cat(mem), PAD, y, BARW, BARH,
                   ranges=[{"max": None, "color": RED[:3], "alpha": 210}]))
    ws.append(vbar(f"{ctx.sid}br", cat(mem), PAD + CW - BARW, y, BARW, BARH,
                   ranges=[{"max": None, "color": RED[:3], "alpha": 210}]))
    ws.append(label(f"{ctx.sid}bll", memlbl, PAD - 4, y + BARH + 4, BARW + 8,
                    18, size=11, align="center"))
    ws.append(label(f"{ctx.sid}brl", memlbl, PAD + CW - BARW - 4, y + BARH + 4,
                    BARW + 8, 18, size=11, align="center"))
    # the big number, centred between the bars, with the engine's name tucked
    # DIRECTLY beneath it (inside the bar region) — with a gap it reads as a
    # header for the watts/temp row below instead of a caption of its number
    ws.append(value(f"{ctx.sid}u", cat(util), PAD + BARW + 8, y + 52,
                    CW - 2 * BARW - 16, 150, size=104, unit="%", color=FG,
                    align="center", vmax=100.0, ranges=RANGES_LOAD))
    ws.append(label(f"{ctx.sid}t", title, PAD + BARW + 8, y + 188,
                    CW - 2 * BARW - 16, 34, size=28, color=DIM,
                    align="center"))
    y += BARH + 14
    # watts + temp beneath: label and value each CENTRED on its half-column
    # axis, so the pair sits symmetrically under the centred headline number.
    half = CW // 2
    ws.append(label(f"{ctx.sid}wl", "WATTS", PAD, y, half, 18, size=12,
                    align="center"))
    ws.append(value(f"{ctx.sid}wv", cat(watts), PAD, y + 18, half, 54,
                    size=44, unit="W", color=DIM, vmax=wmax, align="center"))
    ws.append(label(f"{ctx.sid}tl", "TEMP", PAD + half, y, half, 18, size=12,
                    align="center"))
    ws.append(value(f"{ctx.sid}tv", cat(temp), PAD + half, y + 18, half, 54,
                    size=44, unit="°", vmax=100.0, align="center",
                    ranges=tranges or RANGES_TEMP))
    return y + 84


def s_sys_cpu(ctx, y):
    return s_sys_engine(ctx, y, "CPU", "cpu_util", "cpu_watts", "cpu_temp",
                        mem="ram_pct", memlbl="RAM", tranges=RANGES_CPU)


def s_sys_gpu(ctx, y, i=0):
    return s_sys_engine(ctx, y, f"GPU {i}", f"gpu{i}_util", f"gpu{i}_power",
                        f"gpu{i}_temp", mem=f"gpu{i}_vram_pct", memlbl="VRAM",
                        wmax=700.0)


def s_sys_therm(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    half = CW // 2
    # A 2x2 of EQUAL-SIZE numbers: coolant/chassis over flow/room, each with
    # its heading snug above. Flow's unit lives in the heading — "102 L/h" at
    # 84pt would not fit a half-column, and mixed value sizes read as a
    # hierarchy that does not exist.
    cells = ((0, 0, "COOLANT", "coolant", "°", RANGES_TEMP, None, "{:.1}"),
             (1, 0, "CHASSIS", "case_temp", "°", RANGES_TEMP, None, "{:.1}"),
             (0, 1, "FLOW L/H", "flow", "", None, COOL, "{:.0}"),
             (1, 1, "ROOM", "room_temp", "°", None, DIM, "{:.1}"))
    ROW = 148
    for cx_i, cy_i, lbl, metric, unit, ranges, col, fmt in cells:
        x = PAD + cx_i * half
        cy = y + cy_i * ROW
        ws.append(label(f"{ctx.sid}l{cx_i}{cy_i}", lbl, x, cy, half, 30,
                        size=24, color=DIM, align="center"))
        ws.append(value(f"{ctx.sid}v{cx_i}{cy_i}", cat(metric), x, cy + 30,
                        half, 106, size=84, unit=unit, vmax=400.0, fmt=fmt,
                        align="center", color=col, ranges=ranges))
    return y + 2 * ROW + 10


def s_cq_sys_watts(ctx, y):
    """System-page power row: CPU / GPU0 / GPU1 watts as three mini dials."""
    xs = (ctx.PAD, ctx.PAD + 151, ctx.PAD + 302)
    S = 142
    minis = (("CPU W", "cpu_watts", 500.0), ("GPU 0 W", "gpu0_power", 700.0),
             ("GPU 1 W", "gpu1_power", 700.0))
    return _mini_dials(ctx.ws, xs, y, S, minis)


def s_cq_sys_therm(ctx, y):
    """System-page thermal strip: chassis / room / flow (coolant already has a
    big dial in s_cq_temps)."""
    PAD, ws = ctx.PAD, ctx.ws
    rows = (("CHASSIS", "case_temp", "°"), ("ROOM", "room_temp", "°"),
            ("FLOW", "flow", " L/h"))
    x = PAD
    for n, (lbl, metric, unit) in enumerate(rows):
        ws.append(label(f"{ctx.sid}tl{n}", lbl, x, y, 140, 18, size=12))
        ws.append(value(f"{ctx.sid}tv{n}", cat(metric), x, y + 18, 140, 34,
                        size=26, unit=unit, color=FG, vmax=400.0, fmt="{:.1}"))
        x += 148
    return y + 62


# ═══════════════════════════════ SECTION REGISTRY ════════════════════════════════
# Every portrait page is an ordered list of sections. Contract:
#   fn(ctx, y) -> next_y        draws widgets into ctx.ws starting at y
# Flex sections receive their granted height in ctx.alloc and MUST return
# y + ctx.alloc. ctx.sid is the section key — use it to namespace widget ids so
# they stay stable when the user reorders sections.

class Ctx:
    def __init__(self, ws, mode, slot_names, comfy_job, banks=None):
        self.ws = ws
        self.mode = mode
        self.slot_names = slot_names or ("—", "—")
        self.comfy_job = comfy_job
        self.banks = list(banks or [])
        self.W, self.H = W, H
        self.PAD = 18
        self.CW = W - 36
        self.alloc = 0
        self.sid = ""

    # ── id-namespaced composite helpers ──
    def sect(self, y, title):
        self.ws.append(label(f"sec_{self.sid}", title, self.PAD, y, self.CW, 30,
                             size=22))
        return y + 34

    def stat(self, n, lx, ly, name, metric, unit, size=30, col=None,
             vmax=10000.0, fmt="{:.0}", ranges=None):
        self.ws.append(label(f"{self.sid}_l{n}", name, lx, ly, 200, 20, size=13))
        self.ws.append(value(f"{self.sid}_v{n}", cat(metric), lx, ly + 18, 210,
                             40, size=size, unit=unit, color=col, vmax=vmax,
                             fmt=fmt, ranges=ranges))

    def bigstat(self, n, lx, ly, name, metric, unit, col=None, vmax=10000.0,
                fmt="{:.0}", ranges=None):
        self.ws.append(label(f"{self.sid}_bl{n}", name, lx, ly, 210, 22, size=15))
        self.ws.append(value(f"{self.sid}_bv{n}", cat(metric), lx, ly + 22, 220,
                             56, size=44, unit=unit, color=col, vmax=vmax,
                             fmt=fmt, ranges=ranges))


Sec = namedtuple("Sec", "key label fn flex min_h modes",
                 defaults=(False, 0, None))


def build_stack(ctx, entries, y0):
    """Two-pass stack layout: measure fixed heights with flex at min_h, then
    draw for real with the leftover split across the enabled flex sections so
    the page always ends exactly at H - PAD (when at least one flex section is
    visible). Overflow clips at the panel edge — the settings preview shows it."""
    live = [e for e in entries if not e.modes or ctx.mode in e.modes]
    flex = [e for e in live if e.flex]

    real_ws = ctx.ws
    ctx.ws = []                    # measure pass draws into a scratch list
    y = y0
    for e in live:
        ctx.sid = e.key
        ctx.alloc = e.min_h if e.flex else 0
        y = e.fn(ctx, y)
    ctx.ws = real_ws

    allocs = {}
    if flex:
        fixed_used = (y - y0) - sum(e.min_h for e in flex)
        leftover = (ctx.H - ctx.PAD) - y0 - fixed_used
        per = max(0, leftover - sum(e.min_h for e in flex))
        share, rem = divmod(per, len(flex))
        for i, e in enumerate(flex):
            allocs[e.key] = e.min_h + share + (rem if i == len(flex) - 1 else 0)

    y = y0
    for e in live:
        ctx.sid = e.key
        ctx.alloc = allocs.get(e.key, 0)
        y = e.fn(ctx, y)
    return y


# ── default-family sections (portrait) ───────────────────────────────────────────

def s_header(ctx, y):
    ctx.ws.append(label("hdr", dash.TITLE, ctx.PAD, y, 300, 56, size=42, color=FG))
    return y + 76


def s_clock(ctx, y):
    # anchored top-right regardless of stack position; contributes no height
    ctx.ws.append(w("clock", {"type": "clock_digital", "format": "%H:%M",
                              "font": FONT, "font_size": 40.0, "color": DIM,
                              "align": "right"},
                    ctx.PAD, 14, ctx.CW, 50))
    return y


def s_gpu(ctx, y, i=0, spark_h=78):
    """One GPU block: big util/temp, load bar, VRAM/power, VRAM bar, then
    power/util/VRAM sparklines OVERLAID in one rect (power/util/VRAM)."""
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    y = ctx.sect(y, f"GPU {i}")
    ws.append(value(f"g{i}u", {"type": "nvidia_gpu", "gpu_index": i, "metric": "usage"},
                    PAD, y, 240, 86, size=76, unit="%", ranges=RANGES_LOAD))
    ws.append(value(f"g{i}t", {"type": "nvidia_gpu", "gpu_index": i, "metric": "temp"},
                    PAD, y + 8, CW, 70, size=58, unit="°", ranges=RANGES_TEMP,
                    align="right", vmax=100.0))
    y += 92
    ws.append(bar(f"g{i}b", {"type": "nvidia_gpu", "gpu_index": i, "metric": "usage"},
                  PAD, y, CW, 14))
    y += 24
    ws.append(value(f"g{i}v", cat(f"gpu{i}_vram"), PAD, y, 200, 40, size=32,
                    unit="G", fmt="{:.1}", color=COOL, vmax=100.0))
    ws.append(value(f"g{i}p", cat(f"gpu{i}_power"), PAD, y, CW, 40, size=32,
                    unit="W", align="right", vmax=600.0))
    y += 44
    ws.append(bar(f"g{i}vb", cat(f"gpu{i}_vram_pct"), PAD, y, CW, 10,
                  ranges=[{"max": None, "color": COOL[:3], "alpha": 235}]))
    y += 18
    # 12V-2x6 connector health from wirewatch: worst pin current, coloured on
    # wirewatch's own thresholds (warn 9.2 A / crit 9.5 A), and pin-current
    # spread (collector publishes 0 below the 15 A load floor).
    pin_ranges = [{"max": 9.2, "color": FG[:3], "alpha": 245},
                  {"max": 9.5, "color": WARN[:3], "alpha": 255},
                  {"max": None, "color": HOT[:3], "alpha": 255}]
    spread_ranges = [{"max": 12.0, "color": DIM[:3], "alpha": 245},
                     {"max": 20.0, "color": WARN[:3], "alpha": 255},
                     {"max": None, "color": HOT[:3], "alpha": 255}]
    ws.append(label(f"g{i}pl", "PINS", PAD, y + 3, 60, 22))
    ws.append(value(f"g{i}pa", cat(f"gpu{i}_pins_a"), PAD + 58, y, 100, 26, size=24,
                    unit="A", fmt="{:.1}", ranges=pin_ranges, vmax=12.0))
    ws.append(value(f"g{i}ps", cat(f"gpu{i}_pins_pct"), PAD + 140, y, CW - 140, 26,
                    size=24, unit="%", align="right", ranges=spread_ranges, vmax=50.0))
    y += 28
    for sid, src, vmax, col, fill in (
            ("p", cat(f"gpu{i}_power"), 320.0, WARN, 55),   # power
            ("u", {"type": "nvidia_gpu", "gpu_index": i, "metric": "usage"},
             100.0, FG, 0),                                  # util
            ("v", cat(f"gpu{i}_vram_pct"), 100.0, COOL, 0)):  # VRAM
        sp = spark(f"g{i}s{sid}", src, PAD, y, CW, spark_h, vmax=vmax, color=col)
        sp["kind"]["fill_color"] = col[:3] + [fill]   # only the base series is filled
        ws.append(sp)
    return y + spark_h + 16


def s_cpu(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    y = ctx.sect(y, "CPU")
    ws.append(value("cu", {"type": "cpu_usage"}, PAD, y, 240, 86, size=76, unit="%",
                    ranges=RANGES_LOAD))
    ws.append(value("ct", {"type": "hwmon", "name": "k10temp", "label": "Tctl"},
                    PAD, y + 8, CW, 70, size=58, unit="°", ranges=RANGES_CPU,
                    align="right", vmax=100.0))
    y += 92
    ws.append(bar("cb", {"type": "cpu_usage"}, PAD, y, CW, 14))
    y += 24
    ws.append(value("rv", cat("ram_used"), PAD, y, 220, 40, size=32, unit="G RAM",
                    vmax=128.0))
    y += 44
    ws.append(bar("rb", cat("ram_pct"), PAD, y, CW, 10,
                  ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
    y += 18
    ws.append(spark("cs", {"type": "cpu_usage"}, PAD, y, CW, 70, color=FG))
    return y + 86


def s_loop(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    y = ctx.sect(y, "LOOP")
    # flow left, coolant right — so the temperature column stacks with the
    # GPU/CPU temps above it
    ws.append(value("lf", cat("flow"), PAD, y + 12, 240, 56, size=40,
                    unit=" L/h", color=COOL, vmax=300.0))
    ws.append(value("lc", {"type": "hwmon", "name": "highflownext",
                           "label": "Coolant temp"},
                    PAD, y, CW, 80, size=64, unit="°", ranges=RANGES_TEMP,
                    fmt="{:.1}", align="right", vmax=60.0))
    y += 86
    ws.append(label("lp1l", "PUMP 1", PAD, y - 2, 120, 20, size=13))
    ws.append(value("lp", cat("pump_duty"), PAD, y + 14, 110, 38, size=28,
                    unit="%"))
    ws.append(value("lpr", cat("pump_rpm"), PAD + 105, y + 20, 110, 30,
                    size=20, unit="rpm", color=DIM, vmax=6000.0))
    ws.append(label("lp2l", "PUMP 2", PAD + 240, y - 2, 120, 20, size=13))
    ws.append(value("lp2", cat("pump2_duty"), PAD + 240, y + 14, 110, 38,
                    size=28, unit="%"))
    ws.append(value("lp2r", cat("pump2_rpm"), PAD + 345, y + 20, 110, 30,
                    size=20, unit="rpm", color=DIM, vmax=6000.0))
    y += 54
    ws.append(label("lca", "CASE", PAD, y + 2, 90, 20, size=13))
    ws.append(value("lcav", cat("case_temp"), PAD + 70, y - 6, 130, 34,
                    size=26, unit="°", fmt="{:.1}",
                    ranges=[{"max": 42.0, "color": COOL[:3], "alpha": 245},
                            {"max": 48.0, "color": WARN[:3], "alpha": 245},
                            {"max": None, "color": HOT[:3], "alpha": 255}],
                    vmax=60.0))
    ws.append(label("lrm", "ROOM", PAD + 240, y + 2, 90, 20, size=13))
    ws.append(value("lrmv", cat("room_temp"), PAD + 310, y - 6, 130, 34,
                    size=26, unit="°", fmt="{:.1}", color=DIM, vmax=60.0))
    return y + 42


def s_fans(ctx, y):
    """One row per configured fan bank. Labels, metric keys and which columns
    exist all come from config.json via the banks the collector resolved, so a
    rig with three hubs and a rig with one both render correctly."""
    PAD, ws = ctx.PAD, ctx.ws
    banks = ctx.banks or []
    cols = dash.fan_columns()
    y = ctx.sect(y, "FANS")
    if not banks:
        ws.append(label("fnone", "no fans detected", PAD, y, 300, 30, size=18,
                        color=DIM))
        return y + 42
    has_right = any(b.get("right") for b in banks)
    if cols[0]:
        ws.append(label("fh1", cols[0], PAD + 200, y - 4, 120, 26, size=18))
    if has_right and cols[1]:
        ws.append(label("fh2", cols[1], PAD + 330, y - 4, 130, 26, size=18))
    y += 24
    for i, b in enumerate(banks):
        # hard cap: a long device name would otherwise run into the value column
        ws.append(label(f"fb{i}", b["label"][:11], PAD, y, 190, 44, size=30, color=FG))
        ws.append(value(f"fi{i}", cat(f"{b['slug']}_left"), PAD + 190, y, 130, 44,
                        size=32, vmax=3000.0))
        if b.get("right"):
            ws.append(value(f"fn{i}", cat(f"{b['slug']}_right"), PAD + 320, y,
                            130, 44, size=32, vmax=3000.0))
        y += 50
    return y + 12


def s_history(ctx, y):
    """Long-window history sparks (system page). Flex: the four rows share the
    granted height."""
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    y0 = y
    y = ctx.sect(y, "HISTORY")
    rows = (("GPU 0 UTIL", {"type": "nvidia_gpu", "gpu_index": 0,
                            "metric": "usage"}, 100.0, FG),
            ("GPU 1 UTIL", {"type": "nvidia_gpu", "gpu_index": 1,
                            "metric": "usage"}, 100.0, DIM),
            ("CPU", {"type": "cpu_usage"}, 100.0, WARN),
            ("COOLANT °C", cat("coolant"), 60.0, COOL))
    row_h = max(60, (ctx.alloc - 34) // len(rows))
    for hi, (lbl, src, vmax, col) in enumerate(rows):
        ws.append(label(f"hl{hi}", lbl, PAD, y, 240, 18, size=13))
        y += 20
        sp = spark(f"hs{hi}", src, PAD, y, CW, row_h - 28, vmax=vmax, color=col)
        sp["kind"]["history_length"] = 900   # 15 min at 1s samples
        ws.append(sp)
        y += row_h - 20
    return y0 + ctx.alloc


def s_llama_models(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    y = ctx.sect(y, "LLAMA.CPP")
    names = ctx.slot_names
    for i in range(2):
        nm = names[i] if i < len(names) and names[i] else "—"
        ws.append(label(f"mn{i}", nm[:22], PAD, y + 6, 250, 26, size=18,
                        color=FG if nm != "—" else DIM))
        ws.append(value(f"mc{i}", cat(f"llm{i}_ctx"), PAD + 252, y + 10, 76, 22,
                        size=15, unit="k", color=DIM, vmax=200.0))
        ws.append(value(f"mt{i}", cat(f"llm{i}_tps"), PAD + 310, y, 100, 38, size=28,
                        color=GOOD, align="right", vmax=1000.0))
        ws.append(bar(f"ml{i}", cat(f"llm{i}_live"), PAD + 420, y + 8, 24, 24,
                      vmax=2.0, ranges=LAMP))
        y += 44
    return y


def s_llama_spark(ctx, y):
    ctx.ws.append(spark("ms", cat("llm_tps"), ctx.PAD, y, ctx.CW, ctx.alloc,
                        vmax=300.0, color=GOOD))
    return y + ctx.alloc


def s_inference(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("il", "INFERENCE", PAD, y, CW, 28, size=20))
    y += 32
    ctx.stat(0, PAD, y, "PREFILL", "llm_prefill", " t/s", col=COOL, vmax=5000.0)
    ctx.stat(1, PAD + 235, y, "DECODE", "llm_tps", " t/s", col=GOOD, vmax=1000.0)
    # liveness lamp beside DECODE: green = generating, amber = recent, grey = stale.
    ws.append(bar("live", cat("llm_live"), PAD + 420, y + 26, 26, 26, vmax=2.0,
                  ranges=LAMP))
    y += 64
    ctx.stat(2, PAD, y, "TOKENS/WATT", "llm_tokw", "", col=FG, vmax=10.0)
    ctx.stat(3, PAD + 235, y, "TOTAL", "llm_total", "k", col=DIM, vmax=100000.0)
    y += 64
    # spec-decode acceptance by draft position: how deep the drafter stays correct
    ws.append(label("dl", "SPEC DEPTH", PAD, y, 200, 20, size=13))
    for i in range(3):
        ws.append(bar(f"dp{i}", cat(f"llm_depth{i}"), PAD, y + 22 + i * 12, 200, 8,
                      ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
    return y + 54


def s_comfy_job(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    y = ctx.sect(y, "COMFYUI")
    job = ctx.comfy_job or "—"
    ws.append(label("cjob", job[:34], PAD, y, 380, 26, size=18,
                    color=FG if job != "—" else DIM))
    ws.append(bar("clive", cat("comfy_live"), PAD + 420, y, 24, 24, vmax=2.0,
                  ranges=LAMP))
    return y + 34


def s_comfy_progress(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(bar("cpb", cat("comfy_pct"), PAD, y, CW, 18, vmax=100.0,
                  ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
    y += 28
    ws.append(value("cs1", cat("comfy_step"), PAD, y, 80, 40, size=32,
                    color=FG, vmax=200.0))
    ws.append(label("csl", "/", PAD + 82, y + 4, 24, 30, size=24))
    ws.append(value("cs2", cat("comfy_steps"), PAD + 108, y, 80, 40, size=32,
                    color=DIM, vmax=200.0))
    ws.append(value("cpc", cat("comfy_pct"), PAD, y, CW, 40, size=32, unit="%",
                    align="right", color=GOOD, vmax=100.0))
    return y + 48


def s_comfy_stats(ctx, y):
    PAD = ctx.PAD
    ctx.stat(0, PAD, y, "S/IT", "comfy_spi", " s/it", col=COOL, vmax=300.0,
             fmt="{:.1}")
    ctx.stat(1, PAD + 235, y, "ETA", "comfy_eta_min", "m", col=GOOD, vmax=600.0)
    y += 64
    ctx.stat(2, PAD, y, "QUEUE", "comfy_queue", "", col=FG, vmax=100.0)
    ctx.stat(3, PAD + 235, y, "FAILED", "comfy_failed", "", vmax=100.0,
             ranges=[{"max": 0.5, "color": DIM[:3], "alpha": 245},
                     {"max": None, "color": [232, 62, 58], "alpha": 255}])
    return y + 66


def s_comfy_batch(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("cbl", "BATCH", PAD, y, 200, 20, size=13))
    ws.append(value("cbd", cat("comfy_batch_done"), PAD, y + 18, 90, 40, size=30,
                    color=FG, vmax=1000.0))
    ws.append(label("cbs", "/", PAD + 92, y + 22, 24, 30, size=24))
    ws.append(value("cbt", cat("comfy_batch_total"), PAD + 118, y + 18, 90, 40,
                    size=30, color=DIM, vmax=1000.0))
    y += 62
    ws.append(bar("cbb", cat("comfy_batch_pct"), PAD, y, CW, 12, vmax=100.0,
                  ranges=[{"max": None, "color": COOL[:3], "alpha": 235}]))
    return y + 24


def s_comfy_spark(ctx, y):
    # s/it history over ~one job (30 min at 2s ticks): warm-up ramp + drift
    sp = spark("csp", cat("comfy_spi"), ctx.PAD, y, ctx.CW, ctx.alloc,
               vmax=120.0, color=COOL)
    sp["kind"]["history_length"] = 1800
    ctx.ws.append(sp)
    return y + ctx.alloc


# ── comfy_full page sections ──

def s_cf_job(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    y = ctx.sect(y, "COMFYUI")
    job = ctx.comfy_job or "—"
    ws.append(label("cjob", job[:26], PAD, y, 396, 32, size=22,
                    color=FG if job != "—" else DIM))
    ws.append(bar("clive", cat("comfy_live"), PAD + 416, y + 2, 28, 28, vmax=2.0,
                  ranges=LAMP))
    return y + 44


def s_cf_progress(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(bar("cpb", cat("comfy_pct"), PAD, y, CW, 26, vmax=100.0,
                  ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
    y += 38
    ws.append(value("cs1", cat("comfy_step"), PAD, y, 120, 64, size=54,
                    color=FG, vmax=200.0))
    ws.append(label("csl", "/", PAD + 126, y + 12, 30, 44, size=40))
    ws.append(value("cs2", cat("comfy_steps"), PAD + 160, y, 120, 64, size=54,
                    color=DIM, vmax=200.0))
    ws.append(value("cpc", cat("comfy_pct"), PAD, y, CW, 64, size=54, unit="%",
                    align="right", color=GOOD, vmax=100.0))
    return y + 78


def s_cf_bigstats(ctx, y):
    PAD = ctx.PAD
    ctx.bigstat(0, PAD, y, "S/IT", "comfy_spi", "", col=COOL, vmax=300.0,
                fmt="{:.1}")
    ctx.bigstat(1, PAD + 235, y, "ETA", "comfy_eta_min", "m", col=GOOD,
                vmax=600.0)
    y += 92
    ctx.bigstat(2, PAD, y, "QUEUE", "comfy_queue", "", col=FG, vmax=100.0)
    ctx.bigstat(3, PAD + 235, y, "FAILED", "comfy_failed", "", vmax=100.0,
                ranges=[{"max": 0.5, "color": DIM[:3], "alpha": 245},
                        {"max": None, "color": [232, 62, 58], "alpha": 255}])
    return y + 92


def s_cf_batch(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("cbl", "BATCH", PAD, y, 200, 22, size=15))
    ws.append(value("cbd", cat("comfy_batch_done"), PAD, y + 22, 120, 56, size=44,
                    color=FG, vmax=1000.0))
    ws.append(label("cbs", "/", PAD + 126, y + 30, 30, 40, size=34))
    ws.append(value("cbt", cat("comfy_batch_total"), PAD + 160, y + 22, 120, 56,
                    size=44, color=DIM, vmax=1000.0))
    y += 86
    ws.append(bar("cbb", cat("comfy_batch_pct"), PAD, y, CW, 18, vmax=100.0,
                  ranges=[{"max": None, "color": COOL[:3], "alpha": 235}]))
    return y + 30


def s_cf_spark(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("cspl", "S/IT HISTORY", PAD, y, 220, 18, size=13))
    sp = spark("csp", cat("comfy_spi"), PAD, y + 20, CW, ctx.alloc - 36,
               vmax=120.0, color=COOL)
    sp["kind"]["history_length"] = 1800
    ws.append(sp)
    return y + ctx.alloc


def s_cf_system(ctx, y):
    PAD = ctx.PAD
    y = ctx.sect(y, "SYSTEM")
    ctx.stat(0, PAD, y, "CPU", "cpu_util", "%", size=40, col=FG, vmax=100.0)
    ctx.stat(1, PAD + 235, y, "RAM", "ram_used", "G", size=40, col=GOOD,
             vmax=128.0)
    y += 88
    ctx.stat(2, PAD, y, "COOLANT", "coolant", "°", size=40, col=COOL,
             fmt="{:.1}", vmax=60.0)
    ctx.stat(3, PAD + 235, y, "CASE AIR", "case_temp", "°", size=40, fmt="{:.1}",
             vmax=60.0)
    y += 88
    ctx.stat(4, PAD, y, "FLOW", "flow", " L/h", size=40, col=COOL, vmax=300.0)
    ctx.stat(5, PAD + 235, y, "PUMP", "pump_rpm", "rpm", size=40, col=DIM,
             vmax=6000.0)
    return y + 58


# ── llama_full page sections ──

def s_lf_slots(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    y = ctx.sect(y, "LLAMA.CPP")
    names = ctx.slot_names
    for i in range(2):
        nm = names[i] if i < len(names) and names[i] else "—"
        ws.append(label(f"mn{i}", nm[:24], PAD, y + 8, 280, 30, size=20,
                        color=FG if nm != "—" else DIM))
        ws.append(value(f"mc{i}", cat(f"llm{i}_ctx"), PAD + 280, y + 14, 76, 24,
                        size=16, unit="k", color=DIM, vmax=200.0))
        ws.append(value(f"mt{i}", cat(f"llm{i}_tps"), PAD + 300, y, 110, 46, size=34,
                        color=GOOD, align="right", vmax=1000.0))
        ws.append(bar(f"ml{i}", cat(f"llm{i}_live"), PAD + 420, y + 10, 26, 26,
                      vmax=2.0, ranges=LAMP))
        y += 56
    return y + 8


def s_lf_spark(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("msl", "DECODE T/S", PAD, y, 220, 18, size=13))
    ws.append(spark("ms", cat("llm_tps"), PAD, y + 20, CW, ctx.alloc - 36,
                    vmax=300.0, color=GOOD))
    return y + ctx.alloc


def s_lf_inference(ctx, y):
    PAD = ctx.PAD
    y = ctx.sect(y, "INFERENCE")
    ctx.stat(0, PAD, y, "PREFILL", "llm_prefill", " t/s", size=40, col=COOL,
             vmax=5000.0)
    ctx.stat(1, PAD + 235, y, "DECODE", "llm_tps", " t/s", size=40, col=GOOD,
             vmax=1000.0)
    y += 88
    ctx.stat(2, PAD, y, "TOKENS/WATT", "llm_tokw", "", size=40, col=FG,
             vmax=10.0)
    ctx.stat(3, PAD + 235, y, "TOTAL", "llm_total", "k", size=40, col=DIM,
             vmax=100000.0)
    y += 88
    ctx.stat(4, PAD, y, "SPEC ACCEPT", "llm_accept", "%", size=40, col=COOL,
             vmax=100.0)
    ctx.stat(5, PAD + 235, y, "CONTEXT", "llm_ctx", "k", size=40, col=DIM,
             vmax=200.0)
    return y + 88


def s_lf_depth(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    ws.append(label("dl", "SPEC DEPTH", PAD, y, 200, 20, size=13))
    for i in range(3):
        ws.append(bar(f"dp{i}", cat(f"llm_depth{i}"), PAD, y + 22 + i * 12, 200, 8,
                      ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
    return y + 66


def s_lf_system(ctx, y):
    PAD = ctx.PAD
    y = ctx.sect(y, "SYSTEM")
    ctx.stat(6, PAD, y, "CPU", "cpu_util", "%", size=40, col=FG, vmax=100.0)
    ctx.stat(7, PAD + 235, y, "RAM", "ram_used", "G", size=40, col=GOOD,
             vmax=128.0)
    y += 88
    ctx.stat(8, PAD, y, "COOLANT", "coolant", "°", size=40, col=COOL,
             fmt="{:.1}", vmax=60.0)
    ctx.stat(9, PAD + 235, y, "CASE AIR", "case_temp", "°", size=40, fmt="{:.1}",
             vmax=60.0)
    return y + 58


# ── cluster-family sections (cardash / steampunk) ────────────────────────────────

def s_cl_header(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("hdr", dash.TITLE, PAD, y, 300, 44, size=34, color=FG))
    ws.append(w("clock", {"type": "clock_digital", "format": "%H:%M", "font": FONT,
                          "font_size": 32.0, "color": RED, "align": "right"},
                PAD, y + 4, CW, 40))
    ws.append(bar("hline", cat("comfy_live"), PAD, y + 50, CW, 4, vmax=0.001,
                  ranges=[{"max": None, "color": RED[:3], "alpha": 170}]))
    return y + 66


def _dial_row2(ctx, y, size, left, right, gap=10):
    """Two dials side by side at an explicit size (the fixed-222 _dial_row above
    predates the simplified cluster pages, which use a smaller temperature dial)."""
    span = ctx.CW - size * 2
    for (did, title, source, kw), x in ((left, ctx.PAD),
                                        (right, ctx.PAD + size + span)):
        _dial(ctx.ws, did, title, source, x, y, size, **kw)
    return y + size + gap


def _dial_row(ctx, y, left, right, gap=10):
    """Two dials side by side; left/right are (id, title, source, kwargs)."""
    D = 222
    XR = ctx.PAD + 228
    for (did, title, source, kw), x in ((left, ctx.PAD), (right, XR)):
        _dial(ctx.ws, did, title, source, x, y, D, **kw)
    return y + D + gap


def s_cl_gpu_util(ctx, y):
    return _dial_row(
        ctx, y,
        ("du0", "GPU 0", {"type": "nvidia_gpu", "gpu_index": 0,
                          "metric": "usage"}, dict(vmax=100, unit="%")),
        ("du1", "GPU 1", {"type": "nvidia_gpu", "gpu_index": 1,
                          "metric": "usage"}, dict(vmax=100, unit="%")))


def s_cl_gpu_temp(ctx, y):
    kw = dict(vmax=100, unit="°", redline=0.75, ticks=5, title_size=15)
    return _dial_row(
        ctx, y,
        ("dt0", "GPU 0 °C", {"type": "nvidia_gpu", "gpu_index": 0,
                             "metric": "temp"}, kw),
        ("dt1", "GPU 1 °C", {"type": "nvidia_gpu", "gpu_index": 1,
                             "metric": "temp"}, kw))


def s_cl_cpu(ctx, y):
    return _dial_row(
        ctx, y,
        ("dcp", "CPU", {"type": "cpu_usage"}, dict(vmax=100, unit="%")),
        ("dct", "CPU °C", cat("cpu_temp"),
         dict(vmax=100, unit="°", redline=0.85, ticks=5, title_size=15)))


def s_cl_coolant_flow(ctx, y):
    # flow danger is LOW flow: red arc at the bottom of the range
    flow_arc = [{"max": 70.0, "color": RED[:3], "alpha": 200},
                {"max": None, "color": [235, 238, 245], "alpha": 60}]
    return _dial_row(
        ctx, y,
        ("dco", "COOLANT", cat("coolant"),
         dict(vmax=60, unit="°", vmin=20.0, redline=0.62, ticks=5,
              title_size=15)),
        ("dfl", "FLOW L/H", cat("flow"),
         dict(vmax=300.0, ticks=6, arc=flow_arc, title_size=15)),
        gap=12)


def s_cl_minis(ctx, y):
    return _mini_dials(ctx.ws, (ctx.PAD, ctx.PAD + 151, ctx.PAD + 302), y, 142,
                       fan_minis(ctx.banks))


def s_cd_strip(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    ws.append(label("lca", "CASE", PAD, y + 10, 60, 18, size=12))
    ws.append(value("vca", cat("case_temp"), PAD + 54, y + 2, 90, 30, size=22,
                    unit="°", fmt="{:.1}", color=FG, vmax=60.0))
    for i in range(2):
        ws.append(label(f"lvr{i}", f"VRAM {i}", PAD + 160, y + 4 + i * 20, 74, 16,
                        size=11))
        ws.append(bar(f"vb{i}", cat(f"gpu{i}_vram_pct"), PAD + 238, y + 6 + i * 20,
                      206, 10, ranges=[{"max": None, "color": RED[:3], "alpha": 210}]))
    return y + 50


def _dial_wide(ctx, y, did, title, source, size, gap=8, **kw):
    """A single dial centred across the full column. Two-across caps at ~222px,
    which is barely larger than the old layout — going full width is what makes
    the headline dials actually readable from across the room."""
    _dial(ctx.ws, did, title, source, ctx.PAD + (ctx.CW - size) // 2, y, size, **kw)
    return y + size + gap


# ── simplified cluster sections (cardash / steampunk) ────────────────────────
# The dial-cluster themes had 14 gauges competing on one 480px column, six of
# them 142px mini-dials whose labels were unreadable over a busy background.
# These sections keep only what is worth a glance: three big activity dials,
# memory as bars (a bar reads faster than a gauge for "how full"), and the
# temperatures and pump smaller underneath. No sparklines.
ACT = 302          # headline activity dials, full width
TMP = 212          # temperature dials, two across
PUMPD = 216        # pump, centred on its own row


def s_cq_activity(ctx, y):
    y = _dial_wide(ctx, y, "qg0", "GPU 0",
                   {"type": "nvidia_gpu", "gpu_index": 0, "metric": "usage"},
                   ACT, vmax=100, unit="%", value_size=48, title_size=25)
    y = _dial_wide(ctx, y, "qg1", "GPU 1",
                   {"type": "nvidia_gpu", "gpu_index": 1, "metric": "usage"},
                   ACT, vmax=100, unit="%", value_size=48, title_size=25)
    return _dial_wide(ctx, y, "qcp", "CPU", {"type": "cpu_usage"},
                      ACT, vmax=100, unit="%", value_size=48, title_size=25)


def s_cq_mem(ctx, y):
    """VRAM per GPU and system RAM as bars — 'how full' is a length, not an angle."""
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    rows = (("VRAM 0", cat("gpu0_vram_pct")), ("VRAM 1", cat("gpu1_vram_pct")),
            ("RAM", cat("ram_pct")))
    for i, (lbl, src) in enumerate(rows):
        ws.append(label(f"qml{i}", lbl, PAD, y + 2, 86, 22, size=15, color=DIM))
        # a bar shows "how full" at a glance; the number answers "how full exactly"
        ws.append(bar(f"qmb{i}", src, PAD + 92, y + 4, CW - 160, 18,
                      ranges=[{"max": None, "color": RED[:3], "alpha": 210}]))
        ws.append(value(f"qmv{i}", src, PAD + CW - 62, y, 62, 24, size=17,
                        unit="%", align="right", color=FG))
        y += 32
    return y + 12


def s_cq_temps(ctx, y):
    y = _dial_row2(ctx, y, TMP,
                   ("qtc", "CPU °C", cat("cpu_temp"),
                    dict(vmax=100, unit="°", vmin=20.0, redline=0.85)),
                   ("qtl", "COOLANT", cat("coolant"),
                    dict(vmax=60, unit="°", vmin=20.0, redline=0.62)))
    return _dial_row2(ctx, y, TMP,
                      ("qt0", "GPU 0 °C",
                       {"type": "nvidia_gpu", "gpu_index": 0, "metric": "temp"},
                       dict(vmax=100, unit="°", vmin=20.0, redline=0.8)),
                      ("qt1", "GPU 1 °C",
                       {"type": "nvidia_gpu", "gpu_index": 1, "metric": "temp"},
                       dict(vmax=100, unit="°", vmin=20.0, redline=0.8)))


def s_cq_pump(ctx, y):
    return _dial_wide(ctx, y, "qpm", "PUMP", cat("pump_rpm"), PUMPD,
                      vmax=6000, redline=0.9, value_size=30)


def s_cq_infer(ctx, y):
    """Compact inference readout — only drawn on the combined page while llama or
    comfy is actually working, so the simplified themes do not go blind on the
    thing the fire background is signalling."""
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    if ctx.mode == "comfy":
        ws.append(label("qin", "COMFYUI  " + (ctx.comfy_job or "—")[:20], PAD, y,
                        CW, 22, size=14, color=DIM, align="center"))
        ws.append(value("qiv", cat("comfy_pct"), PAD, y + 26, CW, 60, size=54,
                        align="center", color=RED, unit="%", vmax=100.0))
        return y + 92
    ws.append(label("qin", "LLAMA.CPP  " + (ctx.slot_names[0] or "—")[:20], PAD, y,
                    CW, 22, size=14, color=DIM, align="center"))
    ws.append(value("qiv", cat("llm_tps"), PAD, y + 24, CW, 68, size=64,
                    align="center", color=RED, vmax=1000.0))
    ws.append(label("qil", "TOKENS / SEC", PAD, y + 94, CW, 18, size=11,
                    align="center"))
    return y + 118


def s_cd_cluster_comfy(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("cjob", "COMFYUI  " + (ctx.comfy_job or "—")[:22], PAD, y,
                    CW, 22, size=14, color=DIM, align="center"))
    y += 26
    ws.append(value("cpc", cat("comfy_pct"), PAD, y, CW, 92, size=82, unit="%",
                    align="center", color=RED, vmax=100.0))
    y += 96
    ws.append(bar("cpb", cat("comfy_pct"), PAD, y, CW, 12, vmax=100.0,
                  ranges=[{"max": None, "color": RED[:3], "alpha": 235}]))
    y += 22
    ws.append(value("cbd", cat("comfy_batch_done"), PAD, y, 90, 36, size=26,
                    color=FG, vmax=1000.0))
    ws.append(label("cbs", "/", PAD + 92, y + 4, 22, 26, size=20))
    ws.append(value("cbt", cat("comfy_batch_total"), PAD + 116, y, 90, 36,
                    size=26, color=DIM, vmax=1000.0))
    ws.append(value("ceta", cat("comfy_eta_min"), PAD, y, CW, 36, size=26,
                    unit="m", align="right", color=FG, vmax=600.0))
    return y + 44


def s_cd_cluster_llama(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    nm = ctx.slot_names[0] or "—"
    ws.append(label("mn0", "LLAMA.CPP  " + nm[:20], PAD, y, CW, 22, size=14,
                    color=DIM, align="center"))
    y += 26
    ws.append(value("tps", cat("llm_tps"), PAD, y, CW, 92, size=82,
                    align="center", color=RED, vmax=1000.0))
    y += 96
    ws.append(label("tpsl", "TOKENS / SEC", PAD, y, CW, 20, size=11,
                    align="center"))
    y += 24
    ws.append(bar("live", cat("llm_live"), PAD + CW // 2 - 13, y, 26, 10,
                  vmax=2.0, ranges=LAMP))
    return y + 18


def s_cd_spark(ctx, y):
    if ctx.mode == "comfy":
        src, vmax = cat("comfy_spi"), 120.0
    else:
        src, vmax = cat("llm_tps"), 300.0
    sp = spark("csp", src, ctx.PAD, y, ctx.CW, ctx.alloc, vmax=vmax, color=RED)
    sp["kind"]["history_length"] = 900
    ctx.ws.append(sp)
    return y + ctx.alloc


# cluster system extras

def s_cs_ram_case(ctx, y):
    D = 222
    _dial(ctx.ws, "dram", "RAM GB", cat("ram_used"), ctx.PAD, y, D, 128.0,
          ticks=5, title_size=15)
    _dial(ctx.ws, "dcase", "CASE AIR", cat("case_temp"), ctx.PAD + 228, y, D,
          60.0, unit="°", vmin=15.0, redline=0.7, ticks=5, title_size=15)
    return y + D + 12


def s_cs_vram(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    for i in range(2):
        ws.append(label(f"lvr{i}", f"VRAM {i}", PAD, y + 4 + i * 20, 74, 16,
                        size=11))
        ws.append(bar(f"vb{i}", cat(f"gpu{i}_vram_pct"), PAD + 80, y + 6 + i * 20,
                      CW - 80, 10,
                      ranges=[{"max": None, "color": RED[:3], "alpha": 210}]))
    return y + 52


def s_cs_history(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("hl0", "GPU 0 UTIL", PAD, y, 240, 18, size=12))
    sp = spark("hs0", {"type": "nvidia_gpu", "gpu_index": 0, "metric": "usage"},
               PAD, y + 20, CW, ctx.alloc - 20, vmax=100.0, color=RED)
    sp["kind"]["history_length"] = 900
    ws.append(sp)
    return y + ctx.alloc


# cluster comfy_full extras

def s_ccf_gpu_dials(ctx, y):
    kw = dict(vmax=100, unit="°", redline=0.75, ticks=5, title_size=15)
    return _dial_row(
        ctx, y,
        ("du0", "GPU 0", {"type": "nvidia_gpu", "gpu_index": 0,
                          "metric": "usage"}, dict(vmax=100, unit="%")),
        ("dt0", "GPU 0 °C", {"type": "nvidia_gpu", "gpu_index": 0,
                             "metric": "temp"}, kw))


def s_ccf_rate_dials(ctx, y):
    return _dial_row(
        ctx, y,
        ("dsit", "SEC / IT", cat("comfy_spi"),
         dict(vmax=120.0, ticks=5, title_size=14)),
        ("dbat", "BATCH %", cat("comfy_batch_pct"),
         dict(vmax=100.0, unit="%", ticks=5, title_size=14)),
        gap=14)


def s_ccf_job(ctx, y):
    ctx.ws.append(label("cjob", "COMFYUI  " + (ctx.comfy_job or "—")[:22],
                        ctx.PAD, y, ctx.CW, 22, size=14, color=DIM,
                        align="center"))
    return y + 26


def s_ccf_progress(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(value("cpc", cat("comfy_pct"), PAD, y, CW, 96, size=86, unit="%",
                    align="center", color=RED, vmax=100.0))
    y += 100
    ws.append(bar("cpb", cat("comfy_pct"), PAD, y, CW, 12, vmax=100.0,
                  ranges=[{"max": None, "color": RED[:3], "alpha": 235}]))
    y += 24
    ws.append(value("cs1", cat("comfy_step"), PAD, y, 90, 40, size=30, color=FG,
                    vmax=200.0))
    ws.append(label("csl", "/", PAD + 92, y + 6, 22, 26, size=20))
    ws.append(value("cs2", cat("comfy_steps"), PAD + 116, y, 90, 40, size=30,
                    color=DIM, vmax=200.0))
    ws.append(value("ceta", cat("comfy_eta_min"), PAD, y, CW, 40, size=30,
                    unit="m", align="right", color=FG, vmax=600.0))
    return y + 48


def s_ccf_batch(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(value("cbd", cat("comfy_batch_done"), PAD, y, 90, 36, size=26,
                    color=FG, vmax=1000.0))
    ws.append(label("cbs", "/", PAD + 92, y + 4, 22, 26, size=20))
    ws.append(value("cbt", cat("comfy_batch_total"), PAD + 116, y, 90, 36,
                    size=26, color=DIM, vmax=1000.0))
    ws.append(value("cq", cat("comfy_queue"), PAD, y, CW, 36, size=26,
                    unit=" queued", align="right", color=DIM, vmax=100.0))
    return y + 50


def s_ccf_spark(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("cspl", "S/IT HISTORY", PAD, y, 220, 16, size=11))
    sp = spark("csp", cat("comfy_spi"), PAD, y + 18, CW, ctx.alloc - 18,
               vmax=120.0, color=RED)
    sp["kind"]["history_length"] = 1800
    ws.append(sp)
    return y + ctx.alloc


# cluster llama_full extras

def s_clf_gpu_dials(ctx, y):
    return _dial_row(
        ctx, y,
        ("du0", "GPU 0", {"type": "nvidia_gpu", "gpu_index": 0,
                          "metric": "usage"}, dict(vmax=100, unit="%")),
        ("du1", "GPU 1", {"type": "nvidia_gpu", "gpu_index": 1,
                          "metric": "usage"}, dict(vmax=100, unit="%")))


def s_clf_rate_dials(ctx, y):
    return _dial_row(
        ctx, y,
        ("dtok", "TOK / SEC", cat("llm_tps"),
         dict(vmax=300.0, ticks=6, title_size=14)),
        ("dpre", "PREFILL", cat("llm_prefill"),
         dict(vmax=3000.0, ticks=6, title_size=14)),
        gap=14)


def s_clf_slots(ctx, y):
    PAD, ws = ctx.PAD, ctx.ws
    names = ctx.slot_names
    for i in range(2):
        nm = names[i] if i < len(names) and names[i] else "—"
        ws.append(label(f"mn{i}", nm[:26], PAD, y, 300, 22, size=15,
                        color=FG if nm != "—" else DIM))
        ws.append(value(f"mt{i}", cat(f"llm{i}_tps"), PAD + 300, y - 4, 110, 30,
                        size=22, color=GOOD, align="right", vmax=1000.0))
        ws.append(bar(f"ml{i}", cat(f"llm{i}_live"), PAD + 420, y, 22, 22,
                      vmax=2.0, ranges=LAMP))
        y += 30
    return y + 8


def s_clf_cluster(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(value("tps", cat("llm_tps"), PAD, y, CW, 96, size=86,
                    align="center", color=RED, vmax=1000.0))
    y += 100
    ws.append(label("tpsl", "TOKENS / SEC", PAD, y, CW, 18, size=11,
                    align="center"))
    y += 24
    ws.append(value("acc", cat("llm_accept"), PAD, y, 150, 36, size=24,
                    unit="% acc", color=COOL, vmax=100.0))
    ws.append(value("ctx", cat("llm_ctx"), PAD, y, CW, 36, size=24, unit="k ctx",
                    align="right", color=DIM, vmax=200.0))
    return y + 46


def s_clf_spark(ctx, y):
    PAD, CW, ws = ctx.PAD, ctx.CW, ctx.ws
    ws.append(label("msl", "DECODE HISTORY", PAD, y, 240, 16, size=11))
    sp = spark("ms", cat("llm_tps"), PAD, y + 18, CW, ctx.alloc - 18,
               vmax=300.0, color=RED)
    sp["kind"]["history_length"] = 900
    ws.append(sp)
    return y + ctx.alloc


# ── page tables: (page start y0, ordered default sections) ──────────────────────
P = functools.partial

PAGE_Y0 = {"default": 10, "cluster": 8}

PAGES = {
    "combined.default": [
        Sec("header", "Header", s_header),
        Sec("clock", "Clock", s_clock),
        Sec("gpu0", "GPU 0", P(s_gpu, i=0)),
        Sec("gpu1", "GPU 1", P(s_gpu, i=1)),
        Sec("cpu", "CPU + RAM", s_cpu),
        Sec("loop", "Loop (coolant/pumps)", s_loop),
        Sec("fans", "Fan banks", s_fans),
        Sec("llama_models", "Model slots", s_llama_models, modes={"llama"}),
        Sec("llama_spark", "Decode graph", s_llama_spark, flex=True, min_h=64,
            modes={"llama"}),
        Sec("inference", "Inference detail", s_inference, modes={"llama"}),
        Sec("comfy_job", "Job header", s_comfy_job, modes={"comfy"}),
        Sec("comfy_progress", "Step progress", s_comfy_progress, modes={"comfy"}),
        Sec("comfy_stats", "S/IT · ETA · queue", s_comfy_stats, modes={"comfy"}),
        Sec("comfy_batch", "Batch", s_comfy_batch, modes={"comfy"}),
        Sec("comfy_spark", "S/IT graph", s_comfy_spark, flex=True, min_h=80,
            modes={"comfy"}),
    ],
    # System v2 (2026-08-29): pure numbers, no sparklines — CPU and per-GPU
    # usage/watts/temp, memory allocations, thermals + flow.
    "system.default": [
        Sec("header", "Header", s_header),
        Sec("clock", "Clock", s_clock),
        Sec("sys_cpu", "CPU", s_sys_cpu),
        Sec("sep1", "Separator", s_sys_sep),
        Sec("sys_gpu0", "GPU 0", P(s_sys_gpu, i=0)),
        Sec("sep2", "Separator", s_sys_sep),
        Sec("sys_gpu1", "GPU 1", P(s_sys_gpu, i=1)),
        Sec("sep3", "Separator", s_sys_sep),
        Sec("sys_therm", "Thermals", s_sys_therm),
    ],
    "comfy_full.default": [
        Sec("header", "Header", s_header),
        Sec("clock", "Clock", s_clock),
        Sec("cf_job", "Job header", s_cf_job),
        Sec("cf_progress", "Step progress", s_cf_progress),
        Sec("cf_bigstats", "S/IT · ETA · queue", s_cf_bigstats),
        Sec("cf_batch", "Batch", s_cf_batch),
        Sec("cf_spark", "S/IT history", s_cf_spark, flex=True, min_h=150),
        Sec("gpu0", "GPU 0", P(s_gpu, i=0, spark_h=360)),
        Sec("cf_system", "System stats", s_cf_system),
    ],
    "llama_full.default": [
        Sec("header", "Header", s_header),
        Sec("clock", "Clock", s_clock),
        Sec("lf_slots", "Model slots", s_lf_slots),
        Sec("lf_spark", "Decode history", s_lf_spark, flex=True, min_h=150),
        Sec("lf_inference", "Inference detail", s_lf_inference),
        Sec("lf_depth", "Spec depth", s_lf_depth),
        Sec("gpu0", "GPU 0", P(s_gpu, i=0, spark_h=150)),
        Sec("gpu1", "GPU 1", P(s_gpu, i=1, spark_h=150)),
        Sec("lf_system", "System stats", s_lf_system),
    ],
    # Simplified dial cluster: three big activity dials, memory as bars, then
    # temperatures and pump. The combined page adds a compact inference readout
    # while llama/comfy is working; system is the pure hardware view.
    "combined.cluster": [
        Sec("cl_header", "Header + clock", s_cl_header),
        Sec("cq_activity", "Activity dials", s_cq_activity),
        Sec("cq_mem", "VRAM + RAM bars", s_cq_mem),
        Sec("cq_infer", "Inference readout", s_cq_infer, modes={"llama", "comfy"}),
        Sec("cq_temps", "Temperature dials", s_cq_temps),
        Sec("cq_pump", "Pump dial", s_cq_pump),
    ],
    # System v2 (2026-08-29): adds the watts row and the chassis/room/flow
    # strip to the simplified dial layout. No sparklines anywhere.
    "system.cluster": [
        Sec("cl_header", "Header + clock", s_cl_header),
        Sec("cq_activity", "Activity dials", s_cq_activity),
        Sec("cq_sys_watts", "Power dials", s_cq_sys_watts),
        Sec("cq_mem", "VRAM + RAM bars", s_cq_mem),
        Sec("cq_temps", "Temperature dials", s_cq_temps),
        Sec("cq_sys_therm", "Chassis / room / flow", s_cq_sys_therm),
    ],
    "comfy_full.cluster": [
        Sec("cl_header", "Header + clock", s_cl_header),
        Sec("ccf_gpu_dials", "GPU dials", s_ccf_gpu_dials),
        Sec("ccf_rate_dials", "S/IT + batch dials", s_ccf_rate_dials),
        Sec("ccf_job", "Job label", s_ccf_job),
        Sec("ccf_progress", "Step progress", s_ccf_progress),
        Sec("ccf_batch", "Batch + queue", s_ccf_batch),
        Sec("cl_minis", "Fan mini-dials", s_cl_minis),
        Sec("ccf_spark", "S/IT history", s_ccf_spark, flex=True, min_h=70),
    ],
    "llama_full.cluster": [
        Sec("cl_header", "Header + clock", s_cl_header),
        Sec("clf_gpu_dials", "GPU dials", s_clf_gpu_dials),
        Sec("clf_rate_dials", "Tok/s + prefill dials", s_clf_rate_dials),
        Sec("clf_slots", "Model slots", s_clf_slots),
        Sec("clf_cluster", "Speed cluster", s_clf_cluster),
        Sec("cl_minis", "Fan mini-dials", s_cl_minis),
        Sec("clf_spark", "Decode history", s_clf_spark, flex=True, min_h=70),
    ],
}

# human-readable page names for the settings app
PAGE_LABELS = {
    "combined": "Combined", "system": "System",
    "comfy_full": "ComfyUI", "llama_full": "Llama.cpp",
}


def theme_family(theme):
    return "cluster" if theme in ("cardash", "steampunk") else "default"


def page_entries(page_key, ucfg):
    """The ordered, visible Sec list for a page: user order from config.json
    `pages`, else registry defaults (with legacy `hide` synthesis)."""
    default = PAGES[page_key]
    order = (ucfg.get("pages") or {}).get(page_key)
    if order is None and ucfg.get("hide") and page_key.endswith(".default"):
        hidden = set(ucfg["hide"]) & {"clock", "loop", "fans"}
        order = [e.key for e in default if e.key not in hidden]
    if order is None:
        return default
    by_key = {e.key: e for e in default}
    seen, out = set(), []
    for k in order:
        if k in by_key and k not in seen:
            out.append(by_key[k])
            seen.add(k)
    return out


# ═════════════════════ landscape layouts (fixed compositions) ════════════════════
# Columns and strips, not stacks — not part of the per-page section config (v1).

def landscape_combined(ws, tpl, mode, slot_names, comfy_job, banks=None) -> dict:
    """Landscape (1920x480) default-style dashboard: four columns — GPU 0,
    GPU 1, CPU + loop, and fans + inference (or history for system mode)."""
    PAD = 12
    COLW = 465
    xs = [PAD + i * (COLW + 12) for i in range(4)]

    def gpu_col(x, i):
        y = 10
        ws.append(label(f"gs{i}", f"GPU {i}", x, y, COLW, 24, size=18))
        y += 28
        ws.append(value(f"g{i}u", {"type": "nvidia_gpu", "gpu_index": i,
                                   "metric": "usage"},
                        x, y, 220, 66, size=56, unit="%", ranges=RANGES_LOAD))
        ws.append(value(f"g{i}t", {"type": "nvidia_gpu", "gpu_index": i,
                                   "metric": "temp"},
                        x, y + 6, COLW, 54, size=44, unit="°",
                        ranges=RANGES_TEMP, align="right", vmax=100.0))
        y += 72
        ws.append(bar(f"g{i}b", {"type": "nvidia_gpu", "gpu_index": i,
                                 "metric": "usage"}, x, y, COLW, 10))
        y += 18
        ws.append(value(f"g{i}v", cat(f"gpu{i}_vram"), x, y, 200, 30, size=24,
                        unit="G", fmt="{:.1}", color=COOL, vmax=100.0))
        ws.append(value(f"g{i}p", cat(f"gpu{i}_power"), x, y, COLW, 30, size=24,
                        unit="W", align="right", vmax=600.0))
        y += 34
        ws.append(bar(f"g{i}vb", cat(f"gpu{i}_vram_pct"), x, y, COLW, 8,
                      ranges=[{"max": None, "color": COOL[:3], "alpha": 235}]))
        y += 14
        for sid, src, vmax_, col, fill in (
                ("p", cat(f"gpu{i}_power"), 320.0, WARN, 55),
                ("u", {"type": "nvidia_gpu", "gpu_index": i, "metric": "usage"},
                 100.0, FG, 0),
                ("v", cat(f"gpu{i}_vram_pct"), 100.0, COOL, 0)):
            sp = spark(f"g{i}s{sid}", src, x, y, COLW, H - 12 - y, vmax=vmax_,
                       color=col)
            sp["kind"]["fill_color"] = col[:3] + [fill]
            ws.append(sp)

    gpu_col(xs[0], 0)
    gpu_col(xs[1], 1)

    # column 3: CPU + loop
    x = xs[2]
    y = 10
    ws.append(label("cs3", "CPU", x, y, COLW, 24, size=18))
    y += 28
    ws.append(value("cu", {"type": "cpu_usage"}, x, y, 200, 66, size=56, unit="%",
                    ranges=RANGES_LOAD))
    ws.append(value("ct", {"type": "hwmon", "name": "k10temp", "label": "Tctl"},
                    x, y + 6, COLW, 54, size=44, unit="°",
                    ranges=RANGES_CPU, align="right", vmax=100.0))
    y += 72
    ws.append(bar("cb", {"type": "cpu_usage"}, x, y, COLW, 10))
    y += 18
    ws.append(value("rv", cat("ram_used"), x, y, 220, 30, size=24, unit="G RAM",
                    vmax=128.0))
    y += 34
    ws.append(bar("rb", cat("ram_pct"), x, y, COLW, 8,
                  ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
    y += 20
    ws.append(label("ls3", "LOOP", x, y, COLW, 22, size=16))
    y += 26
    ws.append(value("lc", cat("coolant"), x, y, 180, 50, size=40, unit="°",
                    fmt="{:.1}", ranges=RANGES_TEMP, vmax=60.0))
    ws.append(value("lf", cat("flow"), x, y + 6, COLW, 40, size=30, unit=" L/h",
                    color=COOL, align="right", vmax=300.0))
    y += 56
    ws.append(value("lp", cat("pump_rpm"), x, y, 220, 30, size=22, unit="rpm",
                    color=DIM, vmax=6000.0))
    ws.append(value("lcav", cat("case_temp"), x, y, COLW, 30, size=22,
                    unit="° case", fmt="{:.1}", align="right", color=DIM,
                    vmax=60.0))
    y += 40
    sp = spark("cs", {"type": "cpu_usage"}, x, y, COLW, H - 12 - y, color=FG)
    ws.append(sp)

    # column 4: fans + inference / history
    x = xs[3]
    y = 10
    ws.append(label("fs4", "FANS", x, y, 200, 24, size=18))
    ws.append(w("clock", {"type": "clock_digital", "format": "%H:%M",
                          "font": FONT, "font_size": 26.0, "color": DIM,
                          "align": "right"}, x, y, COLW, 28))
    y += 30
    for i, (name, key) in enumerate((("TOP", "top"), ("FRONT", "front"),
                                     ("CASE", "case"))):
        ws.append(label(f"fb{i}", name, x, y, 110, 26, size=16, color=FG))
        ws.append(value(f"fi{i}", cat(f"{key}_icue"), x + 120, y, 130, 26,
                        size=20, vmax=3000.0))
        if key != "case":
            ws.append(value(f"fn{i}", cat(f"{key}_noctua"), x + 260, y, 130, 26,
                            size=20, vmax=3000.0))
        y += 30
    y += 8
    if mode == "system":
        ws.append(label("hl0", "GPU 0 UTIL", x, y, 240, 16, size=11))
        y += 18
        h1 = (H - 20 - y - 34) // 2
        sp = spark("hs0", {"type": "nvidia_gpu", "gpu_index": 0,
                           "metric": "usage"}, x, y, COLW, h1, vmax=100.0,
                   color=FG)
        sp["kind"]["history_length"] = 900
        ws.append(sp)
        y += h1 + 6
        ws.append(label("hl1", "COOLANT", x, y, 240, 16, size=11))
        y += 18
        sp = spark("hs1", cat("coolant"), x, y, COLW, H - 12 - y, vmax=60.0,
                   color=COOL)
        sp["kind"]["history_length"] = 900
        ws.append(sp)
    elif mode in ("comfy", "comfy_full"):
        ws.append(label("cjob", "COMFYUI " + (comfy_job or "—")[:18], x, y,
                        COLW, 20, size=13, color=DIM))
        y += 24
        ws.append(value("cpc", cat("comfy_pct"), x, y, 220, 60, size=50,
                        unit="%", color=GOOD, vmax=100.0))
        ws.append(value("ceta", cat("comfy_eta_min"), x, y + 10, COLW, 40,
                        size=28, unit="m", align="right", color=FG, vmax=600.0))
        y += 64
        ws.append(bar("cpb", cat("comfy_pct"), x, y, COLW, 10, vmax=100.0,
                      ranges=[{"max": None, "color": GOOD[:3], "alpha": 235}]))
        y += 18
        ws.append(value("cbd", cat("comfy_batch_done"), x, y, 70, 28, size=20,
                        color=FG, vmax=1000.0))
        ws.append(label("cbs", "/", x + 72, y + 2, 18, 22, size=16))
        ws.append(value("cbt", cat("comfy_batch_total"), x + 92, y, 70, 28,
                        size=20, color=DIM, vmax=1000.0))
        ws.append(bar("clive", cat("comfy_live"), x + COLW - 22, y + 2, 20, 20,
                      vmax=2.0, ranges=LAMP))
        y += 34
        sp = spark("csp", cat("comfy_spi"), x, y, COLW, H - 12 - y, vmax=120.0,
                   color=COOL)
        sp["kind"]["history_length"] = 1800
        ws.append(sp)
    else:
        nm = (slot_names or ("—",))[0] or "—"
        ws.append(label("mn0", "LLAMA.CPP " + nm[:16], x, y, COLW, 20, size=13,
                        color=DIM))
        y += 24
        ws.append(value("tps", cat("llm_tps"), x, y, 260, 60, size=50,
                        color=GOOD, vmax=1000.0))
        ws.append(bar("live", cat("llm_live"), x + COLW - 24, y + 20, 22, 22,
                      vmax=2.0, ranges=LAMP))
        y += 66
        ws.append(value("pre", cat("llm_prefill"), x, y, COLW, 28, size=20,
                        unit=" t/s prefill", color=COOL, vmax=5000.0))
        y += 34
        sp = spark("ms", cat("llm_tps"), x, y, COLW, H - 12 - y, vmax=300.0,
                   color=GOOD)
        ws.append(sp)
    return tpl()


def landscape_cluster(ws, tpl, mode, slot_names, comfy_job, banks=None) -> dict:
    """Landscape cluster (cardash/steampunk): a real car-dash strip — four big
    dials left-to-right, digital cluster + history on the right."""
    dial = lambda *a, **k: _dial(ws, *a, **k)
    D = 300
    dy = (H - D) // 2 - 20
    xs = [16 + i * (D + 8) for i in range(4)]
    dial("du0", "GPU 0", {"type": "nvidia_gpu", "gpu_index": 0,
                          "metric": "usage"}, xs[0], dy, D, 100, unit="%")
    dial("du1", "GPU 1", {"type": "nvidia_gpu", "gpu_index": 1,
                          "metric": "usage"}, xs[1], dy, D, 100, unit="%")
    dial("dcp", "CPU", {"type": "cpu_usage"}, xs[2], dy, D, 100, unit="%")
    dial("dco", "COOLANT", cat("coolant"), xs[3], dy, D, 60, unit="°",
         vmin=20.0, redline=0.62, ticks=5, title_size=15)
    # temps strip under the dials
    ty = dy + D + 6
    ws.append(value("tg0", {"type": "nvidia_gpu", "gpu_index": 0,
                            "metric": "temp"}, xs[0], ty, D, 30, size=22,
                    unit="°", align="center", ranges=RANGES_TEMP,
                    vmax=100.0))
    ws.append(value("tg1", {"type": "nvidia_gpu", "gpu_index": 1,
                            "metric": "temp"}, xs[1], ty, D, 30, size=22,
                    unit="°", align="center", ranges=RANGES_TEMP,
                    vmax=100.0))
    ws.append(value("tcp", cat("cpu_temp"), xs[2], ty, D, 30, size=22,
                    unit="°", align="center", ranges=RANGES_CPU,
                    vmax=100.0))
    ws.append(value("tfl", cat("flow"), xs[3], ty, D, 30, size=22, unit=" L/h",
                    align="center", color=COOL, vmax=300.0))

    # right panel: digital cluster + fans + spark
    x = xs[3] + D + 16
    pw = W - x - 16
    y = 14
    ws.append(w("clock", {"type": "clock_digital", "format": "%H:%M",
                          "font": FONT, "font_size": 30.0, "color": RED,
                          "align": "right"}, x, y, pw, 34))
    ws.append(label("hdr", dash.TITLE, x, y + 2, 260, 30, size=22, color=FG))
    y += 44
    if mode == "system":
        ws.append(label("sysl", "SYSTEM", x, y, pw, 18, size=12, color=DIM))
        y += 24
        ws.append(value("scpu", {"type": "cpu_usage"}, x, y, 200, 60, size=46,
                        unit="%", ranges=RANGES_LOAD))
        ws.append(value("sram", cat("ram_used"), x, y + 12, pw, 40, size=28,
                        unit="G RAM", align="right", color=GOOD, vmax=128.0))
        y += 70
        ws.append(value("scse", cat("case_temp"), x, y, pw, 30, size=22,
                        unit="° case", fmt="{:.1}", color=DIM, vmax=60.0))
        y += 40
        spark_src, spark_vmax = ({"type": "nvidia_gpu", "gpu_index": 0,
                                  "metric": "usage"}, 100.0)
    elif mode in ("comfy", "comfy_full"):
        ws.append(label("cjob", (comfy_job or "—")[:20], x, y, pw, 18,
                        size=12, color=DIM))
        y += 22
        ws.append(value("cpc", cat("comfy_pct"), x, y, pw, 84, size=74, unit="%",
                        align="center", color=RED, vmax=100.0))
        y += 88
        ws.append(bar("cpb", cat("comfy_pct"), x, y, pw, 10, vmax=100.0,
                      ranges=[{"max": None, "color": RED[:3], "alpha": 235}]))
        y += 18
        ws.append(value("cbd", cat("comfy_batch_done"), x, y, 70, 30, size=22,
                        color=FG, vmax=1000.0))
        ws.append(label("cbs", "/", x + 72, y + 2, 18, 24, size=18))
        ws.append(value("cbt", cat("comfy_batch_total"), x + 92, y, 70, 30,
                        size=22, color=DIM, vmax=1000.0))
        ws.append(value("ceta", cat("comfy_eta_min"), x, y, pw, 30, size=22,
                        unit="m", align="right", color=FG, vmax=600.0))
        y += 38
        spark_src, spark_vmax = cat("comfy_spi"), 120.0
    else:
        nm = (slot_names or ("—",))[0] or "—"
        ws.append(label("mn0", nm[:20], x, y, pw, 18, size=12, color=DIM))
        y += 22
        ws.append(value("tps", cat("llm_tps"), x, y, pw, 84, size=74,
                        align="center", color=RED, vmax=1000.0))
        y += 88
        ws.append(label("tpsl", "TOKENS / SEC", x, y, pw, 16, size=10,
                        align="center"))
        y += 20
        ws.append(bar("live", cat("llm_live"), x + pw // 2 - 11, y, 22, 8,
                      vmax=2.0, ranges=LAMP))
        y += 16
        spark_src, spark_vmax = cat("llm_tps"), 300.0
    # fans strip: first three configured banks, then the pump
    strip = [(b["label"][:3], f"{b['slug']}_left") for b in (banks or [])[:3]]
    strip.append(("PMP", "pump_rpm"))
    for i, (lbl, metric) in enumerate(strip):
        fx = x + i * (pw // 4)
        ws.append(label(f"fl{i}", lbl, fx, y, pw // 4 - 6, 14, size=10))
        ws.append(value(f"fv{i}", cat(metric), fx, y + 14, pw // 4 - 6, 22,
                        size=16, color=DIM, vmax=6000.0))
    y += 42
    sp = spark("csp", spark_src, x, y, pw, H - 14 - y, vmax=spark_vmax,
               color=RED)
    sp["kind"]["history_length"] = 900
    ws.append(sp)
    return tpl()


# ═════════════════════════════════════ build ═════════════════════════════════════

def build(slot_names=None, mode="llama", comfy_job=None, theme="default",
          orient="portrait", hot=False, banks=None) -> dict:
    """Build the panel template.

    mode: "llama"/"comfy" = the combined dashboard with that tail (the collector
    auto-switches); "system" / "comfy_full" / "llama_full" = the dedicated
    dashboards; "video" = background loop fullscreen; "off" = black frame.
    theme picks the palette + layout family (cardash/steampunk = dial cluster).
    hot = inference is running, which swaps in the theme's fire loop.
    banks = the fan banks the collector resolved from config (labels + metric
    slugs); the FANS panel and the cluster mini-dials are drawn from them.
    Portrait pages honour config.json `pages` (order + visibility) and
    `theme_colors`; landscape layouts are fixed compositions."""
    global W, H
    W, H = (1920, 480) if orient.startswith("landscape") else (480, 1920)
    ucfg = _user_cfg()
    _apply_theme(theme, (ucfg.get("theme_colors") or {}).get(theme))
    bg_op = float(ucfg.get("bg_opacity", BG_VIDEO_OPACITY))
    bg_fps = float(ucfg.get("bg_fps", BG_VIDEO_FPS))
    ws = []

    def tpl():
        return {"id": TPL_ID, "name": "lianli-dash",
                "base_width": W, "base_height": H, "rotated": False,
                "target_device": None,
                "background": {"type": "color", "rgb": [9, 11, 14, 255]},
                "widgets": ws}

    if mode == "off":
        return tpl()                      # no widgets = black frame, panel dark

    bgp = bg_path(theme, hot)
    hotp = "f" if bgp != THEME_BG.get(theme, BG_VIDEO) else ""
    if hotp:
        # Keyed off hotp, not `hot`: the boost applies exactly when the fire loop
        # is really in play. A theme with no fire art must render byte-identically
        # hot or not, or the template would change while the collector's reinstall
        # key (which tracks the resolved path) would not — a stale panel.
        bg_op = float(ucfg.get("bg_opacity_hot",
                               min(bg_op + BG_HOT_BOOST, 0.85)))

    if mode == "video":
        if os.path.exists(bgp):
            ws.append(w(f"fsv{hotp}{int(bg_video_key(theme, hot))}",
                        {"type": "video", "path": bgp, "loop_playback": True,
                         "opacity": 1.0, "fit": "cover"},
                        0, 0, W, H, fps=bg_fps))
        else:
            ws.append(label("novid", "NO BACKGROUND VIDEO", 18, H // 2 - 20,
                            W - 36, 40, size=22, align="center"))
        return tpl()

    # Background video goes FIRST — widget order is z-order, everything draws on top.
    # The widget id embeds the file's mtime: the daemon only swaps a running media
    # source when the TEMPLATE BODY changes (its asset identity hashes the template,
    # not the video file's content), so replacing the background at the same path
    # would otherwise keep the old decoded frames playing forever.
    if os.path.exists(bgp):
        ws.append(w(f"bgv{hotp}{int(bg_video_key(theme, hot))}",
                    {"type": "video", "path": bgp, "loop_playback": True,
                     "opacity": bg_op, "fit": "cover"},
                    0, 0, W, H, fps=bg_fps))

    if orient.startswith("landscape"):
        if theme in ("cardash", "steampunk"):
            return landscape_cluster(ws, tpl, mode, slot_names, comfy_job, banks)
        return landscape_combined(ws, tpl, mode, slot_names, comfy_job, banks)

    # portrait: section-registry pages
    family = theme_family(theme)
    page = {"llama": "combined", "comfy": "combined", "system": "system",
            "comfy_full": "comfy_full", "llama_full": "llama_full"}[mode]
    page_key = f"{page}.{family}"
    ctx = Ctx(ws, mode, slot_names, comfy_job, banks)
    build_stack(ctx, page_entries(page_key, ucfg), PAGE_Y0[family])
    return tpl()


if __name__ == "__main__":
    # usage: build_template_native.py [install] [--mode M] [--theme T]
    #        [--orient O] [--job LABEL] [name0 name1]
    argv = sys.argv[1:]
    install = False
    mode, job, theme, orient, pos = "llama", None, "default", "portrait", []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "install":
            install = True
        elif a == "--mode":
            i += 1
            mode = argv[i]
        elif a == "--job":
            i += 1
            job = argv[i]
        elif a == "--theme":
            i += 1
            theme = argv[i]
        elif a == "--orient":
            i += 1
            orient = argv[i]
        else:
            pos.append(a)
        i += 1
    names = tuple(pos[:2]) if pos else None
    tpl = build(names, mode=mode, comfy_job=job, theme=theme, orient=orient)
    print(f"template '{tpl['id']}' ({mode}/{theme}): {len(tpl['widgets'])} widgets, {W}x{H}")
    # debug dump of the template we just installed; alongside this script
    json.dump(tpl, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "template.json"), "w"), indent=1)
    if install:
        r = dash._ipc({"method": "SetLcdTemplates", "params": {"templates": [tpl]}})
        print("SetLcdTemplates:", r.get("status"), r.get("message", ""))
