import json
import os
import platform
import shutil
import subprocess
import sys
import threading

import numpy as np

__version__ = "3.0.0-cli"


def _find_binary(name):
    env_key = f"{name.upper()}_PATH"
    env_val = os.environ.get(env_key, "").strip()
    if env_val and os.path.isfile(env_val):
        return env_val

    script_dir = os.path.dirname(os.path.abspath(__file__))
    base = os.path.join(script_dir, "..", "..")
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "x64"
    plat = "win" if sys.platform == "win32" else "linux"
    suffixes = [f"{name}.exe", name] if sys.platform == "win32" else [name]
    for d in [script_dir,
              os.path.join(base, "ffmpeg-lib", plat, arch),
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


class CancelledError(Exception):
    pass


def _run(cmd, check=True, timeout=30, cancel=None, return_stderr=False):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_creationflags)
    stdout_buf = []
    stderr_buf = []

    def _reader(pipe, buf):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                buf.append(chunk)
        except Exception:
            pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_buf),
                             daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_buf),
                             daemon=True)
    t_out.start()
    t_err.start()

    try:
        elapsed = 0.0
        while True:
            if cancel and cancel.is_cancelled:
                proc.kill()
                proc.wait(timeout=5)
                raise CancelledError("Cancelled")
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                elapsed += 0.5
                if timeout is not None and elapsed >= timeout:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise subprocess.TimeoutExpired(cmd, timeout)
    except (CancelledError, subprocess.TimeoutExpired):
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        raise
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        raise

    t_out.join()
    t_err.join()
    stdout = b"".join(stdout_buf)
    stderr = b"".join(stderr_buf)

    if check and proc.returncode != 0:
        stderr_str = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{cmd[0]} failed (code {proc.returncode}): {stderr_str}")
    if return_stderr:
        return stdout, stderr.decode("utf-8", errors="replace").strip()
    return stdout


def _require_ffprobe():
    if not _ffprobe:
        raise RuntimeError("ffprobe not found. Set FFPROBE_PATH or install ffmpeg.")
    return _ffprobe


def _require_ffmpeg():
    if not _ffmpeg:
        raise RuntimeError("ffmpeg not found. Set FFMPEG_PATH or install ffmpeg.")
    return _ffmpeg


def probe(handle):
    fp = _require_ffprobe()
    raw = _run([fp, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", handle], timeout=60)
    data = json.loads(raw)

    audio = []
    streams = []
    audio_idx = 0
    for s in data.get("streams", []):
        codec_type = s.get("codec_type", "unknown")
        disposition = s.get("disposition") or {}
        if codec_type == "video" and disposition.get("attached_pic", 0):
            codec_type = "attachment"
        stream_index = int(s.get("index", 0))
        tags = s.get("tags") or {}
        language = tags.get("language", "und")
        title = tags.get("title", "")
        codec = s.get("codec_name", "?")

        entry = {
            "stream_index": stream_index,
            "codec_type": codec_type,
            "codec": codec,
            "language": language,
            "title": title,
        }

        if codec_type == "audio":
            entry["audio_index"] = audio_idx
            entry["channels"] = int(s.get("channels", 0))
            entry["sample_rate"] = int(s.get("sample_rate", 0))
            audio.append({
                "index": audio_idx,
                "stream_index": stream_index,
                "codec": codec,
                "channels": int(s.get("channels", 0)),
                "sample_rate": int(s.get("sample_rate", 0)),
                "bit_rate": int(s.get("bit_rate", 0) or 0),
                "language": language,
                "title": title,
            })
            audio_idx += 1
        elif codec_type == "video":
            entry["width"] = int(s.get("width", 0))
            entry["height"] = int(s.get("height", 0))
        elif codec_type == "subtitle":
            entry["subtitle_codec"] = codec

        streams.append(entry)

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0))

    return {"audio": audio, "streams": streams, "duration": duration}


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


def decode_audio(handle, audio_track_index, target_sr, vocal_filter=False,
                 cancel=None):
    ff = _require_ffmpeg()
    cmd = [ff, "-v", "error",
           "-i", handle,
           "-map", f"0:a:{audio_track_index}"]

    if vocal_filter:
        cmd += ["-af", f"aformat=channel_layouts=mono,bandreject=f=1000:width_type=h:w=2700,aresample={target_sr}"]
    else:
        cmd += ["-ar", str(target_sr)]

    cmd += ["-ac", "1",
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "pipe:1"]
    raw, stderr = _run(cmd, timeout=3600, cancel=cancel, return_stderr=True)
    warnings = stderr if stderr else None
    if len(raw) == 0:
        return np.array([], dtype=np.float32), warnings
    return np.frombuffer(raw, dtype=np.float32).copy(), warnings


FRAME_W, FRAME_H = 160, 120


def extract_frame(handle, timestamp, width=FRAME_W, height=FRAME_H):
    ff = _require_ffmpeg()
    cmd = [ff, "-v", "quiet",
           "-ss", f"{timestamp:.3f}",
           "-i", handle,
           "-vframes", "1",
           "-s", f"{width}x{height}",
           "-f", "rawvideo",
           "-pix_fmt", "gray",
           "pipe:1"]
    raw = _run(cmd, timeout=30)
    if len(raw) != height * width:
        return None
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32)


def version_info():
    result = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = _find_binary(name)
        if not binary:
            continue
        try:
            raw = _run([binary, "-version"], timeout=10)
            first_line = raw.decode("utf-8", errors="replace").split("\n")[0]
            parts = first_line.split()
            ver = parts[2] if len(parts) >= 3 else first_line.strip()
            result[name] = ver
        except Exception:
            pass
    return result


library_versions = {}
try:
    library_versions = version_info()
except Exception:
    pass
