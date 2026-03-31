#!/usr/bin/env python3

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

XCORR_DOWNSAMPLE_RATE = 100

SPEED_SNAP_TOLERANCE = 0.005


def decode_full_audio(filepath, track_index, sr, cancel=None,
                      vocal_filter=False):
    audio, warnings = fflib.decode_audio(filepath, track_index, sr,
                                         vocal_filter=vocal_filter,
                                         cancel=cancel)
    if len(audio) == 0:
        raise RuntimeError("No audio data decoded")
    decoded_dur = len(audio) / sr
    expected_dur = get_duration(filepath)
    msgs = []
    if warnings:
        msgs.append(f"FFmpeg: {warnings}")
    if expected_dur > 0 and decoded_dur < expected_dur - 30:
        msgs.append(f"Decoded {decoded_dur:.1f}s of expected {expected_dur:.1f}s "
                     f"({expected_dur - decoded_dur:.1f}s missing)")
    return audio, msgs


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

    safe_edges = band_edges.copy()
    safe_edges[1:] = np.maximum(safe_edges[1:], safe_edges[:-1] + 1)
    band_widths = np.diff(safe_edges).astype(np.float32)

    if audio_data is not None:
        audio = audio_data
    else:
        audio, _ = decode_full_audio(filepath, track_index, sr, cancel)
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
        band_sums = np.add.reduceat(spectrum, safe_edges[:-1])
        fp = np.log1p(band_sums / band_widths).astype(np.float32)
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
        audio, _ = decode_full_audio(filepath, track_index, sr, cancel)
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
        if len(peak_indices) > 0:
            bands = np.searchsorted(band_edges[1:], peak_indices)
            valid = bands < n_bands
            np.maximum.at(fp, bands[valid], np.log1p(peak_mags[valid]))

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
    n1, n2 = sim.shape
    if n2 <= top_k:
        top_indices = np.argsort(sim, axis=1)[:, ::-1]
    else:
        raw = np.argpartition(sim, -top_k, axis=1)[:, -top_k:]
        row_idx = np.arange(n1)[:, None]
        order = np.argsort(sim[row_idx, raw], axis=1)[:, ::-1]
        top_indices = raw[row_idx, order]
    k = top_indices.shape[1]
    i_arr = np.repeat(np.arange(n1), k)
    j_arr = top_indices.ravel()
    s_arr = sim[i_arr, j_arr]
    matches = list(zip(i_arr.tolist(), j_arr.tolist(), s_arr.tolist()))
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


def downsample_audio(audio, sr=AUDIO_SAMPLE_RATE):
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


def residual_stats(pairs, a, b):
    if not pairs:
        return 0, 0, 0
    inlier_t1 = np.array([p[0] for p in pairs])
    inlier_t2 = np.array([p[1] for p in pairs])
    residuals = np.abs(inlier_t1 - (a * inlier_t2 + b))
    return (float(np.mean(residuals)),
            float(np.max(residuals)),
            float(residuals[-1]) if len(residuals) > 0 else 0)


def _find_xcorr_peaks(xcorr, nfft, effective_rate, n_peaks=3, min_sep_sec=5.0):
    min_sep = int(min_sep_sec * effective_rate)
    peaks = []
    xcorr_copy = xcorr.copy()
    for _ in range(n_peaks):
        pi = int(np.argmax(xcorr_copy))
        pv = float(xcorr_copy[pi])
        if pv <= 0:
            break
        lag = pi if pi <= nfft // 2 else pi - nfft
        peaks.append((pv, float(lag / effective_rate)))
        lo = max(0, pi - min_sep)
        hi = min(len(xcorr_copy), pi + min_sep + 1)
        xcorr_copy[lo:hi] = 0
    return peaks


def xcorr_on_downsampled(d1, d2, effective_rate, speed_candidates,
                         return_alt_offsets=False):
    best_corr = -np.inf
    best_offset = 0.0
    best_speed = 1.0
    all_peaks = [] if return_alt_offsets else None

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

        if return_alt_offsets:
            peaks = _find_xcorr_peaks(xcorr, nfft, effective_rate)
            for peak_v, peak_off in peaks:
                norm_pv = peak_v / overlap if overlap > 0 else 0.0
                all_peaks.append((norm_pv, peak_off, speed))

    if return_alt_offsets:
        all_peaks.sort(key=lambda x: x[0], reverse=True)
        alt = [(off, spd, corr) for corr, off, spd in all_peaks
               if abs(off - best_offset) > 5.0 or abs(spd - best_speed) > 0.001]
        return best_offset, best_speed, best_corr, alt
    return best_offset, best_speed, best_corr


def detect_segments(inlier_pairs, a, coarse_offset=0.0,
                    d1=None, d2=None, effective_rate=100.0,
                    min_segment_sec=300, alt_offsets=None):
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
        return xcorr_on_downsampled(
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
        if alt_offsets:
            for alt_off, alt_spd, alt_corr in alt_offsets:
                if abs(alt_off - primary_offset) >= 10:
                    off_half = alt_off
                    break
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


def snap_speed_to_candidate(a, t1_inliers, t2_inliers):
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
