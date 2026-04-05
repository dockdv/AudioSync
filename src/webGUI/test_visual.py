#!/usr/bin/env python3
"""Standalone test for refine_offset_visual with f1/f2.mkv."""

import os
import sys
import time

_base = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("FFMPEG_PATH", os.path.join(_base, ".local", "tools", "ffmpeg"))
os.environ.setdefault("FFPROBE_PATH", os.path.join(_base, ".local", "tools", "ffprobe"))

import fflib
from visual import refine_offset_visual

V1 = "../../.local/samples/f1.mkv"
V2 = "../../.local/samples/f2.mkv"
COARSE_OFFSET = 2.210  # wrong offset from audio-only alignment
SPEED = 1.0

def progress(kind, msg):
    print(f"  [{kind}] {msg}")

def main():
    print("Probing V1...")
    dur1 = fflib.get_duration(V1)
    print(f"  V1 duration: {dur1:.1f}s")

    print("Probing V2...")
    dur2 = fflib.get_duration(V2)
    print(f"  V2 duration: {dur2:.1f}s")

    print(f"\nRunning refine_offset_visual (coarse offset={COARSE_OFFSET:.3f}s)...")
    t0 = time.time()
    result = refine_offset_visual(
        V1, V2, COARSE_OFFSET, SPEED, dur1, dur2,
        progress_cb=progress)
    elapsed = time.time() - t0

    print(f"\nDone in {elapsed:.1f}s")
    if result is not None:
        print(f"Refined offset: {result:.3f}s (was {COARSE_OFFSET:.3f}s)")
    else:
        print("No refinement (returned None)")

if __name__ == "__main__":
    main()
