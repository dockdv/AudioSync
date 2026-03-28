import json
import os
import platform
import shutil
import subprocess
import sys

import numpy as np

__version__ = "3.0.0-cli"


def _find_binary(name):
    env_key = f"{name.upper()}_PATH"
    env_val = os.environ.get(env_key, "").strip()
    if env_val and os.path.isfile(env_val):
        return env_val

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "x64"
    plat = "win" if sys.platform == "win32" else "linux"
    suffixes = [f"{name}.exe", name] if sys.platform == "win32" else [name]
    for d in [os.path.join(base, "ffmpeg-lib", plat, arch),
              os.path.join(base, "ffmpeg-lib", arch)]:
        for s in suffixes:
            p = os.path.join(d, s)
            if os.path.isfile(p):
                return os.path.abspath(p)

    found = shutil.which(name)
    if found:
        return found

    return None


_ffmpeg = _find_binary("ffmpeg")
_ffprobe = _find_binary("ffprobe")


def get_paths():
    return {"ffmpeg": _ffmpeg or "", "ffprobe": _ffprobe or ""}

_creationflags = 0
if sys.platform == "win32":
    _creationflags = subprocess.CREATE_NO_WINDOW


def _run(cmd, check=True, timeout=30):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=_creationflags)
    if check and r.returncode != 0:
        stderr = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{cmd[0]} failed (code {r.returncode}): {stderr}")
    return r.stdout


def _require_ffprobe():
    if not _ffprobe:
        raise RuntimeError("ffprobe not found. Set FFPROBE_PATH or install ffmpeg.")
    return _ffprobe


def _require_ffmpeg():
    if not _ffmpeg:
        raise RuntimeError("ffmpeg not found. Set FFMPEG_PATH or install ffmpeg.")
    return _ffmpeg


def open_file(path):
    if isinstance(path, bytes):
        path = path.decode("utf-8")
    if not os.path.isfile(path):
        raise RuntimeError(f"File not found: {path}")
    return path


def close_file(handle):
    pass


def probe(handle):
    fp = _require_ffprobe()
    raw = _run([fp, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", handle], timeout=60)
    data = json.loads(raw)

    audio = []
    video = []
    audio_idx = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            audio.append({
                "index": audio_idx,
                "stream_index": s.get("index", audio_idx),
                "codec": s.get("codec_name", "?"),
                "channels": int(s.get("channels", 0)),
                "sample_rate": int(s.get("sample_rate", 0)),
                "language": (s.get("tags") or {}).get("language", "und"),
            })
            audio_idx += 1
        elif s.get("codec_type") == "video":
            video.append({
                "index": s.get("index", 0),
                "codec": s.get("codec_name", "?"),
                "width": int(s.get("width", 0)),
                "height": int(s.get("height", 0)),
            })

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0))

    return {"audio": audio, "video": video, "duration": duration}


def get_duration(handle):
    fp = _require_ffprobe()
    raw = _run([fp, "-v", "quiet", "-print_format", "json",
                "-show_format", handle], timeout=30)
    data = json.loads(raw)
    return float(data.get("format", {}).get("duration", 0))


def get_sample_rate(handle, audio_track_index):
    info = probe(handle)
    tracks = info.get("audio", [])
    if audio_track_index < len(tracks):
        sr = tracks[audio_track_index].get("sample_rate", 0)
        return sr if sr > 0 else 48000
    return 48000


def decode_audio(handle, audio_track_index, target_sr):
    ff = _require_ffmpeg()
    cmd = [ff, "-v", "quiet",
           "-i", handle,
           "-map", f"0:a:{audio_track_index}",
           "-ac", "1",
           "-ar", str(target_sr),
           "-f", "f32le",
           "-acodec", "pcm_f32le",
           "pipe:1"]
    raw = _run(cmd, timeout=600)
    if len(raw) == 0:
        return np.array([], dtype=np.float32)
    return np.frombuffer(raw, dtype=np.float32).copy()


def version_info():
    ff = _find_binary("ffmpeg")
    if not ff:
        return {}
    try:
        raw = _run([ff, "-version"], timeout=10)
        first_line = raw.decode("utf-8", errors="replace").split("\n")[0]
        return {"ffmpeg": first_line.strip()}
    except Exception:
        return {}


library_versions = {}
try:
    library_versions = version_info()
except Exception:
    pass
