#!/usr/bin/env python3
"""Lian Li Dashboard — production settings app (GTK4 + libadwaita).

Configures every aspect of the case-panel dashboard:
  · Pages    — per dashboard type: show/hide AND reorder every section
  · Appearance — theme, per-theme accent/good/warn/hot colours, backgrounds
  · Display  — type, orientation, brightness, background opacity/fps

Everything writes ~/.config/lianli-dash/config.json; the root collector
fast-polls the file's mtime and reinstalls the matching template, so every
change lands on the physical panel in about a second. The live preview pane
renders the pending template through the daemon's RenderTemplatePreview IPC
(real sensor values) — no root needed, the daemon socket is world-accessible.

Section metadata (keys, labels, default order) is imported straight from
build_template_native.PAGES — one source of truth with the renderer.
"""
import base64
import concurrent.futures
import json
import os
import socket
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

import build_template_native as btn

CONFIG_FILE = os.path.expanduser("~/.config/lianli-dash/config.json")
REPO = os.path.dirname(os.path.abspath(__file__))
DAEMON_SOCK = "/tmp/lianli-daemon.sock"

TYPES = [("combined", "Combined (auto)"), ("system", "System"),
         ("comfy", "ComfyUI"), ("llama", "Llama.cpp"),
         ("video", "Video only"), ("off", "Screen off")]
THEMES = [("default", "Default"), ("cardash", "Car dash"),
          ("cyberpunk", "Cyberpunk"), ("steampunk", "Steampunk"),
          ("gits", "Ghost in the Shell")]
ORIENTS = [("portrait", "Portrait"), ("portrait-flipped", "Portrait (flipped)"),
           ("landscape", "Landscape"), ("landscape-flipped", "Landscape (flipped)")]
COLOR_SLOTS = [("accent", "Accent / needle"), ("good", "Good"),
               ("warn", "Warning"), ("hot", "Hot / alarm")]
# preview render mode per settings page
PAGE_PREVIEW_MODE = {"combined": "llama", "system": "system",
                     "comfy_full": "comfy_full", "llama_full": "llama_full"}
PREVIEW_W, PREVIEW_H = 240, 960


# ── config store ─────────────────────────────────────────────────────────────

def read_config() -> dict:
    try:
        c = json.load(open(CONFIG_FILE))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def write_config(updates: dict):
    """Atomic read-merge-write; dict values deep-merge one level down so
    pages/theme_colors updates don't clobber sibling keys."""
    c = read_config()
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k] = {**c[k], **v}
        else:
            c[k] = v
    # write-side v2 migration: pages materialized -> legacy hide retired
    if "pages" in c and "hide" in c:
        del c["hide"]
    c["version"] = 2
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CONFIG_FILE)


# ── daemon IPC client ────────────────────────────────────────────────────────

class DaemonClient:
    def __init__(self, path=DAEMON_SOCK):
        self.path = path
        self.sock = None
        self.lock = threading.Lock()

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect(self.path)
        self.sock = s

    def call(self, method, params=None):
        req = {"method": method}
        if params is not None:
            req["params"] = params
        payload = (json.dumps(req) + "\n").encode()
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.sendall(payload)
                    buf = b""
                    while b"\n" not in buf:
                        chunk = self.sock.recv(1 << 20)
                        if not chunk:
                            raise ConnectionError("daemon closed")
                        buf += chunk
                    return json.loads(buf.decode())
                except Exception:
                    self.sock = None
                    if attempt:
                        raise
        return {"status": "error", "message": "unreachable"}


# ── preview worker ───────────────────────────────────────────────────────────

