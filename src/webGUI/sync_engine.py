#!/usr/bin/env python3

import threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import fflib
from fflib import CancelledError
from audio import (
    AUDIO_SAMPLE_RATE,
    AUDIO_MATCH_TOP_K, AUDIO_RANSAC_ITERATIONS, AUDIO_RANSAC_THRESHOLD_SEC,
    SPEED_CANDIDATES,
    decode_full_audio, extract_audio_fingerprints,
    extract_mel_fingerprints,
    match_fingerprints,
    mutual_nearest_neighbors, downsample_audio, filter_matches_by_offset,
    ransac_linear_fit, residual_stats, xcorr_on_downsampled,
    detect_segments, snap_speed_to_candidate, compute_lufs,
)
from visual import (
    verify_offset_visual, validate_segments_visual, refine_boundary_visual,
)
from ctx import SessionContext


def _bandreject(audio, sr, center=1000.0, width=2700.0):
    """Apply FFT-domain bandreject filter in overlapping chunks."""
    lo = center - width / 2
    hi = center + width / 2
    chunk = sr * 30  # 30-second chunks
    overlap = sr * 2  # 2-second overlap
    n = len(audio)
    out = np.empty(n, dtype=np.float32)
    pos = 0
    while pos < n:
        end = min(pos + chunk, n)
        seg = audio[pos:end]
        sn = len(seg)
        freqs = np.fft.rfftfreq(sn, d=1.0 / sr)
        mask = np.ones(len(freqs), dtype=np.float32)
        mask[(freqs >= lo) & (freqs <= hi)] = 0.0
        edge_lo = (freqs >= lo - 50) & (freqs < lo)
        edge_hi = (freqs > hi) & (freqs <= hi + 50)
        mask[edge_lo] = (lo - freqs[edge_lo]) / 50.0
        mask[edge_hi] = (freqs[edge_hi] - hi) / 50.0
        filtered = np.fft.irfft(np.fft.rfft(seg) * mask, n=sn).astype(np.float32)
        if pos > 0 and overlap > 0:
            ol = min(overlap, sn, pos)
            ramp = np.linspace(0, 1, ol, dtype=np.float32)
            out[pos:pos + ol] = out[pos:pos + ol] * (1 - ramp) + filtered[:ol] * ramp
            out[pos + ol:end] = filtered[ol:]
        else:
            out[pos:end] = filtered
        pos = end - overlap if end < n else n
    return out


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


def _decode_and_fingerprint(ctx):
    if ctx.progress_cb:
        ctx.progress_cb("status", "Decoding V1 + V2 audio...")
    def _dec_cb(label):
        def cb(pct):
            if ctx.progress_cb:
                ctx.progress_cb("status", f"Decoding {label}: {pct}%")
        return cb
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(decode_full_audio, ctx.v1_path, ctx.align_track1,
                         AUDIO_SAMPLE_RATE, ctx.cancel, duration=ctx.align_dur1,
                         progress_cb=_dec_cb("V1"))
        f2 = pool.submit(decode_full_audio, ctx.v2_path, ctx.align_track2,
                         AUDIO_SAMPLE_RATE, ctx.cancel, duration=ctx.align_dur2,
                         progress_cb=_dec_cb("V2"))
        ctx.audio1, msgs1 = f1.result()
        ctx.audio2, msgs2 = f2.result()
    if ctx.cancel:
        ctx.cancel.check()
    for m in (msgs1 or []):
        ctx.decode_warnings.append(f"V1: {m}")
    for m in (msgs2 or []):
        ctx.decode_warnings.append(f"V2: {m}")

    if ctx.progress_cb:
        ctx.progress_cb("status", "Measuring loudness (LUFS)...")
    ctx.v1_lufs = fflib.measure_lufs(ctx.v1_path, ctx.align_track1, cancel=ctx.cancel)
    ctx.v2_lufs = fflib.measure_lufs(ctx.v2_path, ctx.align_track2, cancel=ctx.cancel)

    if ctx.progress_cb:
        ctx.progress_cb("status", "Mel FP: V1...")
    ctx.ts1, ctx.fp1_main = extract_mel_fingerprints(
        ctx.v1_path, track_index=ctx.align_track1, max_samples=ctx.align_max_samples,
        hop_sec=ctx.align_hop1,
        progress_cb=(lambda c, t: ctx.progress_cb("fp", f"V1 Mel: {c}/{t}")
                     if ctx.progress_cb else None),
        cancel=ctx.cancel, audio_data=ctx.audio1, duration=ctx.align_dur1)
    if ctx.cancel:
        ctx.cancel.check()
    if ctx.progress_cb:
        ctx.progress_cb("status", "Mel FP: V2...")
    ctx.ts2, ctx.fp2_main = extract_mel_fingerprints(
        ctx.v2_path, track_index=ctx.align_track2, max_samples=ctx.align_max_samples,
        hop_sec=ctx.align_hop2,
        progress_cb=(lambda c, t: ctx.progress_cb("fp", f"V2 Mel: {c}/{t}")
                     if ctx.progress_cb else None),
        cancel=ctx.cancel, audio_data=ctx.audio2, duration=ctx.align_dur2)
    if ctx.cancel:
        ctx.cancel.check()

    if len(ctx.fp1_main) < 10 or len(ctx.fp2_main) < 10:
        raise RuntimeError(
            f"Not enough audio data (V1: {len(ctx.fp1_main)}, "
            f"V2: {len(ctx.fp2_main)})")

    ctx.ah1 = np.median(np.diff(ctx.ts1)) if len(ctx.ts1) > 1 else ctx.align_hop1
    ctx.ah2 = np.median(np.diff(ctx.ts2)) if len(ctx.ts2) > 1 else ctx.align_hop2


