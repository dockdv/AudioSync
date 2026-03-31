#!/usr/bin/env python3

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import fflib
from probe import get_duration

AUDIO_SAMPLE_RATE = 8000
AUDIO_WINDOW_SEC = 0.5
AUDIO_HOP_SEC = 0.2
AUDIO_MAX_SAMPLES = 8000
AUDIO_N_BANDS = 40
AUDIO_MATCH_TOP_K = 3
AUDIO_RANSAC_ITERATIONS = 3000
AUDIO_RANSAC_THRESHOLD_SEC = 0.3

AUDIO_XCORR_WINDOW_SEC = 10.0

AUDIO_N_COARSE_BANDS = 128
AUDIO_COARSE_N_PEAKS = 15

SPEED_CANDIDATES = [
    23.976 / 25.0,
    24.0 / 25.0,
    23.976 / 24.0,
    1.0,
    24.0 / 23.976,
    25.0 / 24.0,
    25.0 / 23.976,
]



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


class CancelledError(Exception):
    pass


def extract_audio_fingerprints(filepath, track_index=0,
                                max_samples=AUDIO_MAX_SAMPLES,
                                hop_sec=AUDIO_HOP_SEC,
                                window_sec=AUDIO_WINDOW_SEC,
                                sr=AUDIO_SAMPLE_RATE,
                                progress_cb=None, cancel=None,
                                audio_data=None, duration=0):
    if duration <= 0:
        duration = get_duration(filepath)
    if duration <= 0:
        duration = 300.0
    needed_samples = int(duration / hop_sec) + 1
    if needed_samples > max_samples:
        hop_sec = duration / max_samples

    window_samples = int(window_sec * sr)
    hop_samples = int(hop_sec * sr)
    n_bands = AUDIO_N_BANDS

    hann = np.hanning(window_samples).astype(np.float32)
    n_fft_bins = window_samples // 2 + 1
    min_bin = max(1, int(60.0 / (sr / window_samples)))
    band_edges = np.logspace(
        np.log10(min_bin), np.log10(n_fft_bins - 1),
        n_bands + 1
    ).astype(int)
    band_edges = np.clip(band_edges, 0, n_fft_bins - 1)

    if audio_data is not None:
        audio = audio_data
    else:
        audio = _decode_full_audio(filepath, track_index, sr, cancel)
    if len(audio) < window_samples:
        raise RuntimeError("Could not extract enough audio data")

    if progress_cb:
        progress_cb(0, max_samples)

    timestamps, fingerprints = [], []
    pos = 0
    count = 0
    total_possible = min(max_samples,
                         (len(audio) - window_samples) // hop_samples + 1)

    while pos + window_samples <= len(audio) and count < max_samples:
        if cancel and count % 200 == 0:
            cancel.check()
        frame = audio[pos:pos + window_samples] * hann
        spectrum = np.abs(np.fft.rfft(frame))
        fp = np.zeros(n_bands, dtype=np.float32)
        for b in range(n_bands):
            lo = band_edges[b]
            hi = max(lo + 1, band_edges[b + 1])
            fp[b] = np.log1p(np.mean(spectrum[lo:hi]))
        norm = np.linalg.norm(fp)
        if norm > 0:
            fp /= norm
        timestamps.append(pos / sr)
        fingerprints.append(fp)
        pos += hop_samples
        count += 1
        if progress_cb and count % 500 == 0:
            progress_cb(count, total_possible)

    if progress_cb:
        progress_cb(count, count)
    if count < 10:
        raise RuntimeError(
            f"Only {count} audio fingerprints extracted")
    return np.array(timestamps), np.array(fingerprints)


def _decode_full_audio(filepath, track_index, sr, cancel=None,
                       vocal_filter=False, fast_decode=False):
    audio = fflib.decode_audio(filepath, track_index, sr,
                               vocal_filter=vocal_filter,
                               fast_decode=fast_decode)
    if len(audio) == 0:
        raise RuntimeError("No audio data decoded")
    return audio


def extract_band_peak_fingerprints(filepath, track_index=0,
                                    max_samples=AUDIO_MAX_SAMPLES,
                                    hop_sec=AUDIO_HOP_SEC,
                                    window_sec=AUDIO_WINDOW_SEC,
                                    sr=AUDIO_SAMPLE_RATE,
                                    n_bands=AUDIO_N_COARSE_BANDS,
                                    n_peaks=AUDIO_COARSE_N_PEAKS,
                                    progress_cb=None, cancel=None,
                                    audio_data=None, duration=0):
    if duration <= 0:
        duration = get_duration(filepath)
    if duration <= 0:
        duration = 300.0
    needed_samples = int(duration / hop_sec) + 1
    if needed_samples > max_samples:
        hop_sec = duration / max_samples

    window_samples = int(window_sec * sr)
    hop_samples = int(hop_sec * sr)
    hann = np.hanning(window_samples).astype(np.float32)
    n_fft_bins = window_samples // 2 + 1

    min_bin = max(1, int(60.0 / (sr / window_samples)))
    band_edges = np.logspace(
        np.log10(min_bin), np.log10(n_fft_bins - 1),
        n_bands + 1
    ).astype(int)
    band_edges = np.clip(band_edges, 0, n_fft_bins - 1)

    if audio_data is not None:
        audio = audio_data
    else:
        audio = _decode_full_audio(filepath, track_index, sr, cancel)
    if len(audio) < window_samples:
        raise RuntimeError("Could not extract enough audio data")

    if progress_cb:
        progress_cb(0, max_samples)

    timestamps, fingerprints = [], []
    pos = 0
    count = 0
    total_possible = min(max_samples,
                         (len(audio) - window_samples) // hop_samples + 1)

    while pos + window_samples <= len(audio) and count < max_samples:
        if cancel and count % 200 == 0:
            cancel.check()
        frame = audio[pos:pos + window_samples] * hann
        spectrum = np.abs(np.fft.rfft(frame))

        is_peak = np.zeros(len(spectrum), dtype=bool)
        if len(spectrum) > 2:
            is_peak[1:-1] = ((spectrum[1:-1] > spectrum[:-2]) &
                             (spectrum[1:-1] > spectrum[2:]))
        peak_indices = np.where(is_peak)[0]
        peak_mags = spectrum[peak_indices]

        if len(peak_mags) > n_peaks:
            top_idx = np.argsort(peak_mags)[-n_peaks:]
            peak_indices = peak_indices[top_idx]
            peak_mags = peak_mags[top_idx]

        fp = np.zeros(n_bands, dtype=np.float32)
        for pi, pm in zip(peak_indices, peak_mags):
            band = np.searchsorted(band_edges[1:], pi)
            if band < n_bands:
                fp[band] = max(fp[band], np.log1p(pm))

        norm = np.linalg.norm(fp)
        if norm > 0:
            fp /= norm

        timestamps.append(pos / sr)
        fingerprints.append(fp)
        pos += hop_samples
        count += 1
        if progress_cb and count % 500 == 0:
            progress_cb(count, total_possible)

    if progress_cb:
        progress_cb(count, count)
    if count < 10:
        raise RuntimeError(
            f"Only {count} band-peak fingerprints extracted")
    return np.array(timestamps), np.array(fingerprints)


def match_fingerprints(fp1, fp2, top_k=AUDIO_MATCH_TOP_K):
    sim = fp1 @ fp2.T
    matches = []
    for i in range(len(fp1)):
        if sim.shape[1] <= top_k:
            best = np.argsort(sim[i])[::-1]
        else:
            idx = np.argpartition(sim[i], -top_k)[-top_k:]
            best = idx[np.argsort(sim[i][idx])[::-1]]
        for j in best:
            matches.append((i, int(j), float(sim[i][j])))
    return matches


def mutual_nearest_neighbors(matches, n1, n2, top_k=AUDIO_MATCH_TOP_K):
    reverse = {}
    for i, j, sim in matches:
        reverse.setdefault(j, []).append((i, sim))
    reverse_top = {}
    for j, candidates in reverse.items():
        candidates.sort(key=lambda x: x[1], reverse=True)
        reverse_top[j] = {c[0] for c in candidates[:top_k]}
    filtered = [(i, j, sim) for i, j, sim in matches
                if i in reverse_top.get(j, set())]
    return filtered


XCORR_DOWNSAMPLE_RATE = 100


def _downsample_audio(audio, sr=AUDIO_SAMPLE_RATE):
    block = max(1, sr // XCORR_DOWNSAMPLE_RATE)
    effective_rate = sr / block
    n = len(audio) - len(audio) % block
    ds = np.mean(np.abs(audio[:n]).reshape(-1, block), axis=1)
    return ds, effective_rate


def filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                             window_sec=AUDIO_XCORR_WINDOW_SEC,
                             speed=1.0):
    filtered = []
    for i, j, sim in matches:
        predicted_t1 = speed * ts2[j] + coarse_offset
        if abs(ts1[i] - predicted_t1) <= window_sec:
            filtered.append((i, j, sim))
    return filtered


def ransac_linear_fit(t1, t2, n_iter=AUDIO_RANSAC_ITERATIONS,
                       threshold=AUDIO_RANSAC_THRESHOLD_SEC, cancel=None):
    n = len(t1)
    if n < 2:
        return 1.0, 0.0, np.ones(n, dtype=bool), n
    ba, bb, bn, bm = 1.0, 0.0, 0, np.zeros(n, dtype=bool)
    t2_range = t2.max() - t2.min()

    for it in range(n_iter):
        if cancel and it % 200 == 0:
            cancel.check()
        if it % 3 == 0 and n > 20:
            q1 = np.where(t2 <= t2.min() + t2_range * 0.3)[0]
            q4 = np.where(t2 >= t2.max() - t2_range * 0.3)[0]
            if len(q1) > 0 and len(q4) > 0:
                i1 = q1[np.random.randint(len(q1))]
                i2 = q4[np.random.randint(len(q4))]
                idx = np.array([i1, i2])
            else:
                idx = np.random.choice(n, 2, replace=False)
        else:
            idx = np.random.choice(n, 2, replace=False)
        dt = t2[idx[1]] - t2[idx[0]]
        if abs(dt) < 1e-9:
            continue
        a = (t1[idx[1]] - t1[idx[0]]) / dt
        b = t1[idx[0]] - a * t2[idx[0]]
        if a < 0.5 or a > 2.0:
            continue
        mask = np.abs(t1 - (a * t2 + b)) < threshold
        c = np.sum(mask)
        if c > bn:
            bn, bm, ba, bb = c, mask, a, b

    if bn >= 2:
        A = np.vstack([t2[bm], np.ones(bn)]).T
        ba, bb = np.linalg.lstsq(A, t1[bm], rcond=None)[0]
        for _ in range(3):
            refined_mask = np.abs(t1 - (ba * t2 + bb)) < threshold
            rc = np.sum(refined_mask)
            if rc > bn:
                A2 = np.vstack([t2[refined_mask], np.ones(rc)]).T
                ba, bb = np.linalg.lstsq(A2, t1[refined_mask],
                                          rcond=None)[0]
                bn, bm = rc, refined_mask
            else:
                break
    return ba, bb, bm, bn


def _residual_stats(pairs, a, b):
    if not pairs:
        return 0, 0, 0
    inlier_t1 = np.array([p[0] for p in pairs])
    inlier_t2 = np.array([p[1] for p in pairs])
    residuals = np.abs(inlier_t1 - (a * inlier_t2 + b))
    return (float(np.mean(residuals)),
            float(np.max(residuals)),
            float(residuals[-1]) if len(residuals) > 0 else 0)


SPEED_SNAP_TOLERANCE = 0.005
def _xcorr_on_downsampled(d1, d2, effective_rate, speed_candidates):
    best_corr = -np.inf
    best_offset = 0.0
    best_speed = 1.0

    d1n = d1 - np.mean(d1)
    s1 = np.std(d1n)
    if s1 > 0:
        d1n /= s1

    for speed in speed_candidates:
        n2s = int(len(d2) * speed)
        if n2s < 2:
            continue
        d2s = np.interp(np.linspace(0, len(d2) - 1, n2s),
                        np.arange(len(d2)), d2)
        d2n = d2s - np.mean(d2s)
        s2 = np.std(d2n)
        if s2 > 0:
            d2n /= s2

        n = len(d1n) + len(d2n) - 1
        nfft = 1
        while nfft < n:
            nfft <<= 1
        p1 = np.zeros(nfft)
        p2 = np.zeros(nfft)
        p1[:len(d1n)] = d1n
        p2[:len(d2n)] = d2n
        xcorr = np.fft.irfft(np.fft.rfft(p1) * np.conj(np.fft.rfft(p2)),
                              n=nfft)
        overlap = min(len(d1n), len(d2n))
        pv = float(np.max(xcorr)) / overlap if overlap > 0 else 0.0
        pi = int(np.argmax(xcorr))
        if pi > nfft // 2:
            pi -= nfft
        if pv > best_corr:
            best_corr = pv
            best_offset = float(pi / effective_rate)
            best_speed = speed

    return best_offset, best_speed, best_corr


def detect_segments(inlier_pairs, a, coarse_offset=0.0,
                    d1=None, d2=None, effective_rate=100.0,
                    min_segment_sec=300):
    primary_offset = coarse_offset

    primary_seg = {
        "v1_start": 0.0, "v1_end": float("inf"),
        "offset": primary_offset, "n_inliers": len(inlier_pairs),
    }

    if d1 is None or d2 is None:
        return [primary_seg]

    er = effective_rate
    v1_dur = len(d1) / er
    if v1_dur < min_segment_sec * 3:
        return [primary_seg]

    def _xcorr_window(d1_slice, d2_slice):
        if len(d1_slice) < 10 or len(d2_slice) < 10:
            return None, None
        return _xcorr_on_downsampled(
            d1_slice, d2_slice, er, SPEED_CANDIDATES)[:2]

    def _xcorr_at_split(v1_split_sec):
        v1_s = int(v1_split_sec * er)
        v2_est = (v1_split_sec - primary_offset) / a
        v2_s = max(0, int((v2_est - 300) * er))
        a1h = d1[v1_s:]
        a2h = d2[v2_s:]
        if len(a1h) < er * min_segment_sec or len(a2h) < er * min_segment_sec:
            return None
        off, spd = _xcorr_window(a1h, a2h)
        if off is None or abs(spd - a) / a > 0.005:
            return None
        v2_abs = v2_s / er
        return v1_split_sec + off - v2_abs * spd

    off_half = _xcorr_at_split(v1_dur * 0.6)
    if off_half is None or abs(off_half - primary_offset) < 10:
        return [primary_seg]

    second_offset = off_half

    window_sec = min(600, v1_dur * 0.1)
    scan_step = 300
    last_primary_start = 0.0
    first_secondary_start = v1_dur

    for v1_start in range(int(v1_dur * 0.2), int(v1_dur * 0.7), scan_step):
        v1_end_w = v1_start + window_sec
        a1w = d1[int(v1_start * er):int(min(v1_end_w, v1_dur) * er)]
        v2_est = (v1_start - primary_offset) / a
        v2_s = max(0, int((v2_est - 120) * er))
        v2_e = int((v2_est + window_sec + 120) * er)
        a2w = d2[v2_s:min(v2_e, len(d2))]
        if len(a1w) < er * 60 or len(a2w) < er * 60:
            continue
        off_w, spd_w = _xcorr_window(a1w, a2w)
        if off_w is None or abs(spd_w - a) / a > 0.005:
            continue
        v2_abs = v2_s / er
        abs_off_w = v1_start + off_w - v2_abs * spd_w

        if abs(abs_off_w - primary_offset) < 15:
            last_primary_start = max(last_primary_start, float(v1_start))
        elif abs(abs_off_w - second_offset) < 15:
            first_secondary_start = min(first_secondary_start, float(v1_start))

    boundary = last_primary_start + window_sec
    if first_secondary_start < boundary:
        boundary = (last_primary_start + window_sec + first_secondary_start) / 2
    boundary = max(v1_dur * 0.1, min(v1_dur * 0.9, boundary))

    if first_secondary_start < v1_dur:
        ref_half = 60
        ref_step = 15
        ref_start = max(v1_dur * 0.1, boundary - 600)
        ref_end = min(v1_dur * 0.9, boundary + 600)
        last_pri = ref_start
        first_sec = ref_end

        for v1_c in range(int(ref_start), int(ref_end), ref_step):
            d1s = int(max(0, (v1_c - ref_half)) * er)
            d1e = int(min(v1_dur, (v1_c + ref_half)) * er)
            d1w = d1[d1s:d1e]
            if len(d1w) < er * 20:
                continue
            n_out = len(d1w)
            corrs = []
            for test_off in [primary_offset, second_offset]:
                v2_c = (v1_c - test_off) / a
                v2s = max(0, int((v2_c - ref_half / a) * er))
                v2e = min(len(d2), int((v2_c + ref_half / a) * er))
                d2w = d2[v2s:v2e]
                if len(d2w) < er * 20:
                    corrs.append(-1.0)
                    continue
                d2r = np.interp(np.linspace(0, len(d2w) - 1, n_out),
                                np.arange(len(d2w)), d2w)
                c = float(np.corrcoef(d1w, d2r)[0, 1])
                corrs.append(c if not np.isnan(c) else -1.0)

            if corrs[0] > corrs[1] and corrs[0] > 0.1:
                last_pri = max(last_pri, float(v1_c))
            elif corrs[1] > corrs[0] and corrs[1] > 0.1:
                first_sec = min(first_sec, float(v1_c))

        boundary = (last_pri + first_sec) / 2
        boundary = max(v1_dur * 0.1, min(v1_dur * 0.9, boundary))

    first_offset = primary_offset
    b_samp = int(boundary * er)
    if b_samp > er * min_segment_sec:
        v2_est_end = (boundary - primary_offset) / a
        v2_e = min(len(d2), int((v2_est_end + 300) * er))
        d1_first = d1[:b_samp]
        d2_first = d2[:v2_e]
        off_first, spd_first = _xcorr_window(d1_first, d2_first)
        if off_first is not None and abs(spd_first - a) / a <= 0.005:
            first_offset = off_first

    return [
        {"v1_start": 0.0, "v1_end": boundary,
         "offset": first_offset, "n_inliers": len(inlier_pairs)},
        {"v1_start": boundary, "v1_end": float("inf"),
         "offset": second_offset, "n_inliers": 0},
    ]


def _snap_speed_to_candidate(a, t1_inliers, t2_inliers):
    best_candidate = None
    best_dist = float("inf")
    for sc in SPEED_CANDIDATES:
        dist = abs(a - sc) / sc
        if dist < best_dist:
            best_dist = dist
            best_candidate = sc
    if best_dist > SPEED_SNAP_TOLERANCE or best_candidate is None:
        return a, float(np.mean(t1_inliers - a * t2_inliers))
    a_snapped = best_candidate
    b_snapped = float(np.mean(t1_inliers - a_snapped * t2_inliers))
    return a_snapped, b_snapped


def auto_align_audio(fp1, fp2, track1=0, track2=0,
                      progress_cb=None, cancel=None,
                      vocal_filter=False, fast_decode=False):
    dur1 = get_duration(fp1)
    dur2 = get_duration(fp2)
    hop = AUDIO_HOP_SEC
    max_s = AUDIO_MAX_SAMPLES
    hop1 = dur1 / max_s if (dur1 > 0 and dur1 / hop > max_s) else hop
    hop2 = dur2 / max_s if (dur2 > 0 and dur2 / hop > max_s) else hop

    if progress_cb:
        progress_cb("status", "Decoding V1 + V2 audio...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_decode_full_audio, fp1, track1, AUDIO_SAMPLE_RATE,
                         cancel, fast_decode=fast_decode)
        f2 = pool.submit(_decode_full_audio, fp2, track2, AUDIO_SAMPLE_RATE,
                         cancel, fast_decode=fast_decode)
        audio1 = f1.result()
        audio2 = f2.result()
    if cancel:
        cancel.check()

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
            f1 = pool.submit(_decode_full_audio, fp1, track1,
                             AUDIO_SAMPLE_RATE, cancel, vocal_filter=True)
            f2 = pool.submit(_decode_full_audio, fp2, track2,
                             AUDIO_SAMPLE_RATE, cancel, vocal_filter=True)
            xcorr_a1 = f1.result()
            xcorr_a2 = f2.result()
        if cancel:
            cancel.check()
    else:
        xcorr_a1 = audio1
        xcorr_a2 = audio2

    ds1, ds_rate = _downsample_audio(xcorr_a1, AUDIO_SAMPLE_RATE)
    ds2, _ = _downsample_audio(xcorr_a2, AUDIO_SAMPLE_RATE)
    del xcorr_a1, xcorr_a2

    if vocal_filter:
        ds1_seg, _ = _downsample_audio(audio1, AUDIO_SAMPLE_RATE)
        ds2_seg, _ = _downsample_audio(audio2, AUDIO_SAMPLE_RATE)
    else:
        ds1_seg, ds2_seg = ds1, ds2

    coarse_offset, xcorr_speed, xcorr_corr = _xcorr_on_downsampled(
        ds1, ds2, ds_rate, SPEED_CANDIDATES)

    if cancel:
        cancel.check()

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
    a, b = _snap_speed_to_candidate(a, t1_inliers, t2_inliers)

    pairs = [(ts1[g[0]], ts2[g[1]], g[2])
             for g, m in zip(good, mask) if m]
    rmean, rmax, rend = _residual_stats(pairs, a, b)

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
        rmean_fb, rmax_fb, rend_fb = _residual_stats(pairs_fb, a_fb, b_fb)
        if ni < 4 or rmean_fb <= rmean:
            a, b = a_fb, b_fb
            rmean, rmax, rend = rmean_fb, rmax_fb, rend_fb

    if progress_cb:
        progress_cb("status", "Checking for content breaks...")
    segments = detect_segments(pairs, xcorr_speed,
                               coarse_offset=coarse_offset,
                               d1=ds1_seg, d2=ds2_seg,
                               effective_rate=ds_rate)

    if segments and len(segments) > 1:
        segments = refine_boundary_visual(
            fp1, fp2, segments, xcorr_speed,
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
                off_s, spd_s, _ = _xcorr_on_downsampled(
                    d1_s, d2_s, ds_rate, SPEED_CANDIDATES)
                if abs(spd_s - xcorr_speed) / xcorr_speed <= 0.005:
                    v2_abs = v2_s / ds_rate
                    segments[si]["offset"] = seg["v1_start"] + off_s - v2_abs * spd_s

    if segments and len(segments) > 1:
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
    }


def _frame_similarity(f1, f2):
    if f1 is None or f2 is None:
        return -1.0
    f1n = f1.ravel() - np.mean(f1)
    f2n = f2.ravel() - np.mean(f2)
    s1, s2 = np.linalg.norm(f1n), np.linalg.norm(f2n)
    if s1 < 1e-6 or s2 < 1e-6:
        return 0.0
    return float(np.dot(f1n, f2n) / (s1 * s2))


def refine_boundary_visual(v1_path, v2_path, segments, speed,
                           progress_cb=None, cancel=None):
    if len(segments) < 2:
        return segments

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
                        f"({format_timestamp(lo)}-{format_timestamp(hi)})...")

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
            f1 = fflib.extract_frame(v1_path, mid)
            f2 = fflib.extract_frame(v2_path, v2_t)
            sim = _frame_similarity(f1, f2)
            if sim > 0.5:
                lo = mid
            else:
                hi = mid

        new_boundary = (lo + hi) / 2
        refined[si] = dict(refined[si], v1_end=new_boundary)
        refined[si + 1] = dict(refined[si + 1], v1_start=new_boundary)

    return refined


from merger import (find_ffmpeg_binary, merge_with_ffmpeg, remux_with_ffmpeg)