class Preview:
    """Debounced, coalescing renderer: build the pending template in-process,
    daemon renders it with live sensors, result lands in a Gtk.Picture."""

    def __init__(self, picture, spinner, client):
        self.picture = picture
        self.spinner = spinner
        self.client = client
        self.hot = False         # preview the fire background instead of the idle one
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = None      # latest requested (mode, tail) while busy
        self.busy = False
        self.timer = None

    def request(self, page, tail="llama"):
        mode = PAGE_PREVIEW_MODE.get(page, "llama")
        if page == "combined":
            mode = tail
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(500, self._fire, mode)

    def _fire(self, mode):
        self.timer = None
        if self.busy:
            self.pending = mode
            return False
        self.busy = True
        self.spinner.set_visible(True)
        self.spinner.start()
        self.pool.submit(self._render, mode)
        return False

    def _render(self, mode):
        try:
            cfg = read_config()
            tpl = btn.build(("model-a", "model-b"), mode=mode,
                            comfy_job="preview-job",
                            theme=cfg.get("theme", "default"),
                            orient=cfg.get("orient", "portrait"),
                            hot=self.hot)
            land = cfg.get("orient", "portrait").startswith("landscape")
            pw, ph = (PREVIEW_H, PREVIEW_W) if land else (PREVIEW_W, PREVIEW_H)
            r = self.client.call("RenderTemplatePreview",
                                 {"template": tpl, "width": pw, "height": ph})
            jpeg = base64.b64decode(r["data"]["jpeg_base64"])
            GLib.idle_add(self._show, jpeg)
        except Exception as e:
            GLib.idle_add(self._fail, str(e))

    def _show(self, jpeg):
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
            loader.write(jpeg)
            loader.close()
            self.picture.set_pixbuf(loader.get_pixbuf())
        except Exception:
            pass
        self._done()

    def _fail(self, msg):
        self._done()

    def _done(self):
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.busy = False
        if self.pending is not None:
            mode, self.pending = self.pending, None
            self._fire(mode)


# ── widgets helpers ──────────────────────────────────────────────────────────

def combo_row(title, items, current, on_change, subtitle=None):
    row = Adw.ComboRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    model = Gtk.StringList()
    keys = [k for k, _ in items]
    for _, text in items:
        model.append(text)
    row.set_model(model)
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected",
                lambda r, _p: on_change(keys[r.get_selected()]))
    return row


def scale_row(title, lo, hi, step, current, on_change, subtitle=None):
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
    scale.set_value(current)
    scale.set_size_request(220, -1)
    scale.set_valign(Gtk.Align.CENTER)
    scale.connect("value-changed", lambda s: on_change(s.get_value()))
    row.add_suffix(scale)
    return row, scale


def rgba_to_list(rgba):
    return [int(round(rgba.red * 255)), int(round(rgba.green * 255)),
            int(round(rgba.blue * 255)), int(round(rgba.alpha * 255))]


def list_to_rgba(lst):
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = [c / 255 for c in lst]
    return rgba


