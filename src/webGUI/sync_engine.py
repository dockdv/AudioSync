#!/usr/bin/env python3

import threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import fflib
from fflib import CancelledError
from probe import get_duration
from audio import (
    AUDIO_SAMPLE_RATE, AUDIO_HOP_SEC, AUDIO_MAX_SAMPLES,
    AUDIO_MATCH_TOP_K, AUDIO_RANSAC_ITERATIONS, AUDIO_RANSAC_THRESHOLD_SEC,
    SPEED_CANDIDATES,
    decode_full_audio, extract_audio_fingerprints,
    extract_band_peak_fingerprints, match_fingerprints,
    mutual_nearest_neighbors, downsample_audio, filter_matches_by_offset,
    ransac_linear_fit, residual_stats, xcorr_on_downsampled,
    detect_segments, snap_speed_to_candidate,
)
from visual import (
    verify_offset_visual, refine_boundary_visual,
)


def format_timestamp(seconds):
    if seconds is None or seconds != seconds:
        return "0:00.000"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{sign}{h}:{m:02d}:{s:06.3f}"
    return f"{sign}{m}:{s:06.3f}"

class CancellableTask:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    @property
    def is_cancelled(self):
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise CancelledError("Cancelled")


def auto_align_audio(fp1, fp2, track1=0, track2=0,
                      progress_cb=None, cancel=None,
                      vocal_filter=False):
    dur1 = get_duration(fp1)
    dur2 = get_duration(fp2)
    hop = AUDIO_HOP_SEC
    max_s = AUDIO_MAX_SAMPLES
    hop1 = dur1 / max_s if (dur1 > 0 and dur1 / hop > max_s) else hop
    hop2 = dur2 / max_s if (dur2 > 0 and dur2 / hop > max_s) else hop

    decode_warnings = []
    if progress_cb:
        progress_cb("status", "Decoding V1 + V2 audio...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(decode_full_audio, fp1, track1, AUDIO_SAMPLE_RATE,
                         cancel)
        f2 = pool.submit(decode_full_audio, fp2, track2, AUDIO_SAMPLE_RATE,
                         cancel)
        audio1, msgs1 = f1.result()
        audio2, msgs2 = f2.result()
    if cancel:
        cancel.check()
    for m in (msgs1 or []):
        decode_warnings.append(f"V1: {m}")
    for m in (msgs2 or []):
        decode_warnings.append(f"V2: {m}")

    if progress_cb:
        progress_cb("status", "Band-peak FP: V1...")
    ts1, f1_peak = extract_band_peak_fingerprints(
        fp1, track_index=track1, max_samples=max_s, hop_sec=hop1,
        progress_cb=(lambda c, t: progress_cb("fp", f"V1 band-peak: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel, audio_data=audio1, duration=dur1)
    if cancel:
        cancel.check()
    if progress_cb:
        progress_cb("status", "Band-peak FP: V2...")
    ts2, f2_peak = extract_band_peak_fingerprints(
        fp2, track_index=track2, max_samples=max_s, hop_sec=hop2,
        progress_cb=(lambda c, t: progress_cb("fp", f"V2 band-peak: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel, audio_data=audio2, duration=dur2)
    if cancel:
        cancel.check()

    if len(f1_peak) < 10 or len(f2_peak) < 10:
        raise RuntimeError(
            f"Not enough audio data (V1: {len(f1_peak)}, "
            f"V2: {len(f2_peak)})")

    f1_energy, f2_energy = None, None

    def _ensure_energy_fp(a1, a2):
        nonlocal f1_energy, f2_energy
        if f1_energy is not None:
            return
        if progress_cb:
            progress_cb("status", "Computing energy fingerprints (fallback)...")
        f1_energy = extract_audio_fingerprints(
            fp1, track_index=track1, max_samples=max_s, hop_sec=hop1,
            progress_cb=(lambda c, t: progress_cb("fp", f"V1 energy: {c}/{t}")
                         if progress_cb else None),
            cancel=cancel, audio_data=a1, duration=dur1)[1]
        if cancel:
            cancel.check()
        f2_energy = extract_audio_fingerprints(
            fp2, track_index=track2, max_samples=max_s, hop_sec=hop2,
            progress_cb=(lambda c, t: progress_cb("fp", f"V2 energy: {c}/{t}")
                         if progress_cb else None),
            cancel=cancel, audio_data=a2, duration=dur2)[1]
        if cancel:
            cancel.check()

    if progress_cb:
        progress_cb("status", "Computing coarse offset + speed (cross-correlation)...")

    if vocal_filter:
        if progress_cb:
            progress_cb("status", "Decoding band-filtered audio for xcorr...")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(decode_full_audio, fp1, track1,
                             AUDIO_SAMPLE_RATE, cancel, vocal_filter=True)
            f2 = pool.submit(decode_full_audio, fp2, track2,
                             AUDIO_SAMPLE_RATE, cancel, vocal_filter=True)
            xcorr_a1, _ = f1.result()
            xcorr_a2, _ = f2.result()
        if cancel:
            cancel.check()
    else:
        xcorr_a1 = audio1
        xcorr_a2 = audio2

    ds1, ds_rate = downsample_audio(xcorr_a1, AUDIO_SAMPLE_RATE)
    ds2, _ = downsample_audio(xcorr_a2, AUDIO_SAMPLE_RATE)
    del xcorr_a1, xcorr_a2

    if vocal_filter:
        ds1_seg, _ = downsample_audio(audio1, AUDIO_SAMPLE_RATE)
        ds2_seg, _ = downsample_audio(audio2, AUDIO_SAMPLE_RATE)
    else:
        ds1_seg, ds2_seg = ds1, ds2

    coarse_offset, xcorr_speed, xcorr_corr, alt_offsets = xcorr_on_downsampled(
        ds1, ds2, ds_rate, SPEED_CANDIDATES, return_alt_offsets=True)

    audio_offset = coarse_offset
    audio_speed = xcorr_speed

    if cancel:
        cancel.check()

    v1_has_video = any(s["codec_type"] == "video"
                       for s in fflib.probe(fp1).get("streams", []))
    v2_has_video = any(s["codec_type"] == "video"
                       for s in fflib.probe(fp2).get("streams", []))
    visual_corrected = False
    visual_result = None
    if v1_has_video and v2_has_video:
        visual_result = verify_offset_visual(
            fp1, fp2, coarse_offset, xcorr_speed, alt_offsets, dur1, dur2,
            progress_cb=progress_cb, cancel=cancel)
        if visual_result is not None:
            coarse_offset = visual_result["offset"]
            xcorr_speed = visual_result["speed"]
            alt_offsets = []
            visual_corrected = True

    if progress_cb:
        progress_cb("status",
                     f"Matching {len(f1_peak)}x{len(f2_peak)} "
                     f"peak fingerprints...")
    matches = match_fingerprints(f1_peak, f2_peak, top_k=AUDIO_MATCH_TOP_K)
    if cancel:
        cancel.check()

    matches = mutual_nearest_neighbors(matches, len(f1_peak), len(f2_peak),
                                       top_k=AUDIO_MATCH_TOP_K)

    filtered = filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                                        speed=xcorr_speed)
    if len(filtered) >= 20:
        matches = filtered
    else:
        filtered = filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                                            window_sec=30.0, speed=xcorr_speed)
        if len(filtered) >= 20:
            matches = filtered

    if len(matches) < 20:
        if progress_cb:
            progress_cb("status", "Falling back to energy-band matching...")
        _ensure_energy_fp(audio1, audio2)
        matches = match_fingerprints(f1_energy, f2_energy,
                                     top_k=AUDIO_MATCH_TOP_K)
        matches = mutual_nearest_neighbors(matches, len(f1_energy),
                                           len(f2_energy),
                                           top_k=AUDIO_MATCH_TOP_K)
        filtered = filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                                            speed=xcorr_speed)
        if len(filtered) >= 20:
            matches = filtered

    del audio1, audio2

    if len(matches) == 0:
        good = []
    else:
        sims = np.array([m[2] for m in matches])
        thr = max(0.90, np.percentile(sims, 80))
        good = [m for m in matches if m[2] >= thr]
        if len(good) < 20:
            thr = max(0.80, np.percentile(sims, 60))
            good = [m for m in matches if m[2] >= thr]
        if len(good) < 10:
            thr = max(0.70, np.percentile(sims, 40))
            good = [m for m in matches if m[2] >= thr]
    ah1 = np.median(np.diff(ts1)) if len(ts1) > 1 else hop1
    ah2 = np.median(np.diff(ts2)) if len(ts2) > 1 else hop2

    if len(good) < 4:
        atempo_xcorr = 1.0 / xcorr_speed if abs(xcorr_speed) > 1e-9 else 1.0
        return {
            "speed_ratio": atempo_xcorr, "offset": coarse_offset,
            "linear_a": xcorr_speed, "linear_b": coarse_offset,
            "inlier_count": 0, "total_candidates": len(good),
            "inlier_pairs": [],
            "v1_coverage": (float(ts1[0]), float(ts1[-1])),
            "v2_coverage": (float(ts2[0]), float(ts2[-1])),
            "v1_interval": float(ah1), "v2_interval": float(ah2),
            "mode": "audio-xcorr", "sync_tracks": (track1, track2),
            "residual_mean": 0, "residual_max": 0,
            "residual_end": 0,
            "coarse_offset": coarse_offset,
            "segments": [{"v1_start": 0.0, "v1_end": float("inf"),
                          "offset": coarse_offset, "n_inliers": 0}],
            "warnings": decode_warnings,
            "audio_offset": audio_offset,
            "audio_speed": audio_speed,
            "visual_corrected": visual_corrected,
            "visual_offset": visual_result["offset"] if visual_corrected else None,
            "visual_speed": visual_result["speed"] if visual_corrected else None,
            "visual_score": visual_result["score"] if visual_corrected else None,
            "audio_visual_score": visual_result.get("audio_score") if visual_corrected else None,
        }

    t1m = np.array([ts1[g[0]] for g in good])
    t2m = np.array([ts2[g[1]] for g in good])
    ransac_thr = max(AUDIO_RANSAC_THRESHOLD_SEC, (ah1 + ah2) * 0.6)
    if progress_cb:
        progress_cb("status",
                     f"RANSAC ({len(good)} candidates, "
                     f"thr={ransac_thr:.2f}s)...")
    a, b, mask, ni = ransac_linear_fit(
        t1m, t2m, n_iter=AUDIO_RANSAC_ITERATIONS,
        threshold=ransac_thr, cancel=cancel)

    t1_inliers = t1m[mask] if ni >= 2 else t1m
    t2_inliers = t2m[mask] if ni >= 2 else t2m
    a, b = snap_speed_to_candidate(a, t1_inliers, t2_inliers)

    pairs = [(ts1[g[0]], ts2[g[1]], g[2])
             for g, m in zip(good, mask) if m]
    rmean, rmax, rend = residual_stats(pairs, a, b)

    v1_span = float(ts1[-1] - ts1[0])
    inlier_span = 0.0
    if pairs:
        inlier_t1 = [p[0] for p in pairs]
        inlier_span = max(inlier_t1) - min(inlier_t1)
    coverage = inlier_span / v1_span if v1_span > 0 else 0.0

    use_xcorr_fallback = (ni < 15 or rmean > 0.5 or coverage < 0.5)

    if use_xcorr_fallback:
        a_fb = xcorr_speed
        if ni >= 2:
            b_fb = float(np.mean(t1_inliers - a_fb * t2_inliers))
        else:
            b_fb = coarse_offset
        pairs_fb = pairs
        rmean_fb, rmax_fb, rend_fb = residual_stats(pairs_fb, a_fb, b_fb)
        if ni < 4 or rmean_fb <= rmean:
            a, b = a_fb, b_fb
            rmean, rmax, rend = rmean_fb, rmax_fb, rend_fb

    if progress_cb:
        progress_cb("status", "Checking for content breaks...")
    segments = detect_segments(pairs, xcorr_speed,
                               coarse_offset=coarse_offset,
                               d1=ds1_seg, d2=ds2_seg,
                               effective_rate=ds_rate,
                               alt_offsets=alt_offsets)

    if segments and len(segments) > 1:
        if visual_corrected:
            from visual import visual_offset_score
            all_match_visual = True
            for seg in segments:
                v1_s = seg["v1_start"]
                v1_e = seg["v1_end"] if seg["v1_end"] < 1e9 else dur1
                seg_dur = v1_e - v1_s
                if seg_dur < 60:
                    continue
                score = visual_offset_score(
                    fp1, fp2, coarse_offset, xcorr_speed,
                    dur1, dur2, n_probes=3, cancel=cancel)
                if score < 0.4:
                    all_match_visual = False
                    break
            if all_match_visual:
                segments = [{"v1_start": 0.0, "v1_end": float("inf"),
                             "offset": coarse_offset,
                             "n_inliers": len(pairs)}]
        else:
            segments = refine_boundary_visual(
                fp1, fp2, segments, xcorr_speed,
                format_timestamp=format_timestamp,
                progress_cb=progress_cb, cancel=cancel)
            for si in range(len(segments)):
                seg = segments[si]
                v1_s = int(seg["v1_start"] * ds_rate)
                v1_e_raw = seg["v1_end"]
                if v1_e_raw >= 1e9:
                    v1_e_raw = len(ds1_seg) / ds_rate
                v1_e = int(v1_e_raw * ds_rate)
                prev_off = segments[si - 1]["offset"] if si > 0 else coarse_offset
                v2_est = (seg["v1_start"] - prev_off) / xcorr_speed
                v2_s = max(0, int((v2_est - 300) * ds_rate))
                v2_e = min(len(ds2_seg), int((v2_est + (v1_e_raw - seg["v1_start"]) + 300) * ds_rate))
                d1_s = ds1_seg[v1_s:v1_e]
                d2_s = ds2_seg[v2_s:v2_e]
                if len(d1_s) > ds_rate * 60 and len(d2_s) > ds_rate * 60:
                    off_s, spd_s, _ = xcorr_on_downsampled(
                        d1_s, d2_s, ds_rate, SPEED_CANDIDATES)
                    if abs(spd_s - xcorr_speed) / xcorr_speed <= 0.005:
                        v2_abs = v2_s / ds_rate
                        segments[si]["offset"] = seg["v1_start"] + off_s - v2_abs * spd_s

    if visual_corrected:
        a = xcorr_speed
        b = segments[0]["offset"]
    elif segments and len(segments) > 1:
        a = xcorr_speed
        b = segments[0]["offset"]
    elif segments:
        inlier_t1s = np.array([p[0] for p in pairs]) if pairs else np.array([])
        inlier_t2s = np.array([p[1] for p in pairs]) if pairs else np.array([])
        if len(inlier_t1s) >= 2:
            b = float(np.mean(inlier_t1s - a * inlier_t2s))
        else:
            b = segments[0]["offset"]

    atempo = 1.0 / a if abs(a) > 1e-9 else 1.0

    return {
        "speed_ratio": atempo, "offset": b,
        "linear_a": a, "linear_b": b,
        "inlier_count": ni, "total_candidates": len(good),
        "inlier_pairs": pairs,
        "v1_coverage": (float(ts1[0]), float(ts1[-1])),
        "v2_coverage": (float(ts2[0]), float(ts2[-1])),
        "v1_interval": float(ah1), "v2_interval": float(ah2),
        "mode": "audio", "sync_tracks": (track1, track2),
        "residual_mean": rmean, "residual_max": rmax,
        "residual_end": rend,
        "coarse_offset": coarse_offset,
        "segments": segments,
        "warnings": decode_warnings,
        "audio_offset": audio_offset,
        "audio_speed": audio_speed,
        "visual_corrected": visual_corrected,
        "visual_offset": visual_result["offset"] if visual_corrected else None,
        "visual_speed": visual_result["speed"] if visual_corrected else None,
        "visual_score": visual_result["score"] if visual_corrected else None,
        "audio_visual_score": visual_result.get("audio_score") if visual_corrected else None,
    }


from merger import (find_ffmpeg_binary, merge_with_ffmpeg, remux_with_ffmpeg)
