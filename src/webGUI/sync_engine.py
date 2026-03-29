#!/usr/bin/env python3

import os
import re
import subprocess
import sys

import numpy as np
import fflib

MATCH_TOP_K = 3
RANSAC_ITERATIONS = 2000
RANSAC_THRESHOLD_SEC = 0.5

AUDIO_SAMPLE_RATE = 8000
AUDIO_WINDOW_SEC = 0.5
AUDIO_HOP_SEC = 0.2
AUDIO_MAX_SAMPLES = 8000
AUDIO_N_BANDS = 40
AUDIO_MATCH_TOP_K = 3
AUDIO_RANSAC_ITERATIONS = 3000
AUDIO_RANSAC_THRESHOLD_SEC = 0.3

AUDIO_N_PEAKS = 5
AUDIO_PEAK_MIN_DISTANCE = 3
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

LANG_NAMES = {
    "eng":"English","spa":"Spanish","fra":"French","deu":"German","ita":"Italian",
    "por":"Portuguese","rus":"Russian","zho":"Chinese","jpn":"Japanese","kor":"Korean",
    "ara":"Arabic","hin":"Hindi","tur":"Turkish","pol":"Polish","nld":"Dutch",
    "swe":"Swedish","dan":"Danish","nor":"Norwegian","fin":"Finnish","ces":"Czech",
    "ell":"Greek","heb":"Hebrew","tha":"Thai","vie":"Vietnamese","ind":"Indonesian",
    "msa":"Malay","ron":"Romanian","hun":"Hungarian","ukr":"Ukrainian","bul":"Bulgarian",
    "hrv":"Croatian","slk":"Slovak","slv":"Slovenian","srp":"Serbian","lit":"Lithuanian",
    "lav":"Latvian","est":"Estonian","cat":"Catalan","fas":"Persian","urd":"Urdu",
    "ben":"Bengali","tam":"Tamil","tel":"Telugu","mal":"Malayalam","kan":"Kannada",
}

ALL_LANGUAGES = [("und", "Undetermined")] + sorted(
    [(code, name) for code, name in LANG_NAMES.items()],
    key=lambda x: x[1]
)



def check_av():
    try:
        ver = fflib.__version__
        libs = fflib.library_versions
        ffmpeg_ver = libs.get("ffmpeg", "")
        return {
            "pyav": (True,
                     f"fflib {ver}" + (f", {ffmpeg_ver}" if ffmpeg_ver else ""),
                     "")
        }
    except Exception as e:
        return {"pyav": (False, str(e), "")}


MULTI_AUDIO_CONTAINERS = {
    ".mkv", ".mka", ".mp4", ".m4v", ".mov",
    ".ts", ".mts", ".m2ts", ".webm",
}

def needs_container_change(filepath):
    _, ext = os.path.splitext(filepath)
    ext = ext.lower()
    return ext not in MULTI_AUDIO_CONTAINERS, ext


def probe_audio_tracks(filepath):
    tracks = []
    handle = None
    try:
        handle = fflib.open_file(filepath)
        info = fflib.probe(handle)
        for i, a in enumerate(info.get("audio", [])):
            lang = a.get("language", "und") or "und"
            codec = a.get("codec", "?")
            ch = a.get("channels", "?")
            sr = a.get("sample_rate", "?")
            lbl = f"Track {i}: [{lang}] {codec}, {ch}ch, {sr}Hz"
            tracks.append({
                "index": i, "stream_index": a.get("index", i),
                "label": lbl, "language": lang,
                "title": "", "detected_lang": None,
            })
        if tracks:
            return tracks, "libav", ""
        return tracks, "libav", "No audio streams found"
    except Exception as e:
        return [], "none", f"libAV probe error: {e}"
    finally:
        if handle:
            fflib.close_file(handle)

def get_duration(filepath):
    handle = None
    try:
        handle = fflib.open_file(filepath)
        return fflib.get_duration(handle)
    except Exception:
        return 0.0
    finally:
        if handle:
            fflib.close_file(handle)

def get_audio_sample_rate(filepath, track_index=0):
    handle = None
    try:
        handle = fflib.open_file(filepath)
        sr = fflib.get_sample_rate(handle, track_index)
        return sr if sr > 0 else 48000
    except Exception:
        return 48000
    finally:
        if handle:
            fflib.close_file(handle)


