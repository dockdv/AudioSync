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

LANG_NAMES = {
    "en":"English","es":"Spanish","fr":"French","de":"German","it":"Italian",
    "pt":"Portuguese","ru":"Russian","zh":"Chinese","ja":"Japanese","ko":"Korean",
    "ar":"Arabic","hi":"Hindi","tr":"Turkish","pl":"Polish","nl":"Dutch",
    "sv":"Swedish","da":"Danish","no":"Norwegian","fi":"Finnish","cs":"Czech",
    "el":"Greek","he":"Hebrew","th":"Thai","vi":"Vietnamese","id":"Indonesian",
    "ms":"Malay","ro":"Romanian","hu":"Hungarian","uk":"Ukrainian","bg":"Bulgarian",
    "hr":"Croatian","sk":"Slovak","sl":"Slovenian","sr":"Serbian","lt":"Lithuanian",
    "lv":"Latvian","et":"Estonian","ca":"Catalan","fa":"Persian","ur":"Urdu",
    "bn":"Bengali","ta":"Tamil","te":"Telugu","ml":"Malayalam","kn":"Kannada",
}

ALL_LANGUAGES = [("und", "Undetermined")] + sorted(
    [(code, name) for code, name in LANG_NAMES.items()],
    key=lambda x: x[1]
)

_ISO639_MAP = {
    "ar":"ara","bn":"ben","bg":"bul","ca":"cat","cs":"ces","da":"dan",
    "de":"deu","el":"ell","en":"eng","es":"spa","et":"est","fa":"fas",
    "fi":"fin","fr":"fra","he":"heb","hi":"hin","hr":"hrv","hu":"hun",
    "id":"ind","it":"ita","ja":"jpn","kn":"kan","ko":"kor","lt":"lit",
    "lv":"lav","ml":"mal","ms":"msa","nl":"nld","no":"nor","pl":"pol",
    "pt":"por","ro":"ron","ru":"rus","sk":"slk","sl":"slv","sr":"srp",
    "sv":"swe","ta":"tam","te":"tel","th":"tha","tr":"tur","uk":"ukr",
    "ur":"urd","vi":"vie","zh":"zho","und":"und",
}

def lang_to_iso639_2(code):
    if not code:
        return code
    if len(code) == 3:
        return code
    return _ISO639_MAP.get(code, code)


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
                                progress_cb=None, cancel=None):
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


def match_fingerprints(fp1, fp2, top_k=MATCH_TOP_K):
    sim = fp1 @ fp2.T
    matches = []
    for i in range(len(fp1)):
        best = np.argsort(sim[i])[-top_k:][::-1]
        for j in best:
            matches.append((i, int(j), float(sim[i][j])))
    return matches


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

    if progress_cb:
        progress_cb("status",
                     f"Audio FP: V1 track {track1} "
                     f"({dur1:.0f}s, hop={hop1:.2f}s)...")
    ts1, f1 = extract_audio_fingerprints(
        fp1, track_index=track1, max_samples=max_s, hop_sec=hop1,
        progress_cb=(lambda c, t: progress_cb("fp", f"V1 audio: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel)
    if cancel:
        cancel.check()
    if progress_cb:
        progress_cb("status",
                     f"Audio FP: V2 track {track2} "
                     f"({dur2:.0f}s, hop={hop2:.2f}s)...")
    ts2, f2 = extract_audio_fingerprints(
        fp2, track_index=track2, max_samples=max_s, hop_sec=hop2,
        progress_cb=(lambda c, t: progress_cb("fp", f"V2 audio: {c}/{t}")
                     if progress_cb else None),
        cancel=cancel)
    if cancel:
        cancel.check()
    if len(f1) < 10 or len(f2) < 10:
        raise RuntimeError(
            f"Not enough audio data (V1: {len(f1)}, V2: {len(f2)})")
    if progress_cb:
        progress_cb("status",
                     f"Matching {len(f1)}x{len(f2)} audio fingerprints...")
    matches = match_fingerprints(f1, f2, top_k=AUDIO_MATCH_TOP_K)
    if cancel:
        cancel.check()

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
        raise RuntimeError(
            f"Only {len(good)} audio matches. "
            f"Try different tracks or visual sync.")

    t1m = np.array([ts1[g[0]] for g in good])
    t2m = np.array([ts2[g[1]] for g in good])
    ah1 = np.median(np.diff(ts1)) if len(ts1) > 1 else hop1
    ah2 = np.median(np.diff(ts2)) if len(ts2) > 1 else hop2
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
            lang = lang_to_iso639_2(meta.get("language")) or ""
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
