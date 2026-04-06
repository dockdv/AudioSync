#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor
import numpy as np
import fflib


def _dct2(block):
    """Separable Type-II 2D DCT via numpy."""
    n = block.shape[0]
    ns = np.arange(n)
    ks = np.arange(n)
    basis = np.cos(np.pi * (2 * ns[:, None] + 1) * ks[None, :] / (2 * n))
    return basis.T @ block @ basis


def _phash(frame, hash_size=8, dct_size=32):
    h, w = frame.shape
    rh, rw = h // dct_size, w // dct_size
    if rh >= 1 and rw >= 1:
        cropped = frame[:rh * dct_size, :rw * dct_size]
        resized = cropped.reshape(dct_size, rh, dct_size, rw).mean(axis=(1, 3))
    else:
        xs = np.linspace(0, w - 1, dct_size).astype(int)
        ys = np.linspace(0, h - 1, dct_size).astype(int)
        resized = frame[np.ix_(ys, xs)]
    dct = _dct2(resized.astype(np.float64))
    low = dct[:hash_size, :hash_size].ravel()
    med = np.median(low[1:])
    return low > med


def frame_similarity(f1, f2):
    if f1 is None or f2 is None:
        return -1.0
    h1 = _phash(f1)
    h2 = _phash(f2)
    hamming = np.count_nonzero(h1 != h2)
    return 1.0 - hamming / len(h1)


def _extract_frame_safe(path, t):
    try:
        return fflib.extract_frame(path, t)
    except Exception:
        return None


def _compare_at(v1_path, v2_path, t1, t2):
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1_fut = pool.submit(_extract_frame_safe, v1_path, t1)
        f2_fut = pool.submit(_extract_frame_safe, v2_path, t2)
        return frame_similarity(f1_fut.result(), f2_fut.result())


def validate_segments_visual(v1_path, v2_path, segments, primary_offset,
                             speed, dur1, dur2, cancel=None):
    if len(segments) < 2:
        return True
    for si in range(len(segments) - 1):
        boundary = segments[si]["v1_end"]
        if boundary >= 1e9:
            continue
        probes_before = [boundary - d for d in [30, 15, 5]
                         if boundary - d > 0]
        probes_after = [boundary + d for d in [5, 15, 30]
                        if boundary + d < dur1]
        match_before, match_after = 0, 0
        total_before, total_after = 0, 0
        for t1 in probes_before:
            if cancel and hasattr(cancel, 'check'):
                cancel.check()
            t2 = (t1 - primary_offset) / speed
            if t2 < 0 or t2 > dur2:
                continue
            sim = _compare_at(v1_path, v2_path, t1, t2)
            total_before += 1
            if sim > 0.5:
                match_before += 1
        for t1 in probes_after:
            if cancel and hasattr(cancel, 'check'):
                cancel.check()
            t2 = (t1 - primary_offset) / speed
            if t2 < 0 or t2 > dur2:
                continue
            sim = _compare_at(v1_path, v2_path, t1, t2)
            total_after += 1
            if sim > 0.5:
                match_after += 1
        before_ok = total_before == 0 or match_before / total_before > 0.5
        after_ok = total_after == 0 or match_after / total_after > 0.5
        if not (before_ok and after_ok):
            return False
    return True


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
            sim = _compare_at(v1_path, v2_path, mid, v2_t)
            if sim > 0.5:
                lo = mid
            else:
                hi = mid

        new_boundary = (lo + hi) / 2
        refined[si] = dict(refined[si], v1_end=new_boundary)
        refined[si + 1] = dict(refined[si + 1], v1_start=new_boundary)

    return refined


def _is_hard_cut(path, kf_time, prev_time, w, h, mse_threshold=500):
    """Check if a keyframe is a hard cut by comparing to previous frame.

    Returns (is_cut, keyframe_frame, mse).
    Uses MSE (mean squared error) for reliable cut detection.
    """
    frame_kf = fflib.extract_frame_full(path, kf_time, w, h)
    frame_prev = fflib.extract_frame_full(path, prev_time, w, h)

    if frame_kf is None or frame_prev is None:
        return False, None, -1.0
    mse = float(np.mean((frame_kf - frame_prev) ** 2))
    return mse > mse_threshold, frame_kf, mse


def _find_hard_cut_from(keyframes, idx, path, w, h, frame_interval,
                        cancel=None):
    """Walk keyframes starting at idx until a hard cut is found.

    Returns (keyframe_time, keyframe_frame) or (None, None).
    """
    for i in range(idx, min(idx + 50, len(keyframes))):
        if cancel and hasattr(cancel, 'check'):
            cancel.check()
        kf_time = keyframes[i]
        prev_time = max(0.0, kf_time - frame_interval)
        is_cut, frame_kf, sim = _is_hard_cut(path, kf_time, prev_time, w, h)
        if is_cut:
            return kf_time, frame_kf
    return None, None


def _crop_letterbox(frame, frame_ar, target_ar):
    """Crop letterbox/pillarbox bars to match the wider aspect ratio."""
    if frame_ar >= target_ar:
        return frame
    h, w = frame.shape
    new_h = int(w / target_ar)
    margin = (h - new_h) // 2
    return frame[margin:margin + new_h, :]


