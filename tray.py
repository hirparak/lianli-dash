#!/usr/bin/env python3
"""Tray switcher for the Lian Li panel dashboard.

Sits in the bar's SNI tray (omarchy-shell Tray widget). The menu picks the
dashboard TYPE (what data is shown) and THEME (how it's drawn), written to
~/.config/lianli-dash/config.json. The root collector (lianli-dash.service)
fast-polls that file (200ms) and reinstalls the matching template — switching
needs no privileges and lands on the panel in under a second.

Uses AyatanaAppIndicator3 — the modern SNI path; Java-style XEmbed tray icons
don't work on this desktop, appindicators do.
"""
import json
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import GLib, Gtk, AyatanaAppIndicator3 as AppIndicator

CONFIG_FILE = os.path.expanduser("~/.config/lianli-dash/config.json")
TYPES = [
    ("combined", "Combined (auto)"),
    ("system", "System"),
    ("comfy", "ComfyUI"),
    ("llama", "Llama.cpp"),
    ("video", "Video only"),
    ("off", "Screen off"),
]
THEMES = [
    ("default", "Default"),
    ("cardash", "Car dash"),
    ("cyberpunk", "Cyberpunk"),
    ("steampunk", "Steampunk"),
    ("gits", "Ghost in the Shell"),
]


def read_config() -> dict:
    try:
        c = json.load(open(CONFIG_FILE))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def write_config(**updates):
    c = read_config()
    c.update(updates)
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, CONFIG_FILE)


def radio_group(menu, items, current_key, config_key):
    group = None
    for key, text in items:
        item = Gtk.RadioMenuItem.new_with_label_from_widget(group, text)
        group = group or item
        item.set_active(key == current_key)   # before connect: no spurious write
        item.connect("toggled",
                     lambda it, k=key: it.get_active() and write_config(**{config_key: k}))
        menu.append(item)


def build_menu() -> Gtk.Menu:
    cfg = read_config()
    menu = Gtk.Menu()

    hdr = Gtk.MenuItem(label="Dashboard")
    hdr.set_sensitive(False)
    menu.append(hdr)
    radio_group(menu, TYPES, cfg.get("type", "combined"), "type")

    menu.append(Gtk.SeparatorMenuItem())
    hdr = Gtk.MenuItem(label="Theme")
    hdr.set_sensitive(False)
    menu.append(hdr)
    radio_group(menu, THEMES, cfg.get("theme", "default"), "theme")

    menu.append(Gtk.SeparatorMenuItem())
    st = Gtk.MenuItem(label="Settings…")
    st.connect("activate", lambda _i: subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "settings.py")]))
    menu.append(st)
    quit_item = Gtk.MenuItem(label="Quit tray")
    quit_item.connect("activate", Gtk.main_quit)
    menu.append(quit_item)
    menu.show_all()
    return menu


def main():
    # Icon by absolute theme path: themed names (e.g. "video-display") come up
    # as the pink/black missing-texture checkerboard in the omarchy quickshell
    # tray, so ship our own glyph and point the indicator straight at it.
    icon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    ind = AppIndicator.Indicator.new_with_path(
        "lianli-dash", "lianli-dash",
        AppIndicator.IndicatorCategory.APPLICATION_STATUS, icon_dir)
    ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
    ind.set_title("Lian Li dashboard")
    ind.set_menu(build_menu())

    # The menu radios show the config AS OF BUILD TIME, and the theme/type are
    # also changed by the settings app, by collect-side tooling and by scripts —
    # a menu built once at startup ends up ticking the wrong item (or seemingly
    # none). Rebuild whenever config.json's mtime moves; 2s poll is plenty for
    # a menu no one is looking at continuously.
    state = {"mtime": None}

    def refresh():
        try:
            mtime = os.stat(CONFIG_FILE).st_mtime
        except OSError:
            return True
        if mtime != state["mtime"]:
            state["mtime"] = mtime
            ind.set_menu(build_menu())
        return True

    refresh()
    GLib.timeout_add_seconds(2, refresh)
    Gtk.main()


if __name__ == "__main__":
    main()
