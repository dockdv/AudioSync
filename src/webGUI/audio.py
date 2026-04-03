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

AUDIO_N_MELS = 128
DTW_BAND_SEC = 60.0
DTW_MIN_SEGMENT_SEC = 120.0


def compute_lufs(samples, sr):
    """Compute integrated LUFS (EBU R128) from mono float32 samples."""
    if len(samples) == 0:
        return None
    filtered = samples.astype(np.float64)
    # Simple DC removal / high-pass via first-order difference filter
    if len(filtered) > 1:
        filtered = np.diff(filtered, prepend=filtered[0])
    # 400ms gating blocks with 75% overlap (100ms hop)
    block_len = int(sr * 0.4)
    hop = int(sr * 0.1)
    if block_len < 1:
        return None
    n_blocks = max(0, (len(filtered) - block_len) // hop + 1)
    if n_blocks == 0:
        return None
    # Mean square per block
    ms = np.empty(n_blocks, dtype=np.float64)
    for i in range(n_blocks):
        start = i * hop
        block = filtered[start:start + block_len]
        ms[i] = np.mean(block ** 2)
    # Absolute gate: -70 LUFS
    abs_gate = 10 ** ((-70 + 0.691) / 10)
    above = ms[ms > abs_gate]
    if len(above) == 0:
        return None
    # Relative gate: -10 LU below ungated mean
    ungated_mean = np.mean(above)
    rel_gate = ungated_mean * 10 ** (-10 / 10)
    final = above[above > rel_gate]
    if len(final) == 0:
        return None
    lufs = -0.691 + 10 * np.log10(np.mean(final))
    return float(lufs)


def decode_full_audio(filepath, track_index, sr, cancel=None,
                      vocal_filter=False, duration=0):
    audio, warnings = fflib.decode_audio(filepath, track_index, sr,
                                         vocal_filter=vocal_filter,
                                         cancel=cancel)
    if len(audio) == 0:
        raise RuntimeError("No audio data decoded")
    decoded_dur = len(audio) / sr
    expected_dur = duration if duration > 0 else get_duration(filepath)
    msgs = []
    if warnings:
        msgs.append(f"FFmpeg: {warnings}")
    if expected_dur > 0 and decoded_dur < expected_dur - 30:
        msgs.append(f"Decoded {decoded_dur:.1f}s of expected {expected_dur:.1f}s "
                     f"({expected_dur - decoded_dur:.1f}s missing)")
    return audio, msgs


def _extract_fingerprints(filepath, track_index, max_samples, hop_sec,
                          window_sec, sr, frame_fn, label,
                          progress_cb, cancel, audio_data, duration):
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
        fp = frame_fn(spectrum)
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
        raise RuntimeError(f"Only {count} {label} fingerprints extracted")
    return np.array(timestamps), np.array(fingerprints)


def extract_audio_fingerprints(filepath, track_index=0,
                                max_samples=AUDIO_MAX_SAMPLES,
                                hop_sec=AUDIO_HOP_SEC,
                                window_sec=AUDIO_WINDOW_SEC,
                                sr=AUDIO_SAMPLE_RATE,
                                progress_cb=None, cancel=None,
                                audio_data=None, duration=0):
    window_samples = int(window_sec * sr)
    n_fft_bins = window_samples // 2 + 1
    n_bands = AUDIO_N_BANDS
    min_bin = max(1, int(60.0 / (sr / window_samples)))
    band_edges = np.logspace(
        np.log10(min_bin), np.log10(n_fft_bins - 1),
        n_bands + 1
    ).astype(int)
    band_edges = np.clip(band_edges, 0, n_fft_bins - 1)
    safe_edges = band_edges.copy()
    safe_edges[1:] = np.maximum(safe_edges[1:], safe_edges[:-1] + 1)
    band_widths = np.diff(safe_edges).astype(np.float32)

    def frame_fn(spectrum):
        band_sums = np.add.reduceat(spectrum, safe_edges[:-1])
        return np.log1p(band_sums / band_widths).astype(np.float32)

    return _extract_fingerprints(
        filepath, track_index, max_samples, hop_sec, window_sec, sr,
        frame_fn, "energy", progress_cb, cancel, audio_data, duration)


def build_mel_filterbank(n_fft, sr, n_mels=AUDIO_N_MELS,
                         fmin=60.0, fmax=None):
    if fmax is None:
        fmax = sr / 2.0
    mel_min = 2595.0 * np.log10(1.0 + fmin / 700.0)
    mel_max = 2595.0 * np.log10(1.0 + fmax / 700.0)
    mels = np.linspace(mel_min, mel_max, n_mels + 2)
    hz = 700.0 * (10.0 ** (mels / 2595.0) - 1.0)
    bins = np.floor((n_fft - 1) * 2 * hz / sr).astype(int)
    bins = np.clip(bins, 0, n_fft - 1)

    fb = np.zeros((n_mels, n_fft), dtype=np.float32)
    for m in range(n_mels):
        lo, mid, hi = bins[m], bins[m + 1], bins[m + 2]
        if mid == lo:
            mid = lo + 1
        if hi == mid:
            hi = mid + 1
        for k in range(lo, mid):
            fb[m, k] = (k - lo) / (mid - lo)
        for k in range(mid, hi):
            fb[m, k] = (hi - k) / (hi - mid)
    return fb


def extract_mel_fingerprints(filepath, track_index=0,
                              max_samples=AUDIO_MAX_SAMPLES,
                              hop_sec=AUDIO_HOP_SEC,
                              window_sec=AUDIO_WINDOW_SEC,
                              sr=AUDIO_SAMPLE_RATE,
                              n_mels=AUDIO_N_MELS,
                              progress_cb=None, cancel=None,
                              audio_data=None, duration=0):
    window_samples = int(window_sec * sr)
    n_fft_bins = window_samples // 2 + 1
    mel_fb = build_mel_filterbank(n_fft_bins, sr, n_mels)

    def frame_fn(spectrum):
        return np.log1p(mel_fb @ spectrum).astype(np.float32)

    return _extract_fingerprints(
        filepath, track_index, max_samples, hop_sec, window_sec, sr,
        frame_fn, "mel", progress_cb, cancel, audio_data, duration)


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
        found_alt = False
        if alt_offsets:
            for alt_off, alt_spd, alt_corr in alt_offsets:
                if abs(alt_off - primary_offset) < 10:
                    continue
                if alt_corr < 0.1:
                    continue
                v1_s = int(v1_dur * 0.6 * er)
                v2_est = (v1_dur * 0.6 - alt_off) / a
                v2_s = max(0, int((v2_est - 300) * er))
                a1v = d1[v1_s:]
                a2v = d2[v2_s:]
                if len(a1v) >= er * min_segment_sec and len(a2v) >= er * min_segment_sec:
                    off_v, spd_v = _xcorr_window(a1v, a2v)
                    if off_v is not None and abs(spd_v - a) / a <= 0.005:
                        v2_abs = v2_s / er
                        verified_off = v1_dur * 0.6 + off_v - v2_abs * spd_v
                        if abs(verified_off - primary_offset) >= 10:
                            off_half = verified_off
                            found_alt = True
                            break
        if not found_alt:
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


def dtw_align(fp1, fp2, ts1, ts2, coarse_offset, coarse_speed,
              band_sec=DTW_BAND_SEC, cancel=None):
    n1, n2 = len(fp1), len(fp2)
    if n1 == 0 or n2 == 0:
        return np.empty((0, 2), dtype=np.float64)

    hop1 = ts1[1] - ts1[0] if n1 > 1 else 0.2
    hop2 = ts2[1] - ts2[0] if n2 > 1 else 0.2
    w1 = int(band_sec / hop1) if hop1 > 0 else 300
    w2 = int(band_sec / hop2) if hop2 > 0 else 300
    band_w = max(w1, w2, 10)

    def _expected_i(j):
        t2_sec = ts2[j] if j < n2 else ts2[-1]
        t1_sec = coarse_speed * t2_sec + coarse_offset
        return int(round((t1_sec - ts1[0]) / hop1)) if hop1 > 0 else j

    INF = np.float64(1e18)
    cost_band = np.full((n2, 2 * band_w + 1), INF, dtype=np.float64)
    dp = np.full((n2, 2 * band_w + 1), INF, dtype=np.float64)
    trace = np.zeros((n2, 2 * band_w + 1), dtype=np.int8)

    for j in range(n2):
        if cancel and j % 500 == 0:
            cancel.check()
        center = _expected_i(j)
        i_lo = max(0, center - band_w)
        i_hi = min(n1, center + band_w + 1)
        if i_lo >= n1 or i_hi <= 0:
            continue
        fp2_row = fp2[j]
        for bi in range(2 * band_w + 1):
            i = center - band_w + bi
            if i < 0 or i >= n1:
                continue
            cost_band[j, bi] = 1.0 - float(np.dot(fp1[i], fp2_row))

    center0 = _expected_i(0)
    for bi in range(2 * band_w + 1):
        i = center0 - band_w + bi
        if 0 <= i < n1:
            dp[0, bi] = cost_band[0, bi]

    for j in range(1, n2):
        if cancel and j % 500 == 0:
            cancel.check()
        center_prev = _expected_i(j - 1)
        center_cur = _expected_i(j)
        shift = center_cur - center_prev

        for bi in range(2 * band_w + 1):
            if cost_band[j, bi] >= INF:
                continue
            best = INF
            best_dir = 0

            bi_prev_diag = bi + shift - 1
            if 0 <= bi_prev_diag < 2 * band_w + 1:
                v = dp[j - 1, bi_prev_diag]
                if v < best:
                    best = v
                    best_dir = 0

            bi_prev_horiz = bi + shift
            if 0 <= bi_prev_horiz < 2 * band_w + 1:
                v = dp[j - 1, bi_prev_horiz]
                if v < best:
                    best = v
                    best_dir = 1

            if bi > 0 and dp[j, bi - 1] < best:
                best = dp[j, bi - 1]
                best_dir = 2

            dp[j, bi] = cost_band[j, bi] + (best if best < INF else 0.0)
            trace[j, bi] = best_dir

    best_bi = -1
    best_val = INF
    for bi in range(2 * band_w + 1):
        if dp[n2 - 1, bi] < best_val:
            best_val = dp[n2 - 1, bi]
            best_bi = bi

    if best_bi < 0:
        return np.column_stack([ts1[:min(n1, n2)], ts2[:min(n1, n2)]])

    path = []
    j, bi = n2 - 1, best_bi
    while j >= 0:
        center = _expected_i(j)
        i = center - band_w + bi
        i = max(0, min(n1 - 1, i))
        path.append((float(ts1[i]), float(ts2[j])))
        if j == 0:
            break
        d = trace[j, bi]
        shift_back = _expected_i(j) - _expected_i(j - 1)
        if d == 0:
            bi = bi + shift_back - 1
            j -= 1
        elif d == 1:
            bi = bi + shift_back
            j -= 1
        else:
            bi -= 1
        bi = max(0, min(2 * band_w, bi))

    path.reverse()
    return np.array(path, dtype=np.float64)


def dtw_path_to_segments(warp_path, speed, min_segment_sec=DTW_MIN_SEGMENT_SEC):
    if len(warp_path) < 2:
        off = warp_path[0, 0] - speed * warp_path[0, 1] if len(warp_path) == 1 else 0.0
        return [{"v1_start": 0.0, "v1_end": float("inf"),
                 "offset": float(off), "n_inliers": len(warp_path)}]

    offsets = warp_path[:, 0] - speed * warp_path[:, 1]

    boundaries = [0]
    window = max(5, len(offsets) // 200)
    smoothed = np.convolve(offsets, np.ones(window) / window, mode='same')

    for k in range(window, len(smoothed) - window):
        if abs(smoothed[k] - smoothed[k - window]) > 2.0:
            if not boundaries or k - boundaries[-1] > window:
                boundaries.append(k)
    boundaries.append(len(offsets))

    raw_segments = []
    for s in range(len(boundaries) - 1):
        lo, hi = boundaries[s], boundaries[s + 1]
        seg_offsets = offsets[lo:hi]
        med_off = float(np.median(seg_offsets))
        v1_start = float(warp_path[lo, 0])
        v1_end = float(warp_path[min(hi, len(warp_path) - 1), 0])
        raw_segments.append({
            "v1_start": v1_start, "v1_end": v1_end,
            "offset": med_off, "n_inliers": hi - lo,
        })

    if len(raw_segments) <= 1:
        return [{"v1_start": 0.0, "v1_end": float("inf"),
                 "offset": raw_segments[0]["offset"],
                 "n_inliers": raw_segments[0]["n_inliers"]}]

    merged = [raw_segments[0]]
    for seg in raw_segments[1:]:
        prev = merged[-1]
        seg_dur = seg["v1_end"] - seg["v1_start"]
        if seg_dur < min_segment_sec:
            prev["v1_end"] = seg["v1_end"]
            prev["n_inliers"] += seg["n_inliers"]
        else:
            merged.append(seg)

    while len(merged) > 1:
        first_dur = merged[0]["v1_end"] - merged[0]["v1_start"]
        if first_dur < min_segment_sec:
            merged[1]["v1_start"] = merged[0]["v1_start"]
            merged[1]["n_inliers"] += merged[0]["n_inliers"]
            merged.pop(0)
        else:
            break

    merged[0]["v1_start"] = 0.0
    merged[-1]["v1_end"] = float("inf")
    return merged