def refine_offset_visual(v1_path, v2_path, offset, speed, dur1, dur2,
                         cancel=None, progress_cb=None):
    """Refine offset by matching keyframe-based hard cuts between V1/V2.

    Returns the refined offset, or None if insufficient cuts matched.
    """
    margin = max(60.0, dur1 * 0.05)
    usable = dur1 - 2 * margin
    if usable < 30:
        return None

    if progress_cb:
        progress_cb("status", "Visual fine-tune: finding V1 hard cuts...")

    # Get V1 video resolution
    v1_w, v1_h = fflib.get_video_resolution(v1_path)
    if not v1_w or not v1_h:
        return None

    # Define 10 equally spaced locations
    n_locations = 10
    locations = [margin + usable * k / (n_locations + 1)
                 for k in range(1, n_locations + 1)]

    # Each location queries its own keyframe range
    search_len = 60.0  # search up to 60s from each location

    def _find_v1_cut(loc):
        keyframes = fflib.get_keyframe_timestamps(
            v1_path, loc, loc + search_len)
        if not keyframes:
            return None
        frame_interval = 1.0 / 24.0
        if len(keyframes) >= 2:
            gaps = [keyframes[i+1] - keyframes[i]
                    for i in range(min(20, len(keyframes) - 1))]
            pos_gaps = [g for g in gaps if g > 0]
            if pos_gaps:
                frame_interval = min(frame_interval, min(pos_gaps))
        return _find_hard_cut_from(keyframes, 0, v1_path,
                                   v1_w, v1_h, frame_interval,
                                   cancel=cancel)

    # Get V2 resolution
    v2_w, v2_h = fflib.get_video_resolution(v2_path)
    if not v2_w or not v2_h:
        return None

    # Compute aspect ratios for letterbox cropping
    v1_ar = v1_w / v1_h
    v2_ar = v2_w / v2_h
    wider_ar = max(v1_ar, v2_ar)

    def _match_in_v2(v1_time, v1_frame):
        expected_v2 = (v1_time - offset) / speed
        t2_start = max(0.0, expected_v2 - 10.0)
        t2_end = min(dur2, expected_v2 + 10.0)
        v2_keyframes = fflib.get_keyframe_timestamps(v2_path, t2_start, t2_end)
        if not v2_keyframes:
            if progress_cb:
                progress_cb("status",
                            f"Visual fine-tune: V2 no keyframes "
                            f"at {expected_v2:.1f}s")
            return None

        v2_frame_interval = 1.0 / 24.0
        if len(v2_keyframes) >= 2:
            gaps = [v2_keyframes[i+1] - v2_keyframes[i]
                    for i in range(len(v2_keyframes) - 1)]
            if gaps:
                v2_frame_interval = min(v2_frame_interval,
                                        min(g for g in gaps if g > 0))

        best_sim = -1.0
        best_kf = None
        n_cuts = 0
        for kf_time in v2_keyframes:
            if cancel and hasattr(cancel, 'check'):
                cancel.check()
            prev_time = max(0.0, kf_time - v2_frame_interval)
            is_cut, v2_frame, sim = _is_hard_cut(
                v2_path, kf_time, prev_time, v2_w, v2_h)
            if not is_cut:
                continue
            n_cuts += 1
            v1_crop = _crop_letterbox(v1_frame, v1_ar, wider_ar)
            v2_crop = _crop_letterbox(v2_frame, v2_ar, wider_ar)
            match_sim = frame_similarity(v1_crop, v2_crop)
            if match_sim > best_sim:
                best_sim = match_sim
                best_kf = kf_time
            if match_sim > 0.8:
                if progress_cb:
                    progress_cb("status",
                                f"Visual fine-tune: V1 {v1_time:.1f}s "
                                f"\u2194 V2 {kf_time:.1f}s "
                                f"p={match_sim:.3f} \u2713")
                return v1_time - kf_time
        if progress_cb:
            if n_cuts == 0:
                progress_cb("status",
                            f"Visual fine-tune: V1 {v1_time:.1f}s "
                            f"\u2194 V2 no cuts found in "
                            f"{len(v2_keyframes)} keyframes \u2717")
            else:
                progress_cb("status",
                            f"Visual fine-tune: V1 {v1_time:.1f}s "
                            f"\u2194 V2 best={best_kf:.1f}s "
                            f"p={best_sim:.3f} \u2717")
        return None

    # Interleaved: find V1 cut → match in V2 → next location
    # Require 3 consecutive cuts with offsets agreeing within ±2 frames
    FRAME_TOL = 0.083  # ±2 frames at 24fps
    consec_offsets = []
    for loc in locations:
        if cancel and hasattr(cancel, 'check'):
            cancel.check()
        result = _find_v1_cut(loc)
        if not result or result[0] is None:
            consec_offsets = []
            continue
        v1_time, v1_frame = result
        if progress_cb:
            progress_cb("status",
                        f"Visual fine-tune: matching V1 cut at "
                        f"{v1_time:.1f}s in V2...")
        matched = _match_in_v2(v1_time, v1_frame)
        if matched is not None:
            if consec_offsets and abs(matched - consec_offsets[0]) > FRAME_TOL:
                consec_offsets = []
            consec_offsets.append(matched)
            if len(consec_offsets) >= 3:
                break
        else:
            consec_offsets = []

    if len(consec_offsets) < 3:
        if progress_cb:
            progress_cb("status",
                        f"Visual fine-tune: only {len(consec_offsets)} "
                        f"consecutive cuts matched, keeping coarse offset")
        return None

    refined = float(np.median(consec_offsets))

    if abs(refined) > 5.0:
        if progress_cb:
            progress_cb("status",
                        f"Visual fine-tune: |{refined:.3f}s| > 5.0s, "
                        f"discarding")
        return None

    if progress_cb:
        progress_cb("status",
                    f"Visual fine-tune: {len(consec_offsets)} cuts matched, "
                    f"offset {offset:.3f}s -> {refined:.3f}s")

    return refined
