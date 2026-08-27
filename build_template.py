#!/usr/bin/env python3
"""Generate + install the dashboard as a lianli-daemon TEMPLATE.

Why a template instead of pushing PNGs: every image update goes through the daemon's config,
and any config change triggers a full reload that re-attaches and re-initialises the panel —
a visible flicker on every frame. Template media (MediaType::Custom) is rendered by the
daemon's own autonomous renderer thread, which updates the frame WITHOUT touching config, so
there's no reload and no flicker.

Values come from native daemon sensors where they exist (cpu_usage, mem_usage, nvidia_gpu,
hwmon) and from `cat /run/lianli-dash/<metric>` files written by collect.py for the rest.

usage: build_template.py [install]
"""
import json
import sys

import dash  # for _ipc / LCD_SERIAL

# The daemon does NOT rotate the composite — `orientation` only changes the canvas size — so
# compose in the panel's NATIVE buffer orientation (480x1920), the same shape the working
# image-push path used.
W, H = 480, 1920
TPL_ID = "lianli-dash"
# Absolute path: template-relative names only resolve for templates that BUNDLE their font
# (the built-ins ship a .ttf in their asset folder). Ours uses a system font.
FONT = {"path": "/usr/share/fonts/TTF/JetBrainsMonoNerdFontMono-Regular.ttf"}

# palette (matches the rig's white-idle / red-load language)
FG = [232, 236, 243, 255]
DIM = [128, 138, 155, 255]
CARD = [18, 21, 27, 255]
EDGE = [38, 43, 54, 120]
COOL = [86, 170, 235, 255]
GOOD = [74, 200, 130, 255]

# threshold colouring, reused by value widgets
RANGES_LOAD = [{"max": 40.0, "color": [232, 236, 243], "alpha": 245},
               {"max": 75.0, "color": [232, 150, 50], "alpha": 245},
               {"max": None, "color": [232, 62, 58], "alpha": 255}]
RANGES_TEMP = [{"max": 55.0, "color": [86, 170, 235], "alpha": 245},
               {"max": 75.0, "color": [232, 150, 50], "alpha": 245},
               {"max": None, "color": [232, 62, 58], "alpha": 255}]


def cat(metric: str) -> dict:
    return {"type": "command", "cmd": f"cat /run/lianli-dash/{metric}"}


def w(id_, kind, x, y, width, height, **extra):
    d = {"id": id_, "kind": kind, "x": float(x), "y": float(y),
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
                   "value_max": vmax, "background_color": [30, 34, 43, 255],
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


def build() -> dict:
    """One full-bleed image widget showing the frame rendered by collect.py.

    The template widget set can't express this layout as richly as the PIL renderer (and
    value_text is numeric-only), but going through a template is what buys us flicker-free
    updates: the daemon's autonomous renderer re-reads the image on its own schedule instead
    of us rewriting the media config every frame.
    """
    return {"id": TPL_ID, "name": "lianli-dash",
            "base_width": W, "base_height": H, "rotated": False,
            "target_device": None,
            "background": {"type": "color", "rgb": [9, 11, 14, 255]},
            # NOTE: widget x/y are the widget's CENTRE, not its top-left corner. (Confirmed
            # by the built-in neon-us88 template, where a value and the gauge it sits inside
            # share identical x/y.) Anchoring a full-bleed image at 0,0 put its bottom-right
            # corner at the middle of the panel.
            "widgets": [w("frame", {"type": "image", "path": "/run/lianli-dash/dash.png",
                                    "opacity": 1.0, "fit": "stretch"},
                          W / 2, H / 2, W, H, update_interval_ms=1000)]}


if __name__ == "__main__":
    tpl = build()
    print(f"template '{tpl['id']}': {len(tpl['widgets'])} widgets, {W}x{H} rotated")
    json.dump(tpl, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "template.json"), "w"), indent=1)
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        r = dash._ipc({"method": "SetLcdTemplates", "params": {"templates": [tpl]}})
        print("SetLcdTemplates:", r.get("status"), r.get("message", ""))
        r = dash._ipc({"method": "SetLcdMedia", "params": {
            "device_id": f"serial:{dash.lcd_serial()}",
            "config": {"serial": dash.lcd_serial(), "type": "custom",
                       "template_id": TPL_ID, "orientation": 0.0,
                       "update_interval_ms": 1000}}})
        print("SetLcdMedia(custom):", r.get("status"), r.get("message", ""))
