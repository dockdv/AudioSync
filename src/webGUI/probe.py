#!/usr/bin/env python3

import os
import fflib

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
            "fflib": (True,
                      f"fflib {ver}" + (f", {ffmpeg_ver}" if ffmpeg_ver else ""),
                      "")
        }
    except Exception as e:
        return {"fflib": (False, str(e), "")}


MULTI_AUDIO_CONTAINERS = {
    ".mkv", ".mka", ".mp4", ".m4v", ".mov",
    ".ts", ".mts", ".m2ts", ".webm",
}

def needs_container_change(filepath):
    _, ext = os.path.splitext(filepath)
    ext = ext.lower()
    return ext not in MULTI_AUDIO_CONTAINERS, ext


def probe_full(filepath):
    try:
        info = fflib.probe(filepath)
        streams = info.get("streams", [])
        duration = info.get("duration", 0.0)
        tracks = []
        for i, a in enumerate(info.get("audio", [])):
            lang = a.get("language", "und") or "und"
            codec = a.get("codec", "?")
            ch = a.get("channels", "?")
            sr = a.get("sample_rate", "?")
            lbl = f"Track {i}: [{lang}] {codec}, {ch}ch, {sr}Hz"
            tracks.append({
                "index": i, "stream_index": a.get("stream_index", i),
                "label": lbl, "language": lang,
                "title": a.get("title", ""),
                "codec": codec, "channels": ch, "sample_rate": sr,
            })
        if tracks:
            return tracks, streams, duration, "libav", ""
        return tracks, streams, duration, "libav", "No audio streams found"
    except Exception as e:
        return [], [], 0.0, "none", f"libAV probe error: {e}"

def get_duration(filepath):
    try:
        return fflib.get_duration(filepath)
    except Exception:
        return 0.0

def get_audio_sample_rate(filepath, track_index=0):
    try:
        sr = fflib.get_sample_rate(filepath, track_index)
        return sr if sr > 0 else 48000
    except Exception:
        return 48000