def _compute_coarse_alignment(ctx):
    if ctx.progress_cb:
        ctx.progress_cb("status", "Computing coarse offset + speed (cross-correlation)...")

    if ctx.vocal_filter:
        if ctx.progress_cb:
            ctx.progress_cb("status", "Applying vocal bandreject filter...")
        xcorr_a1 = _bandreject(ctx.audio1, AUDIO_SAMPLE_RATE)
        xcorr_a2 = _bandreject(ctx.audio2, AUDIO_SAMPLE_RATE)
    else:
        xcorr_a1 = ctx.audio1
        xcorr_a2 = ctx.audio2

    ds1, ctx.ds_rate = downsample_audio(xcorr_a1, AUDIO_SAMPLE_RATE)
    ds2, _ = downsample_audio(xcorr_a2, AUDIO_SAMPLE_RATE)
    del xcorr_a1, xcorr_a2

    if ctx.vocal_filter:
        ctx.ds1_seg, _ = downsample_audio(ctx.audio1, AUDIO_SAMPLE_RATE)
        ctx.ds2_seg, _ = downsample_audio(ctx.audio2, AUDIO_SAMPLE_RATE)
    else:
        ctx.ds1_seg, ctx.ds2_seg = ds1, ds2

    ctx.coarse_offset, ctx.xcorr_speed, _, ctx.alt_offsets = \
        xcorr_on_downsampled(ds1, ds2, ctx.ds_rate, SPEED_CANDIDATES,
                             return_alt_offsets=True)

    v2_st = 0.0
    for t in ctx.v2_info.get("audio", []):
        if t.get("index") == ctx.align_track2:
            v2_st = t.get("start_time", 0.0)
            break
    ctx.v2_start_delay = v2_st
    if v2_st > 0.01:
        ctx.coarse_offset -= v2_st
        ctx.alt_offsets = [(off - v2_st, spd, corr)
                           for off, spd, corr in ctx.alt_offsets]

    ctx.audio_offset = ctx.coarse_offset
    ctx.audio_speed = ctx.xcorr_speed

    if ctx.cancel:
        ctx.cancel.check()

    if ctx.v1_has_video and ctx.v2_has_video:
        ctx.visual_result = verify_offset_visual(
            ctx.v1_path, ctx.v2_path, ctx.coarse_offset,
            ctx.xcorr_speed, ctx.alt_offsets,
            ctx.align_dur1, ctx.align_dur2,
            progress_cb=ctx.progress_cb, cancel=ctx.cancel)
        if ctx.visual_result is not None:
            ctx.coarse_offset = ctx.visual_result["offset"]
            ctx.xcorr_speed = ctx.visual_result["speed"]
            ctx.alt_offsets = []
            ctx.visual_corrected = True


