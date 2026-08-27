#!/usr/bin/env bash
# Install/replace the dashboard's background video loop.
#
# Transcodes any input clip to what the panel + daemon actually want:
#   480x1920 portrait (cover-crop), capped length + fps.
# The daemon pre-decodes the WHOLE loop to RGBA in RAM (3.5MiB/frame at
# full-bleed), so the caps are what keep memory sane: 8s @ 12fps ≈ 340MiB.
# The collector notices the new file mtime and reinstalls the template
# within a tick — no service restart needed. Remove the loop with:
#   rm ~/.local/share/lianli-dash/background.mp4
#
# usage: ./set-bg-video.sh <input-video> [seconds] [fps] [theme]
# theme "default" (or omitted) -> the classic background.mp4; any other theme
# name -> ~/.local/share/lianli-dash/backgrounds/<theme>.mp4
set -euo pipefail
in=${1:?usage: set-bg-video.sh <input-video> [seconds] [fps] [theme]}
secs=${2:-8}
fps=${3:-12}
theme=${4:-default}
if [ "$theme" = "default" ]; then
  out="${DASH_BG_VIDEO:-$HOME/.local/share/lianli-dash/background.mp4}"
else
  out="$HOME/.local/share/lianli-dash/backgrounds/$theme.mp4"
fi
mkdir -p "$(dirname "$out")"
ffmpeg -y -i "$in" -t "$secs" -an \
  -vf "scale=480:1920:force_original_aspect_ratio=increase,crop=480:1920,fps=$fps" \
  -c:v libx264 -preset slow -crf 22 -pix_fmt yuv420p "$out.tmp.mp4"
mv "$out.tmp.mp4" "$out"
echo "installed: $out ($(du -h "$out" | cut -f1), ${secs}s @ ${fps}fps)"
echo "RAM cost when loaded: ~$((secs * fps * 36 / 10)) MiB"