def format_timestamp(seconds):
    if seconds is None or seconds != seconds:
        return "0:00.000"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"

class CancellableTask:
    def __init__(self):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def is_cancelled(self):
        return self._cancelled

    def check(self):
        if self._cancelled:
            raise CancelledError("Cancelled")


class CancelledError(Exception):
    pass


def extract_audio_fingerprints(filepath, track_index=0,
                                max_samples=AUDIO_MAX_SAMPLES,
                                hop_sec=AUDIO_HOP_SEC,
                                window_sec=AUDIO_WINDOW_SEC,
                                sr=AUDIO_SAMPLE_RATE,
                                progress_cb=None, cancel=None,
                                audio_data=None):
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


def _decode_full_audio(filepath, track_index, sr, cancel=None):
    handle = None
    try:
        handle = fflib.open_file(filepath)
        audio = fflib.decode_audio(handle, track_index, sr)
        if len(audio) == 0:
            raise RuntimeError("No audio data decoded")
        return audio
    finally:
        if handle:
            fflib.close_file(handle)


def extract_peak_fingerprints(filepath, track_index=0,
                              max_samples=AUDIO_MAX_SAMPLES,
                              hop_sec=AUDIO_HOP_SEC,
                              window_sec=AUDIO_WINDOW_SEC,
                              sr=AUDIO_SAMPLE_RATE,
                              n_peaks=AUDIO_N_PEAKS,
                              min_distance=AUDIO_PEAK_MIN_DISTANCE,
                              progress_cb=None, cancel=None,
                              audio_data=None):
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

        # find local maxima
        is_peak = np.zeros(len(spectrum), dtype=bool)
        if len(spectrum) > 2:
            is_peak[1:-1] = ((spectrum[1:-1] > spectrum[:-2]) &
                             (spectrum[1:-1] > spectrum[2:]))

        peak_indices = np.where(is_peak)[0]
        peak_mags = spectrum[peak_indices]

        # sort by magnitude descending, enforce min_distance
        order = np.argsort(peak_mags)[::-1]
        selected = []
        used = set()
        for idx in order:
            bin_pos = peak_indices[idx]
            if any(abs(bin_pos - s) < min_distance for s in used):
                continue
            selected.append(bin_pos)
            used.add(bin_pos)
            if len(selected) >= n_peaks:
                break

        # build sparse fingerprint vector
        fp = np.zeros(n_fft_bins, dtype=np.float32)
        for b in selected:
            fp[b] = np.log1p(spectrum[b])
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
            f"Only {count} peak fingerprints extracted")
    return np.array(timestamps), np.array(fingerprints)


def extract_band_peak_fingerprints(filepath, track_index=0,
                                    max_samples=AUDIO_MAX_SAMPLES,
                                    hop_sec=AUDIO_HOP_SEC,
                                    window_sec=AUDIO_WINDOW_SEC,
                                    sr=AUDIO_SAMPLE_RATE,
                                    n_bands=AUDIO_N_COARSE_BANDS,
                                    n_peaks=AUDIO_COARSE_N_PEAKS,
                                    progress_cb=None, cancel=None,
                                    audio_data=None):
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


def match_fingerprints(fp1, fp2, top_k=MATCH_TOP_K):
    sim = fp1 @ fp2.T
    matches = []
    for i in range(len(fp1)):
        best = np.argsort(sim[i])[-top_k:][::-1]
        for j in best:
            matches.append((i, int(j), float(sim[i][j])))
    return matches


def mutual_nearest_neighbors(matches, n1, n2, top_k=AUDIO_MATCH_TOP_K):
    from collections import defaultdict
    # build reverse map: for each j, find top-k best i by similarity
    reverse = defaultdict(list)
    for i, j, sim in matches:
        reverse[j].append((i, sim))
    reverse_top = {}
    for j, candidates in reverse.items():
        candidates.sort(key=lambda x: x[1], reverse=True)
        reverse_top[j] = {c[0] for c in candidates[:top_k]}
    # keep match only if i is in j's reverse top-k
    filtered = [(i, j, sim) for i, j, sim in matches
                if i in reverse_top.get(j, set())]
    return filtered