def _align_ransac(ctx):
    if ctx.progress_cb:
        ctx.progress_cb("status",
                         f"Matching {len(ctx.fp1_main)}x"
                         f"{len(ctx.fp2_main)} fingerprints...")
    matches = match_fingerprints(ctx.fp1_main, ctx.fp2_main,
                                 top_k=AUDIO_MATCH_TOP_K)
    if ctx.cancel:
        ctx.cancel.check()

    matches = mutual_nearest_neighbors(matches, len(ctx.fp1_main),
                                       len(ctx.fp2_main),
                                       top_k=AUDIO_MATCH_TOP_K)

    filtered = filter_matches_by_offset(matches, ctx.ts1, ctx.ts2,
                                        ctx.coarse_offset,
                                        speed=ctx.xcorr_speed)
    if len(filtered) >= 20:
        matches = filtered
    else:
        filtered = filter_matches_by_offset(matches, ctx.ts1, ctx.ts2,
                                            ctx.coarse_offset,
                                            window_sec=30.0,
                                            speed=ctx.xcorr_speed)
        if len(filtered) >= 20:
            matches = filtered

    if len(matches) < 20:
        if ctx.progress_cb:
            ctx.progress_cb("status", "Falling back to energy-band matching...")
        f1_energy = extract_audio_fingerprints(
            ctx.v1_path, track_index=ctx.align_track1, max_samples=ctx.align_max_samples,
            hop_sec=ctx.align_hop1,
            progress_cb=(lambda c, t: ctx.progress_cb("fp", f"V1 energy: {c}/{t}")
                         if ctx.progress_cb else None),
            cancel=ctx.cancel, audio_data=ctx.audio1, duration=ctx.align_dur1)[1]
        if ctx.cancel:
            ctx.cancel.check()
        f2_energy = extract_audio_fingerprints(
            ctx.v2_path, track_index=ctx.align_track2, max_samples=ctx.align_max_samples,
            hop_sec=ctx.align_hop2,
            progress_cb=(lambda c, t: ctx.progress_cb("fp", f"V2 energy: {c}/{t}")
                         if ctx.progress_cb else None),
            cancel=ctx.cancel, audio_data=ctx.audio2, duration=ctx.align_dur2)[1]
        if ctx.cancel:
            ctx.cancel.check()
        matches = match_fingerprints(f1_energy, f2_energy,
                                     top_k=AUDIO_MATCH_TOP_K)
        matches = mutual_nearest_neighbors(matches, len(f1_energy),
                                           len(f2_energy),
                                           top_k=AUDIO_MATCH_TOP_K)
        filtered = filter_matches_by_offset(matches, ctx.ts1, ctx.ts2,
                                            ctx.coarse_offset,
                                            speed=ctx.xcorr_speed)
        if len(filtered) >= 20:
            matches = filtered

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

    if len(good) < 4:
        return False

    t1m = np.array([ctx.ts1[g[0]] for g in good])
    t2m = np.array([ctx.ts2[g[1]] for g in good])
    ransac_thr = max(AUDIO_RANSAC_THRESHOLD_SEC, (ctx.ah1 + ctx.ah2) * 0.6)
    if ctx.progress_cb:
        ctx.progress_cb("status",
                         f"RANSAC ({len(good)} candidates, "
                         f"thr={ransac_thr:.2f}s)...")
    a, b, mask, ni = ransac_linear_fit(
        t1m, t2m, n_iter=AUDIO_RANSAC_ITERATIONS,
        threshold=ransac_thr, cancel=ctx.cancel)

    t1_inliers = t1m[mask] if ni >= 2 else t1m
    t2_inliers = t2m[mask] if ni >= 2 else t2m
    a, b = snap_speed_to_candidate(a, t1_inliers, t2_inliers)

    pairs = [(ctx.ts1[g[0]], ctx.ts2[g[1]], g[2])
             for g, m in zip(good, mask) if m]
    rmean, rmax, rend = residual_stats(pairs, a, b)

    v1_span = float(ctx.ts1[-1] - ctx.ts1[0])
    inlier_span = 0.0
    if pairs:
        inlier_t1 = [p[0] for p in pairs]
        inlier_span = max(inlier_t1) - min(inlier_t1)
    coverage = inlier_span / v1_span if v1_span > 0 else 0.0

    if ni < 15 or rmean > 0.5 or coverage < 0.5:
        a_fb = ctx.xcorr_speed
        if ni >= 2:
            b_fb = float(np.mean(t1_inliers - a_fb * t2_inliers))
        else:
            b_fb = ctx.coarse_offset
        rmean_fb, rmax_fb, rend_fb = residual_stats(pairs, a_fb, b_fb)
        if ni < 4 or rmean_fb <= rmean:
            a, b = a_fb, b_fb
            rmean, rmax, rend = rmean_fb, rmax_fb, rend_fb

    if ctx.progress_cb:
        ctx.progress_cb("status", "Checking for content breaks...")
    segments = detect_segments(pairs, ctx.xcorr_speed,
                               coarse_offset=ctx.coarse_offset,
                               d1=ctx.ds1_seg, d2=ctx.ds2_seg,
                               effective_rate=ctx.ds_rate,
                               alt_offsets=ctx.alt_offsets)

    if segments and len(segments) > 1 and ctx.v1_has_video and ctx.v2_has_video:
        if ctx.progress_cb:
            ctx.progress_cb("status", "Validating segments visually...")
        if validate_segments_visual(
                ctx.v1_path, ctx.v2_path, segments,
                ctx.coarse_offset,
                ctx.xcorr_speed, ctx.align_dur1, ctx.align_dur2, cancel=ctx.cancel):
            segments = [{"v1_start": 0.0, "v1_end": float("inf"),
                         "offset": ctx.coarse_offset,
                         "n_inliers": len(pairs)}]
        else:
            refine_segs = refine_boundary_visual(
                ctx.v1_path, ctx.v2_path, segments, ctx.xcorr_speed,
                format_timestamp=format_timestamp,
                progress_cb=ctx.progress_cb, cancel=ctx.cancel)
            for i, seg in enumerate(refine_segs):
                segments[i]["v1_start"] = seg["v1_start"]
                segments[i]["v1_end"] = seg["v1_end"]
            for si in range(len(segments)):
                seg = segments[si]
                v1_s = int(seg["v1_start"] * ctx.ds_rate)
                v1_e_raw = seg["v1_end"]
                if v1_e_raw >= 1e9:
                    v1_e_raw = len(ctx.ds1_seg) / ctx.ds_rate
                v1_e = int(v1_e_raw * ctx.ds_rate)
                prev_off = (segments[si - 1]["offset"] if si > 0
                            else ctx.coarse_offset)
                v2_est = (seg["v1_start"] - prev_off) / ctx.xcorr_speed
                v2_s = max(0, int((v2_est - 300) * ctx.ds_rate))
                v2_e = min(len(ctx.ds2_seg),
                           int((v2_est + (v1_e_raw - seg["v1_start"]) + 300)
                               * ctx.ds_rate))
                d1_s = ctx.ds1_seg[v1_s:v1_e]
                d2_s = ctx.ds2_seg[v2_s:v2_e]
                if len(d1_s) > ctx.ds_rate * 60 and len(d2_s) > ctx.ds_rate * 60:
                    off_s, spd_s, _ = xcorr_on_downsampled(
                        d1_s, d2_s, ctx.ds_rate, SPEED_CANDIDATES)
                    if abs(spd_s - ctx.xcorr_speed) / ctx.xcorr_speed <= 0.005:
                        v2_abs = v2_s / ctx.ds_rate
                        segments[si]["offset"] = (seg["v1_start"] + off_s
                                                  - v2_abs * spd_s)

    if ctx.visual_corrected or (segments and len(segments) > 1):
        a = ctx.xcorr_speed
        if ctx.visual_corrected and len(segments) == 1:
            b = ctx.coarse_offset
            segments[0]["offset"] = b
        else:
            b = segments[0]["offset"]
    elif segments:
        inlier_t1s = np.array([p[0] for p in pairs]) if pairs else np.array([])
        inlier_t2s = np.array([p[1] for p in pairs]) if pairs else np.array([])
        if len(inlier_t1s) >= 2:
            b = float(np.mean(inlier_t1s - a * inlier_t2s))
        else:
            b = segments[0]["offset"]

    ctx.align_mode = "audio"
    ctx.align_a = a
    ctx.align_b = b
    ctx.align_ni = ni
    ctx.align_total_good = len(good)
    ctx.align_pairs = pairs
    ctx.align_rmean = rmean
    ctx.align_rmax = rmax
    ctx.align_rend = rend
    ctx.segments = segments
    return True


