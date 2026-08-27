"""Where the dashboard keeps its config and assets.

Everything is XDG-based so the dashboard is not tied to one user's home
directory. Each location can also be overridden by an environment variable,
which is what the systemd units use: the collector runs as root (the daemon's
IPC socket is root-owned), and under root "~" is /root — so the units point
DASH_CONFIG and DASH_DATA at the desktop user's directories explicitly.

Paths are always absolute: the daemon reads asset paths out of the template we
hand it, and it does not share our working directory.
"""
import os

APP = "lianli-dash"


def _xdg(var: str, fallback: str) -> str:
    """XDG spec: the variable wins only if it is set AND absolute."""
    v = os.environ.get(var, "")
    if not os.path.isabs(v):
        v = os.path.expanduser(fallback)
    return v


CONFIG_DIR = os.environ.get("DASH_CONFIG_DIR") or os.path.join(
    _xdg("XDG_CONFIG_HOME", "~/.config"), APP)
DATA_DIR = os.environ.get("DASH_DATA_DIR") or os.path.join(
    _xdg("XDG_DATA_HOME", "~/.local/share"), APP)

CONFIG_FILE = os.environ.get("DASH_CONFIG") or os.path.join(CONFIG_DIR, "config.json")

# Background loops: <DATA_DIR>/background.mp4 is the "default" theme's loop and
# keeps its historical name; per-theme loops live in <DATA_DIR>/backgrounds/.
BG_VIDEO = os.environ.get("DASH_BG_VIDEO") or os.path.join(DATA_DIR, "background.mp4")
BG_DIR = os.environ.get("DASH_BG_DIR") or os.path.join(DATA_DIR, "backgrounds")

# The rendered frame for the legacy PIL image path.
FRAME = os.environ.get("DASH_FRAME") or os.path.join(DATA_DIR, "frame.png")


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, BG_DIR):
        os.makedirs(d, exist_ok=True)