def cross_correlation_offset(fp1, ts1, fp2, ts2):
    # compute energy envelopes
    e1 = np.sum(np.abs(fp1), axis=1)
    e2 = np.sum(np.abs(fp2), axis=1)

    # resample to common uniform time grid
    dt1 = np.median(np.diff(ts1)) if len(ts1) > 1 else 0.2
    dt2 = np.median(np.diff(ts2)) if len(ts2) > 1 else 0.2
    dt = min(dt1, dt2)
    t_max = max(ts1[-1], ts2[-1])
    t_grid = np.arange(0, t_max + dt, dt)
    e1_interp = np.interp(t_grid, ts1, e1)
    e2_interp = np.interp(t_grid, ts2, e2)

    # normalize
    e1_interp -= np.mean(e1_interp)
    std1 = np.std(e1_interp)
    if std1 > 0:
        e1_interp /= std1
    e2_interp -= np.mean(e2_interp)
    std2 = np.std(e2_interp)
    if std2 > 0:
        e2_interp /= std2

    # zero-pad to next power of 2
    n = len(e1_interp) + len(e2_interp) - 1
    nfft = 1
    while nfft < n:
        nfft <<= 1
    e1_pad = np.zeros(nfft)
    e2_pad = np.zeros(nfft)
    e1_pad[:len(e1_interp)] = e1_interp
    e2_pad[:len(e2_interp)] = e2_interp

    # FFT cross-correlation
    xcorr = np.fft.irfft(np.fft.rfft(e1_pad) * np.conj(np.fft.rfft(e2_pad)),
                         n=nfft)
    peak_idx = int(np.argmax(xcorr))
    if peak_idx > nfft // 2:
        peak_idx -= nfft
    return float(peak_idx * dt)


XCORR_DOWNSAMPLE_RATE = 100

def cross_correlation_with_speed(audio1, audio2, sr=AUDIO_SAMPLE_RATE):
    # downsample raw audio to ~100Hz by block-averaging
    block = max(1, sr // XCORR_DOWNSAMPLE_RATE)
    effective_rate = sr / block

    def downsample(audio):
        n = len(audio) - len(audio) % block
        return np.mean(np.abs(audio[:n]).reshape(-1, block), axis=1)

    d1 = downsample(audio1)
    d2 = downsample(audio2)

    best_corr = -np.inf
    best_offset = 0.0
    best_speed = 1.0

    for speed in SPEED_CANDIDATES:
        # resample d2 to simulate speed change
        n2_stretched = int(len(d2) * speed)
        if n2_stretched < 2:
            continue
        d2s = np.interp(
            np.linspace(0, len(d2) - 1, n2_stretched),
            np.arange(len(d2)), d2)

        # normalize
        d1n = d1 - np.mean(d1)
        std1 = np.std(d1n)
        if std1 > 0:
            d1n /= std1
        d2n = d2s - np.mean(d2s)
        std2 = np.std(d2n)
        if std2 > 0:
            d2n /= std2

        # FFT cross-correlation
        n = len(d1n) + len(d2n) - 1
        nfft = 1
        while nfft < n:
            nfft <<= 1
        d1_pad = np.zeros(nfft)
        d2_pad = np.zeros(nfft)
        d1_pad[:len(d1n)] = d1n
        d2_pad[:len(d2n)] = d2n

        xcorr = np.fft.irfft(
            np.fft.rfft(d1_pad) * np.conj(np.fft.rfft(d2_pad)),
            n=nfft)
        peak_val = float(np.max(xcorr))
        peak_idx = int(np.argmax(xcorr))
        if peak_idx > nfft // 2:
            peak_idx -= nfft

        if peak_val > best_corr:
            best_corr = peak_val
            best_offset = float(peak_idx / effective_rate)
            best_speed = speed

    return best_offset, best_speed, best_corr


def filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                             window_sec=AUDIO_XCORR_WINDOW_SEC,
                             speed=1.0):
    filtered = []
    for i, j, sim in matches:
        predicted_t1 = speed * ts2[j] + coarse_offset
        if abs(ts1[i] - predicted_t1) <= window_sec:
            filtered.append((i, j, sim))
    return filtered