def _speed_to_atempo(a):
    return 1.0 / a if abs(a) > 1e-9 else 1.0


def prepare_align(ctx):
    from probe import get_duration
    from audio import AUDIO_HOP_SEC, AUDIO_MAX_SAMPLES
    if not ctx.v1_info:
        ctx.v1_info = {}
    if not ctx.v2_info:
        ctx.v2_info = {}
    ctx.align_dur1 = ctx.v1_info.get("duration", 0) or get_duration(ctx.v1_path)
    ctx.align_dur2 = ctx.v2_info.get("duration", 0) or get_duration(ctx.v2_path)
    hop = AUDIO_HOP_SEC
    max_s = AUDIO_MAX_SAMPLES
    ctx.align_hop1 = ctx.align_dur1 / max_s if (ctx.align_dur1 > 0 and ctx.align_dur1 / hop > max_s) else hop
    ctx.align_hop2 = ctx.align_dur2 / max_s if (ctx.align_dur2 > 0 and ctx.align_dur2 / hop > max_s) else hop
    ctx.align_max_samples = max_s
    ctx.v1_has_video = any(s.get("codec_type") == "video" for s in ctx.v1_info.get("streams", []))
    ctx.v2_has_video = any(s.get("codec_type") == "video" for s in ctx.v2_info.get("streams", []))
    ctx.decode_warnings = []


