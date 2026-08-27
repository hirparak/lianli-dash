#!/usr/bin/env python3
"""Turn a generated i2v clip into a seamless panel background loop.

Two ways to close a loop, and the right one depends on how the clip was made:

  --mode drop      The clip was generated with first_frame == last_frame, so its
                   final frame duplicates frame 0. Drop it and the loop wraps.
                   Cheap, but pinning both ends makes i2v models render almost no
                   motion (measured 0.2 grey-levels/frame vs 2.4 for the old
                   sine-zoom loops) — which is the "it just pulses" problem.

  --mode xfade     The clip was generated free-running, so it actually moves but
                   never returns home. Crossfade its tail back onto its head:
                   with N source frames and an overlap of K, the loop is
                   L = N - K frames and

                       out[i] = a*src[i] + (1-a)*src[i+L]   for i < K, a = i/(K-1)
                       out[i] = src[i]                      for i >= K

                   At i=0, a=0 so out[0] == src[L], which follows src[L-1] — the
                   frame the loop just came from. At i=K-1, a=1 so out[K-1] ==
                   src[K-1], which runs straight into src[K]. Both joins are
                   continuous, so the wrap is invisible. Works because the motion
                   here is flowing/particulate (rain, embers, steam, sparks): two
                   different moments of falling rain blend without ghosting. It
                   would smear on rigid motion like a rotating gear tooth, so
                   steampunk gets a smaller overlap.

Output is what the daemon wants: 480x1920 cover-crop, constant fps, h264 yuv420p.
The daemon pre-decodes the entire loop to RGBA (3.5MiB/frame), so frame count is
the RAM bill and is reported.

usage: mkloop.py <in.mp4> <out.mp4> [--mode drop|xfade] [--fps 12] [--overlap 24]
"""
import argparse
import os
import subprocess
import sys

import numpy as np

W, H = 480, 1920


def decode(path):
    """Decode to raw RGB24 at panel size, cover-cropped."""
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}")
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, check=True)
    a = np.frombuffer(p.stdout, dtype=np.uint8)
    n = a.size // (W * H * 3)
    return a[: n * W * H * 3].reshape(n, H, W, 3)


def encode(frames, path, fps):
    tmp = path + ".tmp.mp4"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "slow", "-crf", "20",
         "-pix_fmt", "yuv420p", tmp],
        stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.astype(np.uint8).tobytes())
    p.stdin.close()
    if p.wait() != 0:
        sys.exit("ffmpeg encode failed")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mode", choices=["drop", "xfade"], default="xfade")
    ap.add_argument("--fps", type=float, default=12.0)
    ap.add_argument("--overlap", type=int, default=24,
                    help="xfade only: frames of tail blended back onto the head")
    a = ap.parse_args()

    src = decode(a.src).astype(np.float32)
    n = len(src)

    if a.mode == "drop":
        out = src[:-1] if n > 2 else src
    else:
        k = max(2, min(a.overlap, n // 2))
        L = n - k
        out = src[:L].copy()
        ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None, None, None]
        out[:k] = ramp * src[:k] + (1.0 - ramp) * src[L:L + k]

    encode(np.clip(out, 0, 255), a.dst, a.fps)
    print(f"{os.path.basename(a.dst)}: {len(out)} frames @ {a.fps}fps = "
          f"{len(out)/a.fps:.1f}s, {os.path.getsize(a.dst)/1e6:.1f}MB, "
          f"~{len(out)*36//10}MiB decoded")


if __name__ == "__main__":
    main()
