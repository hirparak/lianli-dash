"""Drive the panel's 60-LED ring as a vertical VRAM gauge.

The ring is 60 individually addressable LEDs around the (portrait) panel; the
daemon's `SetRgbEffect` IPC flattens everything to one colour, so per-LED frames
go through its OpenRGB SDK server instead (enable `rgb.openrgb_server` in the
daemon config; port 6743). A diffuser blends neighbouring LEDs, which suits a
filling-bar rendering well.

Physical map — measured LED-by-LED with Kieran at the rig (2026-08-29; three
rounds of inference from partial observations all placed a corner wrong, so
every corner was probed individually):
    corner LEDs sit IN each corner: BL=13, BR=20, TR=43, TL=50
    left column  = 51..12 (index increases DOWNWARD), 22 LEDs
    bottom edge  = 14..19, 6 LEDs
    right column = 21..42 (index increases UPWARD), 22 LEDs
    top edge     = 44..49, 6 LEDs
    LED 0 is the 10th of the left column from the top (just above centre)
The columns are EQUAL (22 each) — earlier "left sits one higher" symptoms were
mis-assigned corner LEDs, not asymmetric edges.

Rendering: both columns fill bottom -> up with combined VRAM; the bottom edge
plus its two corner LEDs light as the bar's base whenever there is any fill;
the top edge plus its corners cap the bar at full. Colour is the caller's
business (white calm / red fire / full-ring alarm).
"""
import socket
import struct

MAGIC = b"ORGB"
LEDS = 60
COL = 22                              # LEDs per column = fill resolution
LEFT_BOTTOM = 12                      # left column runs 51..12 downward
RIGHT_BOTTOM = 21                     # right column runs 21..42 upward
BASE = (13, 14, 15, 16, 17, 18, 19, 20)   # bottom edge + both corner LEDs
CAP = (43, 44, 45, 46, 47, 48, 49, 50)    # top edge + both corner LEDs
EDGE = COL                            # legacy alias


class RingClient:
    """Just enough OpenRGB SDK protocol (v4) for UpdateLEDs on the ring."""

    def __init__(self, host="127.0.0.1", port=6743, name="lianli-dash-ring"):
        self.sock = socket.create_connection((host, port), timeout=3)
        self._send(0, 50, name.encode() + b"\x00")        # SET_CLIENT_NAME

    def _send(self, dev, pkt, payload=b""):
        self.sock.sendall(MAGIC + struct.pack("<III", dev, pkt, len(payload))
                          + payload)

    def update(self, colors):
        payload = struct.pack("<IH", 4 + 2 + 4 * len(colors), len(colors))
        for r, g, b in colors:
            payload += struct.pack("<BBBB", r, g, b, 0)
        self._send(0, 1050, payload)                       # UPDATE_LEDS

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def bar_frame(fill, rgb):
    """A 60-LED frame: vertical bar at `fill` (0..1) in colour `rgb`."""
    colors = [(0, 0, 0)] * LEDS
    fill = max(0.0, min(1.0, fill))
    steps = round(fill * COL)
    if steps > 0:
        for i in BASE:
            colors[i] = rgb
    for s in range(steps):
        colors[(LEFT_BOTTOM - s) % LEDS] = rgb
        colors[(RIGHT_BOTTOM + s) % LEDS] = rgb
    if steps >= COL:
        for i in CAP:
            colors[i] = rgb
    return colors


def full_frame(rgb):
    return [rgb] * LEDS