def free_audio(ctx):
    ctx.audio1 = None
    ctx.audio2 = None


def build_align_result(ctx):
    atempo = _speed_to_atempo(ctx.align_a)
    vr = ctx.visual_result
    vc = ctx.visual_corrected
    return {
        "speed_ratio": atempo, "offset": ctx.align_b,
        "linear_a": ctx.align_a, "linear_b": ctx.align_b,
        "inlier_count": ctx.align_ni, "total_candidates": ctx.align_total_good,
        "inlier_pairs": ctx.align_pairs,
        "v1_coverage": (float(ctx.ts1[0]), float(ctx.ts1[-1])),
        "v2_coverage": (float(ctx.ts2[0]), float(ctx.ts2[-1])),
        "v1_interval": float(ctx.ah1), "v2_interval": float(ctx.ah2),
        "mode": ctx.align_mode, "sync_tracks": (ctx.align_track1, ctx.align_track2),
        "residual_mean": ctx.align_rmean, "residual_max": ctx.align_rmax,
        "residual_end": ctx.align_rend,
        "coarse_offset": ctx.coarse_offset,
        "segments": ctx.segments,
        "warnings": ctx.decode_warnings,
        "audio_offset": ctx.audio_offset,
        "audio_speed": ctx.audio_speed,
        "visual_corrected": vc,
        "visual_offset": vr["offset"] if vc and vr else None,
        "visual_speed": vr["speed"] if vc and vr else None,
        "visual_score": vr["score"] if vc and vr else None,
        "audio_visual_score": vr.get("audio_score") if vc and vr else None,
        "v1_lufs": ctx.v1_lufs,
        "v2_lufs": ctx.v2_lufs,
        "v2_start_delay": ctx.v2_start_delay,
    }


def auto_align_audio(ctx):
    prepare_align(ctx)
    _decode_and_fingerprint(ctx)
    _compute_coarse_alignment(ctx)

    if _align_ransac(ctx):
        free_audio(ctx)
    else:
        free_audio(ctx)
        ctx.align_mode = "audio-xcorr"
        ctx.align_a = ctx.xcorr_speed
        ctx.align_b = ctx.coarse_offset
        ctx.segments = [{"v1_start": 0.0, "v1_end": float("inf"),
                         "offset": ctx.coarse_offset, "n_inliers": 0}]

    v2_st = 0.0
    for t in ctx.v2_info.get("audio", []):
        if t.get("index") == ctx.align_track2:
            v2_st = t.get("start_time", 0.0)
            break
    if v2_st > 0.01 and not ctx.visual_corrected:
        ctx.align_b -= v2_st
        ctx.coarse_offset -= v2_st
        for seg in (ctx.segments or []):
            seg["offset"] -= v2_st

    ctx.atempo = _speed_to_atempo(ctx.align_a)
    ctx.offset = ctx.align_b

    return build_align_result(ctx)


from merger import merge_with_ffmpeg
