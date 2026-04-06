#!/usr/bin/env python3

import os
import fflib

LANG_NAMES = {
    "eng":"English","spa":"Spanish","fre":"French","ger":"German","ita":"Italian",
    "por":"Portuguese","rus":"Russian","chi":"Chinese","jpn":"Japanese","kor":"Korean",
    "ara":"Arabic","hin":"Hindi","tur":"Turkish","pol":"Polish","dut":"Dutch",
    "swe":"Swedish","dan":"Danish","nor":"Norwegian","fin":"Finnish","cze":"Czech",
    "gre":"Greek","heb":"Hebrew","tha":"Thai","vie":"Vietnamese","ind":"Indonesian",
    "may":"Malay","rum":"Romanian","hun":"Hungarian","ukr":"Ukrainian","bul":"Bulgarian",
    "hrv":"Croatian","slo":"Slovak","slv":"Slovenian","srp":"Serbian","lit":"Lithuanian",
    "lav":"Latvian","est":"Estonian","cat":"Catalan","per":"Persian","urd":"Urdu",
    "ben":"Bengali","tam":"Tamil","tel":"Telugu","mal":"Malayalam","kan":"Kannada",
}

_LANG_NORMALIZE = {
    "en": "eng", "es": "spa", "fr": "fre", "de": "ger", "it": "ita",
    "pt": "por", "ru": "rus", "zh": "chi", "ja": "jpn", "ko": "kor",
    "ar": "ara", "hi": "hin", "tr": "tur", "pl": "pol", "nl": "dut",
    "sv": "swe", "da": "dan", "no": "nor", "fi": "fin", "cs": "cze",
    "el": "gre", "he": "heb", "th": "tha", "vi": "vie", "id": "ind",
    "ms": "may", "ro": "rum", "hu": "hun", "uk": "ukr", "bg": "bul",
    "hr": "hrv", "sk": "slo", "sl": "slv", "sr": "srp", "lt": "lit",
    "lv": "lav", "et": "est", "ca": "cat", "fa": "per", "ur": "urd",
    "bn": "ben", "ta": "tam", "te": "tel", "ml": "mal", "kn": "kan",
    "fra": "fre", "deu": "ger", "zho": "chi", "nld": "dut", "ces": "cze",
    "ell": "gre", "fas": "per", "ron": "rum", "slk": "slo", "msa": "may",
}


def normalize_language(code):
    if not code:
        return "und"
    code = code.strip().lower()
    return _LANG_NORMALIZE.get(code, code)

ALL_LANGUAGES = [("und", "Undetermined")] + sorted(
    [(code, name) for code, name in LANG_NAMES.items()],
    key=lambda x: x[1]
)


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
            tracks.append({
                "index": i, "stream_index": a.get("stream_index", i),
                "codec_type": "audio",
                "language": lang, "title": a.get("title", ""),
                "codec": codec, "channels": ch, "sample_rate": sr,
                "start_time": a.get("start_time", 0.0),
                "bit_rate": a.get("bit_rate", 0),
            })
        empty_idxs = [s["stream_index"] for s in streams if s.get("empty")]
        warning = ""
        if empty_idxs:
            warning = ("Skipped empty audio track(s) with no packets: "
                       + ", ".join(f"#{i}" for i in empty_idxs))
        if tracks:
            return tracks, streams, duration, "libav", "", warning
        return tracks, streams, duration, "libav", "No audio streams found", warning
    except Exception as e:
        return [], [], 0.0, "none", f"libAV probe error: {e}", ""

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
