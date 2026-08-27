# lianli-dash

A Linux system dashboard for the **Lian Li 8.8" Universal Screen** — the internal
480×1920 case panel — rendered locally and pushed to the panel over USB. No Windows,
no L-Connect, no virtual display.

Shows GPUs (util / VRAM / temp / power), CPU (util / temp / RAM), the water loop
(coolant, flow, pumps), per-radiator fan banks, and llama.cpp inference (loaded model +
live tokens/sec). Five themes, portrait or landscape, with animated video backgrounds
that switch to a "burning" variant while inference is running.

> Built against one specific rig (dual RTX PRO 6000, a custom water loop on Aquacomputer
> Octo controllers, an iCUE Link hub). The panel driving, template engine and theming are
> general; the sensor layer assumes that hardware in places. Fan-bank tables and radiator
> mappings below are that machine's — treat them as a worked example, not a spec.

**This needs a patched `lianli-daemon`** — see *Daemon patches* below. It will not work
against stock upstream.

## How the panel is driven

The screen is **`1cbe:a088`** — a vendor-specific USB device, 480×1920 native portrait, with
a separate ARGB frame at `0416:8050`. It has two modes:

- **LCD mode** (VID `0x1CBE`) — the daemon streams media to it. *This is what we use.*
- **Desktop mode** (VID `0x1A86`, CH340) — re-enumerates as a normal secondary display via evdi.

LCD mode turns out to be the better fit for a fixed dashboard: we render exactly the frame we
want and push it, with no compositor, browser, or virtual monitor in the path.

