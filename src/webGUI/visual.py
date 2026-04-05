#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor
import numpy as np
import fflib
from audio import SPEED_CANDIDATES


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
        sim = _compare_at(v1_path, v2_path, t1, t2)
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

    def _match_in_v2(v1_time, v1_frame):
        expected_v2 = (v1_time - offset) / speed
        t2_start = max(0.0, expected_v2 - 10.0)
        t2_end = min(dur2, expected_v2 + 10.0)
        v2_keyframes = fflib.get_keyframe_timestamps(v2_path, t2_start, t2_end)
        if not v2_keyframes:
            return None

        v2_frame_interval = 1.0 / 24.0
        if len(v2_keyframes) >= 2:
            gaps = [v2_keyframes[i+1] - v2_keyframes[i]
                    for i in range(len(v2_keyframes) - 1)]
            if gaps:
                v2_frame_interval = min(v2_frame_interval,
                                        min(g for g in gaps if g > 0))

        for kf_time in v2_keyframes:
            if cancel and hasattr(cancel, 'check'):
                cancel.check()
            prev_time = max(0.0, kf_time - v2_frame_interval)
            is_cut, v2_frame, sim = _is_hard_cut(
                v2_path, kf_time, prev_time, v2_w, v2_h)
            if not is_cut:
                continue
            match_sim = frame_similarity(v1_frame, v2_frame)
            if match_sim > 0.8:
                return v1_time - kf_time
        return None

    # Interleaved: find V1 cut → match in V2 → next location
    matched_offsets = []
    consecutive = 0
    for loc in locations:
        if cancel and hasattr(cancel, 'check'):
            cancel.check()
        result = _find_v1_cut(loc)
        if not result or result[0] is None:
            consecutive = 0
            continue
        v1_time, v1_frame = result
        if progress_cb:
            progress_cb("status",
                        f"Visual fine-tune: matching V1 cut at "
                        f"{v1_time:.1f}s in V2...")
        matched = _match_in_v2(v1_time, v1_frame)
        if matched is not None:
            matched_offsets.append(matched)
            consecutive += 1
            if consecutive >= 2:
                break
        else:
            consecutive = 0

    if len(matched_offsets) < 2:
        if progress_cb:
            progress_cb("status",
                        f"Visual fine-tune: only {len(matched_offsets)} cuts "
                        f"matched, keeping coarse offset")
        return None

    refined = float(np.median(matched_offsets))

    # Refined offset must be closer to zero than audio offset
    if abs(refined) > abs(offset):
        if progress_cb:
            progress_cb("status",
                        f"Visual fine-tune: |{refined:.3f}s| > |{offset:.3f}s|, "
                        f"discarding")
        return None

    if progress_cb:
        progress_cb("status",
                    f"Visual fine-tune: {len(matched_offsets)} cuts matched, "
                    f"offset {offset:.3f}s -> {refined:.3f}s")

    return refined