# ── main window ──────────────────────────────────────────────────────────────

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Lian Li Dashboard")
        self.set_default_size(1060, 760)
        self.client = DaemonClient()
        self.toaster = Adw.ToastOverlay()
        self._debounce_timer = None
        self._debounce_updates = {}
        self.current_page = "combined"
        self.combined_tail = "llama"

        # ── shell: header + [sidebar | content | preview] ──
        root = Adw.ToolbarView()
        header = Adw.HeaderBar()
        root.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.sidebar.set_size_request(190, -1)
        self.sidebar.connect("row-selected", self._nav_selected)
        outer.append(self.sidebar)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.stack = Gtk.Stack(hexpand=True)
        scroller = Gtk.ScrolledWindow(hexpand=True)
        scroller.set_child(self.stack)
        outer.append(scroller)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # preview column
        pv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_top=10, margin_bottom=10, margin_start=10,
                         margin_end=10)
        pv_box.append(Gtk.Label(label="Preview", css_classes=["heading"]))
        overlay = Gtk.Overlay()
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
        self.picture.set_size_request(PREVIEW_W + 8, PREVIEW_H + 8)
        frame = Gtk.Frame()
        frame.set_child(self.picture)
        overlay.set_child(frame)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER, visible=False)
        self.spinner.set_size_request(36, 36)
        overlay.add_overlay(self.spinner)
        pv_box.append(overlay)
        self.tail_toggle = Gtk.Box(spacing=0, halign=Gtk.Align.CENTER,
                                   css_classes=["linked"])
        for key, text in (("llama", "Llama tail"), ("comfy", "Comfy tail")):
            b = Gtk.ToggleButton(label=text)
            b.set_active(key == "llama")
            b.connect("toggled", self._tail_toggled, key)
            self.tail_toggle.append(b)
        pv_box.append(self.tail_toggle)
        pv_note = Gtk.Label(
            label="Rendered by the daemon with live sensors",
            css_classes=["dim-label", "caption"], wrap=True)
        pv_box.append(pv_note)
        outer.append(pv_box)

        root.set_content(outer)
        self.toaster.set_child(root)
        self.set_content(self.toaster)

        self.preview = Preview(self.picture, self.spinner, self.client)

        # ── nav + pages ──
        self._nav_rows = []
        family = btn.theme_family(read_config().get("theme", "default"))
        for key, title in (("combined", "Combined"), ("system", "System"),
                           ("comfy_full", "ComfyUI"), ("llama_full", "Llama.cpp"),
                           ("appearance", "Appearance"), ("display", "Display")):
            row = Gtk.ListBoxRow()
            row.page_key = key
            lbl = Gtk.Label(label=title, xalign=0, margin_top=10,
                            margin_bottom=10, margin_start=12)
            row.set_child(lbl)
            self.sidebar.append(row)
            self._nav_rows.append(row)

        for key in ("combined", "system", "comfy_full", "llama_full"):
            self.stack.add_named(self._build_page_editor(key, family), key)
        self.stack.add_named(self._build_appearance(), "appearance")
        self.stack.add_named(self._build_display(), "display")

        self.sidebar.select_row(self._nav_rows[0])
        GLib.idle_add(lambda: self.preview.request("combined",
                                                   self.combined_tail))

    # ── debounced config writes ──
    def queue_write(self, updates):
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(self._debounce_updates.get(k), dict):
                self._debounce_updates[k].update(v)
            else:
                self._debounce_updates[k] = v
        if self._debounce_timer:
            GLib.source_remove(self._debounce_timer)
        self._debounce_timer = GLib.timeout_add(400, self._flush)

    def _flush(self):
        self._debounce_timer = None
        updates, self._debounce_updates = self._debounce_updates, {}
        write_config(updates)
        self.preview.request(self.current_page, self.combined_tail)
        return False

    # ── navigation ──
    def _nav_selected(self, _box, row):
        if not row:
            return
        key = row.page_key
        self.stack.set_visible_child_name(key)
        if key in PAGE_PREVIEW_MODE:
            self.current_page = key
            self.tail_toggle.set_visible(key == "combined")
            self.preview.request(key, self.combined_tail)
        else:
            self.tail_toggle.set_visible(False)

    def _tail_toggled(self, button, key):
        if not button.get_active():
            return
        for child in self.tail_toggle:
            if child is not button:
                child.set_active(False)
        self.combined_tail = key
        self.preview.request(self.current_page, key)

    # ── Pages editor ──
    def _build_page_editor(self, page, family):
        page_key = f"{page}.{family}"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16,
                      margin_end=16)
        group = Adw.PreferencesGroup(
            title=f"{dict(TYPES + [('comfy_full', 'ComfyUI'), ('llama_full', 'Llama.cpp')]).get(page, page)} sections",
            description=("Tick to show, arrows to reorder. Graph sections grow "
                         "to fill freed space. Applies to portrait; landscape "
                         "layouts are fixed."))
        box.append(group)

        defaults = btn.PAGES[page_key]
        by_key = {e.key: e for e in defaults}
        cfg_order = (read_config().get("pages") or {}).get(page_key)
        if cfg_order is None:
            order = [(e.key, True) for e in defaults]
        else:
            vis = [k for k in cfg_order if k in by_key]
            order = [(k, True) for k in vis] + \
                    [(e.key, False) for e in defaults if e.key not in vis]

        listbox = Gtk.ListBox(css_classes=["boxed-list"],
                              selection_mode=Gtk.SelectionMode.NONE)
        group.add(listbox)
        state = {"order": order, "page_key": page_key, "listbox": listbox}
        self._rebuild_rows(state)
        return box

    def _rebuild_rows(self, state):
        listbox = state["listbox"]
        while (child := listbox.get_first_child()) is not None:
            listbox.remove(child)
        defaults = btn.PAGES[state["page_key"]]
        by_key = {e.key: e for e in defaults}
        for idx, (key, visible) in enumerate(state["order"]):
            e = by_key[key]
            row = Adw.ActionRow(title=e.label)
            sub = []
            if e.modes:
                sub.append(f"{'/'.join(sorted(e.modes))} tail")
            if e.flex:
                sub.append("grows to fill space")
            if sub:
                row.set_subtitle(" · ".join(sub))
            chk = Gtk.CheckButton(active=visible, valign=Gtk.Align.CENTER)
            chk.connect("toggled", self._row_toggled, state, idx)
            row.add_prefix(chk)
            up = Gtk.Button(icon_name="go-up-symbolic",
                            css_classes=["flat"], valign=Gtk.Align.CENTER)
            dn = Gtk.Button(icon_name="go-down-symbolic",
                            css_classes=["flat"], valign=Gtk.Align.CENTER)
            up.set_sensitive(idx > 0)
            dn.set_sensitive(idx < len(state["order"]) - 1)
            up.connect("clicked", self._row_move, state, idx, -1)
            dn.connect("clicked", self._row_move, state, idx, +1)
            row.add_suffix(up)
            row.add_suffix(dn)
            listbox.append(row)

    def _row_toggled(self, chk, state, idx):
        key, _ = state["order"][idx]
        state["order"][idx] = (key, chk.get_active())
        self._write_page(state)

    def _row_move(self, _btn, state, idx, delta):
        o = state["order"]
        j = idx + delta
        o[idx], o[j] = o[j], o[idx]
        self._rebuild_rows(state)
        self._write_page(state)

    def _write_page(self, state):
        visible = [k for k, v in state["order"] if v]
        self.queue_write({"pages": {state["page_key"]: visible}})

    # ── Appearance ──
    def _build_appearance(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16,
                      margin_end=16)
        cfg = read_config()

        g1 = Adw.PreferencesGroup(title="Theme")
        g1.add(combo_row("Theme", THEMES, cfg.get("theme", "default"),
                         self._theme_changed))
        box.append(g1)

        self.color_group = Adw.PreferencesGroup(
            title="Theme colours",
            description="Override this theme's key colours; needles, bars, "
                        "graphs and status ranges all follow.")
        self.color_rows = {}
        for slot, title in COLOR_SLOTS:
            row = Adw.ActionRow(title=title)
            dialog = Gtk.ColorDialog(with_alpha=False)
            btn_ = Gtk.ColorDialogButton(dialog=dialog,
                                         valign=Gtk.Align.CENTER)
            btn_.connect("notify::rgba", self._color_changed, slot)
            row.add_suffix(btn_)
            self.color_rows[slot] = btn_
            self.color_group.add(row)
        reset = Adw.ActionRow(title="Reset to theme defaults")
        rb = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER,
                        css_classes=["destructive-action"])
        rb.connect("clicked", self._colors_reset)
        reset.add_suffix(rb)
        self.color_group.add(reset)
        box.append(self.color_group)
        self._load_colors(cfg.get("theme", "default"))

        g3 = Adw.PreferencesGroup(
            title="Background",
            description="Each theme keeps two loops: one for idle, and one that "
                        "plays while inference is running.")
        slot = Adw.ComboRow(title="Loop",
                            subtitle="Which of the theme's two loops to preview "
                                     "and replace",
                            model=Gtk.StringList.new(["Idle", "Inference (fire)"]))
        slot.set_selected(0)
        slot.connect("notify::selected", self._bg_slot_changed)
        self.bg_slot = slot
        g3.add(slot)
        pick = Adw.ActionRow(title="Set background video",
                             subtitle="Transcoded for the panel automatically")
        pb = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        pb.connect("clicked", self._pick_background)
        pick.add_suffix(pb)
        g3.add(pick)
        r_op, _ = scale_row("Opacity", 0.0, 1.0, 0.05,
                            float(cfg.get("bg_opacity", 0.35)),
                            lambda v: self.queue_write({"bg_opacity": round(v, 2)}))
        g3.add(r_op)
        idle_op = float(cfg.get("bg_opacity", 0.35))
        r_oph, _ = scale_row("Opacity while inferring", 0.0, 1.0, 0.05,
                             float(cfg.get("bg_opacity_hot",
                                           min(idle_op + 0.10, 0.85))),
                             lambda v: self.queue_write(
                                 {"bg_opacity_hot": round(v, 2)}),
                             subtitle="The fire loop is scrimmed less so it "
                                      "reads as angry, not merely warm")
        g3.add(r_oph)
        r_fps, _ = scale_row("Frame rate", 6, 24, 1,
                             float(cfg.get("bg_fps", 12)),
                             lambda v: self.queue_write({"bg_fps": int(v)}),
                             subtitle="Higher = smoother, more RAM/CPU")
        g3.add(r_fps)
        box.append(g3)
        return box

    def _theme_changed(self, theme):
        self.queue_write({"theme": theme})
        self._load_colors(theme)
        # family may change -> rebuild page editors
        GLib.timeout_add(500, self._rebuild_editors)

    def _rebuild_editors(self):
        family = btn.theme_family(read_config().get("theme", "default"))
        for key in ("combined", "system", "comfy_full", "llama_full"):
            old = self.stack.get_child_by_name(key)
            if old:
                self.stack.remove(old)
            self.stack.add_named(self._build_page_editor(key, family), key)
        current = self.stack.get_visible_child_name()
        if current in ("combined", "system", "comfy_full", "llama_full"):
            self.stack.set_visible_child_name(current)
        return False

    def _load_colors(self, theme):
        self._loading_colors = True
        merged = {**btn.BASE_PALETTE, **btn.THEME_STYLE.get(theme, {})}
        user = (read_config().get("theme_colors") or {}).get(theme) or {}
        slot_map = dict(btn.USER_COLOR_SLOTS)
        for slot, _ in COLOR_SLOTS:
            val = user.get(slot) or merged[slot_map[slot]]
            self.color_rows[slot].set_rgba(list_to_rgba(val))
        self._loading_colors = False

    def _color_changed(self, button, _pspec, slot):
        if getattr(self, "_loading_colors", False):
            return
        theme = read_config().get("theme", "default")
        val = rgba_to_list(button.get_rgba())
        val[3] = 255
        self.queue_write({"theme_colors": {theme: {
            **((read_config().get("theme_colors") or {}).get(theme) or {}),
            slot: val}}})

    def _colors_reset(self, _btn):
        theme = read_config().get("theme", "default")
        cfg = read_config()
        tc = cfg.get("theme_colors") or {}
        tc.pop(theme, None)
        write_config({"theme_colors": tc})
        self._load_colors(theme)
        self.preview.request(self.current_page, self.combined_tail)
        self.toaster.add_toast(Adw.Toast(title=f"{theme} colours reset"))

    def _bg_slot_changed(self, row, _p):
        """Previewing the fire slot shows the fire background, so you can judge
        the loop you are about to replace."""
        self.preview.hot = bool(row.get_selected())
        self.preview.request(self.current_page, self.combined_tail)

    def _pick_background(self, _btn):
        dialog = Gtk.FileDialog(title="Choose a background video")
        f = Gtk.FileFilter()
        f.set_name("Videos")
        for pat in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.avi", "*.gif"):
            f.add_pattern(pat)
        dialog.set_default_filter(f)
        dialog.open(self, None, self._background_chosen)

    def _background_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        src = gfile.get_path()
        theme = read_config().get("theme", "default")
        # set-bg-video.sh writes backgrounds/<name>.mp4 for any name, so the fire
        # slot is just the theme name with the suffix the builder looks for.
        hot = bool(getattr(self, "bg_slot", None) and self.bg_slot.get_selected())
        slot_name = f"{theme}_fire" if hot else theme
        label = f"{theme} (fire)" if hot else theme
        self.toaster.add_toast(Adw.Toast(title=f"Transcoding for {label}…"))

        def run():
            r = subprocess.run(
                [os.path.join(REPO, "set-bg-video.sh"), src, "8",
                 str(int(read_config().get("bg_fps", 12))), slot_name],
                capture_output=True, text=True)
            msg = (f"Background installed for {label}" if r.returncode == 0
                   else "Transcode failed — see terminal")
            GLib.idle_add(lambda: self.toaster.add_toast(Adw.Toast(title=msg)))
            GLib.idle_add(lambda: self.preview.request(self.current_page,
                                                       self.combined_tail))
        threading.Thread(target=run, daemon=True).start()

    # ── Display ──
    def _build_display(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16,
                      margin_end=16)
        cfg = read_config()
        g = Adw.PreferencesGroup(title="Panel")
        g.add(combo_row("Dashboard type", TYPES, cfg.get("type", "combined"),
                        lambda v: self.queue_write({"type": v}),
                        subtitle="Also switchable from the tray"))
        g.add(combo_row("Orientation", ORIENTS, cfg.get("orient", "portrait"),
                        lambda v: self.queue_write({"orient": v}),
                        subtitle="Match the physical mounting"))
        row, scale = scale_row("Brightness", 0, 100, 1,
                               int(cfg.get("brightness", 100)),
                               self._brightness_changed)
        g.add(row)
        box.append(g)
        note = Adw.PreferencesGroup()
        lbl = Gtk.Label(
            label="Changes land on the panel in about a second.\n"
                  "Brightness drags apply instantly and persist on release.",
            css_classes=["dim-label"], xalign=0, wrap=True)
        note.add(lbl)
        box.append(note)
        return box

    def _brightness_changed(self, v):
        v = int(v)
        # instant, transient feedback while dragging (throttled)
        now = GLib.get_monotonic_time()
        if now - getattr(self, "_last_bright", 0) > 100_000:
            self._last_bright = now
            serial = getattr(self, "_lcd_serial", None)
            if serial is None:
                try:
                    devs = self.client.call("ListDevices").get("data") or []
                    serial = next((d["device_id"] for d in devs
                                   if d.get("has_lcd")), "")
                except Exception:
                    serial = ""
                self._lcd_serial = serial
            if serial:
                try:
                    self.client.call("SetLcdBrightness",
                                     {"device_id": serial, "brightness": v})
                except Exception:
                    pass
        # persisted via the collector on debounce flush
        self.queue_write({"brightness": v})


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.hirparak.LianliDash")

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    App().run()
