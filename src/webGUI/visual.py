#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor
import numpy as np
import fflib
from audio import SPEED_CANDIDATES


def frame_similarity(f1, f2):
    if f1 is None or f2 is None:
        return -1.0
    f1n = f1.ravel() - np.mean(f1)
    f2n = f2.ravel() - np.mean(f2)
    s1, s2 = np.linalg.norm(f1n), np.linalg.norm(f2n)
    if s1 < 1e-6 or s2 < 1e-6:
        return 0.0
    return float(np.dot(f1n, f2n) / (s1 * s2))


def _extract_frame_safe(path, t):
    try:
        return fflib.extract_frame(path, t)
    except Exception:
        return None


def visual_offset_score(v1_path, v2_path, offset, speed, dur1, dur2,
                        n_probes=6, cancel=None):
    margin = min(30.0, dur1 * 0.05)
    probes = [margin + (dur1 - 2 * margin) * k / (n_probes + 1)
              for k in range(1, n_probes + 1)]

    sims = []
    for t1 in probes:
        if cancel and hasattr(cancel, 'check'):
            cancel.check()
        t2 = (t1 - offset) / speed
        if t2 < 0 or t2 > dur2:
            continue
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1_fut = pool.submit(_extract_frame_safe, v1_path, t1)
            f2_fut = pool.submit(_extract_frame_safe, v2_path, t2)
            f1 = f1_fut.result()
            f2 = f2_fut.result()
        sim = frame_similarity(f1, f2)
        if sim >= 0:
            sims.append(sim)

    if not sims:
        return -1.0
    return float(np.median(sims))


def verify_offset_visual(v1_path, v2_path, coarse_offset, xcorr_speed,
                         alt_offsets, dur1, dur2,
                         progress_cb=None, cancel=None):
    if progress_cb:
        progress_cb("status", "Verifying alignment visually...")

    xcorr_score = visual_offset_score(
        v1_path, v2_path, coarse_offset, xcorr_speed, dur1, dur2,
        cancel=cancel)

    if xcorr_score > 0.7:
        return None

    candidates = [(coarse_offset, xcorr_speed, xcorr_score)]

    pending = []
    if abs(coarse_offset) > 0.5:
        pending.append((0.0, xcorr_speed))
    if alt_offsets:
        for alt_off, alt_spd, alt_corr in alt_offsets[:3]:
            pending.append((alt_off, alt_spd))
    if abs(xcorr_speed - 1.0) > 0.001:
        pending.append((0.0, 1.0))
    for sc in SPEED_CANDIDATES:
        if abs(sc - xcorr_speed) > 0.001:
            pending.append((0.0, sc))

    seen = {(round(coarse_offset, 2), round(xcorr_speed, 4))}
    unique_pending = []
    for off, spd in pending:
        key = (round(off, 2), round(spd, 4))
        if key not in seen:
            seen.add(key)
            unique_pending.append((off, spd))
        if len(unique_pending) >= 5:
            break

    for off, spd in unique_pending:
        if cancel and hasattr(cancel, 'check'):
            cancel.check()
        score = visual_offset_score(
            v1_path, v2_path, off, spd, dur1, dur2, cancel=cancel)
        candidates.append((off, spd, score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    best_off, best_spd, best_score = candidates[0]

    if (best_off, best_spd) == (coarse_offset, xcorr_speed):
        return None

    margin = best_score - xcorr_score
    if best_score > 0.4 and (xcorr_score < 0.2 or margin > 0.15):
        if progress_cb:
            progress_cb("status",
                        f"Visual correction: offset {coarse_offset:.2f} -> "
                        f"{best_off:.2f}, speed {xcorr_speed:.4f} -> "
                        f"{best_spd:.4f} (score {xcorr_score:.2f} -> "
                        f"{best_score:.2f})")
        return {"offset": best_off, "speed": best_spd, "score": best_score, "audio_score": xcorr_score}

    return None


def refine_boundary_visual(v1_path, v2_path, segments, speed,
                           format_timestamp=None, progress_cb=None,
                           cancel=None):
    if len(segments) < 2:
        return segments

    def _fmt(t):
        if format_timestamp:
            return format_timestamp(t)
        return f"{t:.1f}s"

    refined = list(segments)
    for si in range(len(segments) - 1):
        seg1 = segments[si]
        boundary = seg1["v1_end"]
        if boundary >= 1e9:
            continue
        off1 = seg1["offset"]

        lo = boundary - 30.0
        hi = boundary + 30.0
        if lo < 0:
            lo = 0.0

        if progress_cb:
            progress_cb("status",
                        f"Visual refine boundary {si+1} "
                        f"({_fmt(lo)}-{_fmt(hi)})...")

        for _ in range(20):
            if cancel:
                cancel.check()
            if hi - lo < 0.1:
                break
            mid = (lo + hi) / 2
            v2_t = (mid - off1) / speed
            if v2_t < 0:
                lo = mid
                continue
            f1 = _extract_frame_safe(v1_path, mid)
            f2 = _extract_frame_safe(v2_path, v2_t)
            sim = frame_similarity(f1, f2)
            if sim > 0.5:
                lo = mid
            else:
                hi = mid

        new_boundary = (lo + hi) / 2
        refined[si] = dict(refined[si], v1_end=new_boundary)
        refined[si + 1] = dict(refined[si + 1], v1_start=new_boundary)

    return refined