def ransac_linear_fit(t1, t2, n_iter=RANSAC_ITERATIONS,
                       threshold=RANSAC_THRESHOLD_SEC, cancel=None):
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


def auto_align_audio(fp1, fp2, track1=0, track2=0,
                      progress_cb=None, cancel=None):
    dur1 = get_duration(fp1)
    dur2 = get_duration(fp2)
    hop = AUDIO_HOP_SEC
    max_s = AUDIO_MAX_SAMPLES
    hop1 = dur1 / max_s if (dur1 > 0 and dur1 / hop > max_s) else hop
    hop2 = dur2 / max_s if (dur2 > 0 and dur2 / hop > max_s) else hop

    # decode audio once per file
    if progress_cb:
        progress_cb("status", "Decoding V1 audio...")
    audio1 = _decode_full_audio(fp1, track1, AUDIO_SAMPLE_RATE, cancel)
    if cancel:
        cancel.check()
    if progress_cb:
        progress_cb("status", "Decoding V2 audio...")
    audio2 = _decode_full_audio(fp2, track2, AUDIO_SAMPLE_RATE, cancel)
    if cancel:
        cancel.check()

    # extract energy-band fingerprints (used for cross-correlation envelope)
    if progress_cb:
        progress_cb("status",
                     f"Energy FP: V1 track {track1} "
                     f"({dur1:.0f}s, hop={hop1:.2f}s)...")
    ts1, f1_energy = extract_audio_fingerprints(
        fp1, track_index=track1, max_samples=max_s, hop_sec=hop1,
        progress_cb=(lambda c, t: progress_cb("fp", f"V1 energy: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel, audio_data=audio1)
    if cancel:
        cancel.check()
    if progress_cb:
        progress_cb("status",
                     f"Energy FP: V2 track {track2} "
                     f"({dur2:.0f}s, hop={hop2:.2f}s)...")
    ts2, f2_energy = extract_audio_fingerprints(
        fp2, track_index=track2, max_samples=max_s, hop_sec=hop2,
        progress_cb=(lambda c, t: progress_cb("fp", f"V2 energy: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel, audio_data=audio2)
    if cancel:
        cancel.check()

    # extract band-grouped peak fingerprints (used for matching)
    if progress_cb:
        progress_cb("status", "Band-peak FP: V1...")
    _, f1_peak = extract_band_peak_fingerprints(
        fp1, track_index=track1, max_samples=max_s, hop_sec=hop1,
        progress_cb=(lambda c, t: progress_cb("fp", f"V1 band-peak: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel, audio_data=audio1)
    if cancel:
        cancel.check()
    if progress_cb:
        progress_cb("status", "Band-peak FP: V2...")
    _, f2_peak = extract_band_peak_fingerprints(
        fp2, track_index=track2, max_samples=max_s, hop_sec=hop2,
        progress_cb=(lambda c, t: progress_cb("fp", f"V2 band-peak: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel, audio_data=audio2)
    if cancel:
        cancel.check()

    if len(f1_energy) < 10 or len(f2_energy) < 10:
        raise RuntimeError(
            f"Not enough audio data (V1: {len(f1_energy)}, "
            f"V2: {len(f2_energy)})")

    # cross-correlation coarse offset + speed estimate (using raw audio)
    if progress_cb:
        progress_cb("status", "Computing coarse offset + speed (cross-correlation)...")
    coarse_offset, xcorr_speed, xcorr_corr = cross_correlation_with_speed(
        audio1, audio2, sr=AUDIO_SAMPLE_RATE)
    if cancel:
        cancel.check()

    # match using peak fingerprints (more distinctive)
    if progress_cb:
        progress_cb("status",
                     f"Matching {len(f1_peak)}x{len(f2_peak)} "
                     f"peak fingerprints...")
    matches = match_fingerprints(f1_peak, f2_peak, top_k=AUDIO_MATCH_TOP_K)
    if cancel:
        cancel.check()

    # mutual nearest neighbor filtering
    matches = mutual_nearest_neighbors(matches, len(f1_peak), len(f2_peak),
                                       top_k=AUDIO_MATCH_TOP_K)

    # filter matches by coarse offset and speed
    filtered = filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                                        speed=xcorr_speed)
    if len(filtered) >= 20:
        matches = filtered
    else:
        # widen window if too few matches
        filtered = filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                                            window_sec=30.0, speed=xcorr_speed)
        if len(filtered) >= 20:
            matches = filtered
        # else keep all MNN matches

    # fallback: if peak matching yielded too few, use energy-band matching
    if len(matches) < 20:
        if progress_cb:
            progress_cb("status", "Falling back to energy-band matching...")
        matches = match_fingerprints(f1_energy, f2_energy,
                                     top_k=AUDIO_MATCH_TOP_K)
        matches = mutual_nearest_neighbors(matches, len(f1_energy),
                                           len(f2_energy),
                                           top_k=AUDIO_MATCH_TOP_K)
        filtered = filter_matches_by_offset(matches, ts1, ts2, coarse_offset,
                                            speed=xcorr_speed)
        if len(filtered) >= 20:
            matches = filtered

    # adaptive similarity threshold
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
        # fallback to cross-correlation result
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
    atempo = 1.0 / a if abs(a) > 1e-9 else 1.0
    pairs = [(ts1[g[0]], ts2[g[1]], g[2])
             for g, m in zip(good, mask) if m]
    rmean, rmax, rend = _residual_stats(pairs, a, b)

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
    }


def find_ffmpeg_binary():
    path = fflib.get_paths().get("ffmpeg", "")
    return path if path and os.path.isfile(path) else None


def merge_with_ffmpeg(v1_path, v2_path, out_path, atempo, offset,
                      v1_n_audio, v2_indices, v1_duration,
                      ffmpeg_path=None, metadata_args=None,
                      progress_cb=None, cancel=None):
    if not ffmpeg_path:
        ffmpeg_path = find_ffmpeg_binary()
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg binary not found")

    v1_sr = get_audio_sample_rate(v1_path, 0)
    v1_dur = v1_duration or get_duration(v1_path)

    cmd = [ffmpeg_path, "-y", "-hide_banner"]

    cmd += ["-i", v1_path]
    if abs(offset) > 0.001:
        cmd += ["-itsoffset", f"{offset:.6f}"]
    cmd += ["-i", v2_path]

    cmd += ["-map", "0"]

    for tidx in v2_indices:
        cmd += ["-map", f"1:a:{tidx}"]

    cmd += ["-c", "copy"]

    for i, tidx in enumerate(v2_indices):
        out_audio_idx = v1_n_audio + i
        filters = []
        if abs(atempo - 1.0) > 0.0001:
            remaining = atempo
            parts = []
            while remaining > 100.0:
                parts.append("atempo=100.0")
                remaining /= 100.0
            while remaining < 0.5:
                parts.append("atempo=0.5")
                remaining /= 0.5
            parts.append(f"atempo={remaining:.6f}")
            filters.extend(parts)
        filters.append(f"aresample={v1_sr}")
        filter_str = ",".join(filters)
        cmd += [f"-filter:a:{out_audio_idx}", filter_str]
        cmd += [f"-c:a:{out_audio_idx}", "aac",
                f"-b:a:{out_audio_idx}", "192k"]

    if metadata_args:
        for i, meta in enumerate(metadata_args):
            lang = meta.get("language") or ""
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
            cmd += [f"-metadata:s:a:{i}", f"title={title}"]

    if v1_dur > 0:
        cmd += ["-t", f"{v1_dur:.6f}"]

    cmd += [out_path]

    if progress_cb:
        progress_cb("status", f"Running: {os.path.basename(ffmpeg_path)}")

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, errors="replace",
        creationflags=creationflags,
    )

    time_re = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
    try:
        for line in proc.stderr:
            if cancel and cancel.is_cancelled:
                proc.kill()
                raise CancelledError("Cancelled")
            m = time_re.search(line)
            if m and progress_cb and v1_dur > 0:
                h, mi, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                pos = h * 3600 + mi * 60 + s + cs / 100.0
                pct = min(99, int(pos / v1_dur * 100))
                progress_cb("progress", f"mux:{pct}")

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
    except CancelledError:
        proc.wait()
        raise

    if progress_cb:
        progress_cb("progress", "mux:100")
        progress_cb("status", "Done!")