Frames go to [`lianli-daemon`](https://github.com/sgtaziz/lian-li-linux) over its IPC socket
(`/tmp/lianli-daemon.sock`, newline-delimited JSON). We build **only the daemon crate** from
source — no npm, no GUI, no system package:

```bash
cargo build --release -p lianli-daemon     # needs nasm for turbojpeg's SIMD
```

## Gotchas (all learned the hard way)

- **The daemon only re-reads the image when the media CONFIG changes.** Re-sending the same
  path is a silent no-op — no log line, panel keeps the old frame. Every push therefore uses a
  **unique filename**. (An alternating A/B flag isn't enough: a fresh process restarts the flag
  and reuses the previous name.)
- **`device_id` must be `serial:<serial>`**, the daemon's `LcdConfig::device_id()` format.
  Passing the bare serial doesn't match, so `SetLcdMedia` **appends** another entry instead of
  replacing — duplicate entries then fight over the panel and the stale one wins, which looks
  exactly like a frozen display.
- **An LCD entry needs `serial` (or `index`)**, or it binds to nothing (`LCD[unknown]`).
- **Don't mix `index` and `serial`** between calls — they produce different `device_id()`s and
  so create duplicates.
- **Any config change triggers a full daemon reload**, which re-attaches and re-initialises the
  panel (`Initializing LCD` per frame) — visible as a **flicker**. See *Status* below.
- Under `sudo`, `~` is `/root`: frame paths must be absolute.

## Data sources

| Metric | Source |
|---|---|
| GPU util / VRAM / temp / power | `nvidia-smi` |
| CPU util, RAM | `/proc/stat`, `/proc/meminfo` |
| CPU temp | hwmon (`k10temp`) |
| Coolant, flow, pump, fan RPM | CoolerControl API (`localhost:11987`) |
| Model + tokens/sec | llama.cpp router (`localhost:11434`) |

**Flow** is reported by the High Flow Next in **decilitres/hour** (hwmon labels it
`Flow [dL/h]`) — divide by 10 for L/h. **Pump** is shown as duty % with RPM beneath.

**Live tokens/sec** is computed from the *delta* of `tokens_predicted_total` /
`tokens_predicted_seconds_total`. The `predicted_tokens_seconds` gauge is only the last
finished request, so it sits at a stale value between generations. Requires `metrics = true`
in the `[*]` section of `models.ini`.

## Fan banks

The FANS panel shows one row per **bank** — a group of channels you care about together,
usually "the fans on this radiator". Which channels belong to which bank is per-rig wiring,
so it lives in `config.json`, not in the source.

With nothing configured, banks are **discovered automatically**: one per device that has
spinning fans, ranked so the busiest devices win the four available rows. That means a fresh
clone shows a populated panel immediately, rather than an empty box.

To lay it out yourself, see what CoolerControl exposes:

```bash
python3 discover.py            # every device, channel and current RPM
python3 discover.py --write    # seed config.json with one bank per device, then edit
```

Then describe the grouping:

```jsonc
"fan_columns": ["INTAKE", "EXHAUST"],   // the two column headings
"fan_banks": [
  { "label": "TOP RAD",
    "left":  { "device": "a1b2c3d4", "channels": ["port2", "port3"] },
    "right": { "device": "e5f6a7b8", "channels": null } },   // null = all its fans
  { "label": "CASE",
    "left":  { "device": "a1b2c3d4", "channels": ["port14"] },
    "right": null }                                          // one column only
]
```

`device` matches a CoolerControl **UID prefix** (the 8 characters `discover.py` prints), or
falls back to a case-insensitive match on the device *name* — `"octo"` is fine with one Octo,
but use UIDs once you have two, because names collide. Four banks fit; extras are ignored.

Working out which physical fans a channel drives is much easier if you make them move:
`ramptest.py` ramps one device at a time and restores the previous settings afterwards.

### Things worth knowing about fan sensors

These cost real debugging time and apply to any rig:

- **Bind sensors by device name, not by bare channel name.** Several devices expose a `temp1`
  and a `fan1`; an early version of this dashboard happily read a 51 °C VRM sensor as "coolant".
- **Aquacomputer idles unpopulated channels at 100% duty / 0 rpm.** An empty header looks like
  a failed fan. `discover.py` marks stopped channels for this reason.
- **The Octo's `fan9` is the flow-meter header**, not a fan, and the D5 Next's `fan1` is the
  pump. Both are excluded from bank discovery.
- **Flow is reported in decilitres/hour** (hwmon labels it `Flow [dL/h]`), so a raw 1255 is
  125.5 L/h.

## Orientation

Composed in a logical orientation then rotated to the panel's native 480×1920, so remounting
the screen is a config change, not a code change:

```
DASH_ORIENTATION=portrait|landscape    DASH_ROTATE=270
```

## Install

```bash
./install.sh [/path/to/lianli-daemon]
sudo systemctl enable --now lianli-daemon lianli-dash
```

`install.sh` substitutes this checkout's location and your XDG directories into the
systemd units. Both services run as root — the daemon needs raw USB and its IPC socket is
root-owned — which is exactly why the units carry `DASH_CONFIG_DIR`/`DASH_DATA_DIR`
explicitly: under root, `~` is `/root`, so XDG would otherwise resolve to the wrong home.

### Configuration

`$XDG_CONFIG_HOME/lianli-dash/config.json` (written by the settings app or by hand):

| Key | Meaning |
|---|---|
| `type` | `combined` / `system` / `comfy` / `llama` / `video` / `off` |
| `theme` | `default` / `cardash` / `cyberpunk` / `steampunk` / `gits` |
| `orient` | `portrait` / `portrait-flipped` / `landscape` / `landscape-flipped` |
| `brightness` | 0–100 |
| `bg_opacity`, `bg_opacity_hot` | background scrim, idle and while inferring |
| `lcd_serial` | usually unnecessary — auto-detected from the daemon |

The panel serial is **auto-detected** (the first device reporting `has_lcd`), so nothing
machine-specific is baked into the source. Override with `DASH_LCD_SERIAL` or the
`lcd_serial` key if you run more than one panel.

Everything else is environment-overridable: `DASH_TITLE` (defaults to your hostname),
`DASH_CC_USER`/`DASH_CC_PASS` (CoolerControl login, defaults to CoolerControl's own),
`DASH_LLAMA`, `DASH_ORIENTATION`, `DASH_INTERVAL`.

## Daemon patches

The dashboard drives [sgtaziz/lian-li-linux](https://github.com/sgtaziz/lian-li-linux),
built as the daemon crate only (`cargo build --release -p lianli-daemon`; needs `nasm`).
Two local patches are required, and a clean re-clone reintroduces both bugs:

1. **Video/Image widgets with `opacity < 1` render invisible.** `blit_with_opacity`
   blends RGB but never writes destination alpha, and widgets draw onto a transparent
   scratch buffer — so the frame overlay discards everything. Only `opacity: 1.0` worked,
   via the `fast_overlay` shortcut. Fixed with proper src-over compositing.
2. **Asset hot-swap into a live pipeline.** Without it every template change tears down
   and restarts the h264 stream (~3.5s of USB re-enumeration, and a race that can leave
   the panel frozen). With it, a swap that keeps canvas/rotation/fps reuses the running
   pipeline — the background switch lands in ~1.3s.

## Template gotchas (cost several rounds — none of this is documented upstream)

1. **Widget `x`/`y` are the widget's CENTRE, not its top-left corner.** Anchoring a full-bleed
   image at `0,0` puts its bottom-right corner at the middle of the panel. Confirmed by the
   built-in `neon-us88`, where a value and the gauge it sits inside share identical `x`/`y`.
2. **`template.rotated` has NO runtime effect** — it appears only in the GUI editor/browser
   code, never in the renderer. Don't trust it to rotate anything.
3. **Only the IMAGE media path rotates.** `apply_orientation()` is called from `image.rs`
   alone; the custom/template path renders at canvas size and sends it **unrotated**. On the
   template path `orientation` therefore only *reshapes the canvas* (`render_dimensions` swaps
   W/H at 90/270) — set it to 0 and compose in the panel's native 480×1920.
4. The renderer letterboxes: `scale = min(canvas_w/base_w, canvas_h/base_h)`. A 1920×480 base
   in a 480×1920 canvas scales to 0.25 — a tiny landscape strip.
5. `fit` accepts `stretch` / `contain` / `cover` (not `fill`), and `value_text` parses its
   sensor as a float, so it cannot display a string.

## Backgrounds

Each theme has **two** loops under `~/.local/share/lianli-dash/backgrounds/`:

| File | Plays when |
|---|---|
| `<theme>.mp4` | idle |
| `<theme>_fire.mp4` | inference is running (llama.cpp decoding **or** a ComfyUI job) |

The fire loop is the *same scene ignited*, not a different wallpaper, so the switch reads as the
dashboard catching fire rather than as a wallpaper change. `collect.py` sets `hot` when either
engine is busy, holds it `HOT_HOLD` (45 s) past the last busy tick so the gaps between chat turns
don't strobe the panel, and folds it into the template reinstall key — so the swap rides the
existing ~1.3 s asset hot-swap. A theme with no `_fire.mp4` on disk simply keeps its idle loop.

`default` is the no-theme look and stays abstract (an ember gradient) so the stats keep reading.

The fire loop is scrimmed less hard than the idle one — `bg_opacity_hot`, default `bg_opacity + 0.10`
(0.45 vs 0.35). At the idle opacity the burn reads as merely warm; 0.55 starts to swallow the dim
section headers. The boost applies only when a `_fire.mp4` actually exists, so a theme without one
renders identically hot or not.

### Making the loops

Loops are generated from the theme still (`themebg_*.png`, 512×2048) with local models:

- **Fire still** — Qwen-Image-Edit 2511 (lightning, 4 steps, ~5 s/image) recolours the scene.
- **Motion** — Wan 2.2 i2v 14B + the lightx2v 4-step CFG-distill LoRA.

**Don't pin `first_frame` and `last_frame` to the same still to close the loop.** It looks like
the obvious trick and it fails: telling an i2v model the scene is unchanged over the clip makes
it render almost nothing (measured **0.2** grey-levels/frame vs **2.4** for the old sine-zoom
loops), *and* it still doesn't land back on the start frame — the wrap measured 8.3, a visible
pop. Generate free-running instead and close the loop afterwards:

```bash
./mkloop.py in.mp4 ~/.local/share/lianli-dash/backgrounds/gits_fire.mp4 --mode xfade --overlap 24
```

`mkloop.py --mode xfade` crossfades the tail back onto the head (`L = N - overlap` frames), which
is invisible on flowing content — rain, embers, steam, sparks — because two different moments of
falling rain blend without ghosting. Rigid motion (a rotating gear tooth) smears, so use a smaller
overlap there.

**H3 is the exception: it honours pinned endpoints**, so its loops normally close with `--mode drop`
(just discard the duplicated last frame). But it can still collapse a clip to a near-still on some
scene/seed combinations — `cyberpunk_fire` came back at motion 0.52 with only 5% of pixels moving,
frozen, while the very same scene's idle loop moved fine. The fix that worked, in order:

1. Lead the prompt with continuous-motion verbs ("nothing in the frame is still") rather than scene
   description, and change the seed.
2. Raise `MiniMaxH3SigmaShift.shift_video` (5.0 → 7.0). This restores motion **but breaks the endpoint
   pin** — the clip drifts too far to land back home (seam 28.4, a hard visible jump).
3. So close *that* clip with `--mode xfade --overlap 16` instead of `drop`: motion 6.5 with a seam of
   4.5, i.e. below its own per-frame motion. High shift and xfade go together.

`qa.py` (in the scratch pipeline) is the gate. It scores mean per-frame motion, the wrap seam, and
`active` — the share of pixels that actually vary — because the mean alone is too blunt: `steampunk`
idle scores just 1.25 since most of the frame is static dark brass, yet its gauge needles sweep and
its steam billows (21% active). A loop fails only when both the mean and the active area are dead.

The GTK settings app's **Appearance → Background** section picks which of the two loops you are
replacing, and previews it.

## Status

- ✅ Panel driven, live metrics, correct units, verified fan-bank mapping, services on boot.
- ✅ **Flicker fixed.** The dashboard is served as `MediaType::Custom` + a template, which the
  daemon renders on its own autonomous thread — no config write per frame, so no reload, no
  re-init, no flicker. Verified 0 re-inits in steady state.
- The template is a single full-bleed **image widget** showing the frame drawn by `dash.py`
  (via `collect.py`), because the template path can't rotate and `value_text` can't render
  strings. This keeps the richer PIL layout while getting flicker-free updates.
- Next step (optional): port the layout to native widgets (`RadialGauge`, `Sparkline`, …) now
  that the centre-anchored coordinate system is understood — gains real gauges/graphs.

## Files

- `dash.py` — metrics collection + PIL renderer + IPC push loop
- `mkloop.py` — closes a generated i2v clip into a seamless panel loop (crossfade or drop)
- `set-bg-video.sh` — transcodes any clip into a loop slot (`<theme>` or `<theme>_fire`)
- `ramptest.py` — ramps one Octo at a time to identify which drives which radiator (snapshots
  and restores the fan profiles)
- `systemd/` — units for the daemon and the dashboard

## Licence

**MIT** — see [`LICENSE`](LICENSE).

The daemon this drives, [sgtaziz/lian-li-linux](https://github.com/sgtaziz/lian-li-linux),
is a separate program with its own licence; this dashboard talks to it over its IPC socket
and contains none of its code.
