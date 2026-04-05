import json
import os
import platform
import re
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

_HWACCEL_PRIORITY = ["cuda", "vaapi", "videotoolbox", "qsv"]


def _detect_hwaccel():
    if not _ffmpeg:
        return None
    try:
        raw = subprocess.run(
            [_ffmpeg, "-hwaccels"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, creationflags=_creationflags if sys.platform == "win32" else 0,
        ).stdout.decode("utf-8", errors="replace")
        available = set()
        capture = False
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("Hardware acceleration methods:"):
                capture = True
                continue
            if capture and line:
                available.add(line)
        for method in _HWACCEL_PRIORITY:
            if method not in available:
                continue
            try:
                result = subprocess.run(
                    [_ffmpeg, "-v", "quiet", "-hwaccel", method,
                     "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1",
                     "-vframes", "1", "-f", "null", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=10,
                    creationflags=_creationflags if sys.platform == "win32" else 0)
                if result.returncode == 0:
                    return method
            except Exception:
                continue
    except Exception:
        pass
    return None


_hwaccel_method = _detect_hwaccel()
_hwaccel_failed = False


def _hwaccel_flags():
    if _hwaccel_failed or not _hwaccel_method:
        return []
    return ["-hwaccel", _hwaccel_method]


def get_paths():
    return {"ffmpeg": _ffmpeg or "", "ffprobe": _ffprobe or "",
            "hwaccel": _hwaccel_method or "none"}

_creationflags = 0
if sys.platform == "win32":
    _creationflags = subprocess.CREATE_NO_WINDOW


class CancelledError(Exception):
    pass


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")


def _run(cmd, check=True, timeout=30, cancel=None, return_stderr=False,
         discard_stdout=False, progress_cb=None, duration=0,
         progress_prefix="mux"):
    stdout_pipe = subprocess.DEVNULL if discard_stdout else subprocess.PIPE
    use_progress = progress_cb is not None and duration > 0

    if use_progress:
        proc = subprocess.Popen(cmd, stdout=stdout_pipe, stderr=subprocess.PIPE,
                                universal_newlines=True, errors="replace",
                                creationflags=_creationflags)
        stderr_lines = []
        try:
            buf = []
            while True:
                ch = proc.stderr.read(1)
                if not ch:
                    break
                if ch in ('\r', '\n'):
                    line = ''.join(buf)
                    buf = []
                    if not line:
                        continue
                    if cancel and cancel.is_cancelled:
                        proc.kill()
                        proc.wait()
                        proc.stderr.close()
                        raise CancelledError("Cancelled")
                    stderr_lines.append(line)
                    all_times = _TIME_RE.findall(line)
                    if all_times:
                        h, mi, s, frac_str = (int(all_times[-1][0]),
                                              int(all_times[-1][1]),
                                              int(all_times[-1][2]),
                                              all_times[-1][3])
                        pos = (h * 3600 + mi * 60 + s
                               + int(frac_str) / (10 ** len(frac_str)))
                        pct = min(99, int(pos / duration * 100))
                        progress_cb("progress", f"{progress_prefix}:{pct}")
                else:
                    buf.append(ch)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if check and proc.returncode != 0:
                tail = "\n".join(stderr_lines[-20:])
                raise RuntimeError(
                    f"{cmd[0]} failed (code {proc.returncode}):\n{tail}")
        except CancelledError:
            raise
        stdout = b""
        stderr_str = "\n".join(stderr_lines)
    else:
        proc = subprocess.Popen(cmd, stdout=stdout_pipe, stderr=subprocess.PIPE,
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

        if not discard_stdout:
            t_out = threading.Thread(target=_reader,
                                     args=(proc.stdout, stdout_buf), daemon=True)
            t_out.start()
        t_err = threading.Thread(target=_reader,
                                  args=(proc.stderr, stderr_buf), daemon=True)
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
            if not discard_stdout:
                t_out.join(timeout=2)
            t_err.join(timeout=2)
            raise
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
            if not discard_stdout:
                t_out.join(timeout=2)
            t_err.join(timeout=2)
            raise

        if not discard_stdout:
            t_out.join()
        t_err.join()
        stdout = b"".join(stdout_buf) if not discard_stdout else b""
        stderr_str = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()

        if check and proc.returncode != 0:
            raise RuntimeError(
                f"{cmd[0]} failed (code {proc.returncode}): {stderr_str}")

    if return_stderr:
        return stdout, stderr_str
    return stdout



def _normalize_lang(code):
    from probe import normalize_language
    return normalize_language(code)


def _parse_frame_rate(s):
    """Parse ffprobe frame rate string like '24000/1001' into a float."""
    if not s or s == "0/0":
        return 0.0
    parts = s.split("/")
    if len(parts) == 2:
        num, den = float(parts[0]), float(parts[1])
        return num / den if den > 0 else 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def probe(handle):
    fp = _ffprobe
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
        language = _normalize_lang(tags.get("language", "und"))
        title = tags.get("title", "")
        codec = s.get("codec_name", "?")

        entry = {
            "stream_index": stream_index,
            "codec_type": codec_type,
            "codec": codec,
            "language": language,
            "title": title,
            "start_time": float(s.get("start_time", 0)),
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
                "start_time": float(s.get("start_time", 0)),
            })
            audio_idx += 1
        elif codec_type == "video":
            entry["width"] = int(s.get("width", 0))
            entry["height"] = int(s.get("height", 0))
            entry["frame_rate"] = _parse_frame_rate(s.get("r_frame_rate", "0/1"))
        elif codec_type == "subtitle":
            entry["subtitle_codec"] = codec

        streams.append(entry)

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0))

    return {"audio": audio, "streams": streams, "duration": duration}


_LUFS_SUMMARY_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s+([-\d.]+)\s+LUFS",
                              re.MULTILINE)


def measure_lufs(handle, audio_track_index, cancel=None):
    """Measure integrated LUFS using FFmpeg ebur128 filter."""
    ff = _ffmpeg
    cmd = [ff, "-v", "info",
           "-i", handle,
           "-map", f"0:a:{audio_track_index}",
           "-af", "ebur128",
           "-f", "null", "-"]
    _, stderr = _run(cmd, timeout=3600, cancel=cancel, return_stderr=True,
                     discard_stdout=True)
    m = _LUFS_SUMMARY_RE.search(stderr or "")
    if m:
        return float(m.group(1))
    return None


def get_duration(handle):
    fp = _ffprobe
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
                 cancel=None, progress_cb=None, duration=0):
    ff = _ffmpeg
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

    if not progress_cb or duration <= 0:
        raw, stderr = _run(cmd, timeout=3600, cancel=cancel, return_stderr=True)
        warnings = stderr if stderr else None
        if len(raw) == 0:
            return np.array([], dtype=np.float32), warnings
        return np.frombuffer(raw, dtype=np.float32).copy(), warnings

    expected_bytes = int(duration * target_sr * 4)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_creationflags)
    stderr_buf = []
    t_err = threading.Thread(target=lambda: stderr_buf.append(
        proc.stderr.read()), daemon=True)
    t_err.start()

    chunks = []
    total_read = 0
    last_pct = -1
    try:
        while True:
            if cancel and cancel.is_cancelled:
                proc.kill()
                proc.wait(timeout=5)
                t_err.join(timeout=2)
                raise CancelledError("Cancelled")
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total_read += len(chunk)
            pct = min(99, int(total_read / expected_bytes * 100))
            if pct != last_pct:
                progress_cb(pct)
                last_pct = pct
    except CancelledError:
        raise
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        t_err.join(timeout=2)
        raise

    proc.wait(timeout=30)
    t_err.join(timeout=5)
    stderr_str = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()
    warnings = stderr_str if stderr_str else None

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): {stderr_str}")

    raw = b"".join(chunks)
    if len(raw) == 0:
        return np.array([], dtype=np.float32), warnings
    return np.frombuffer(raw, dtype=np.float32).copy(), warnings


FRAME_W, FRAME_H = 160, 120


def extract_frame(handle, timestamp, width=FRAME_W, height=FRAME_H):
    global _hwaccel_failed
    ff = _ffmpeg
    hw = _hwaccel_flags()
    if hw:
        cmd = [ff, "-v", "quiet"] + hw + [
            "-ss", f"{timestamp:.3f}",
            "-i", handle,
            "-vframes", "1",
            "-s", f"{width}x{height}",
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "pipe:1"]
        try:
            raw = _run(cmd, timeout=30)
            if len(raw) == height * width:
                return np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32)
        except Exception:
            pass
        _hwaccel_failed = True

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


def get_keyframe_timestamps(handle, start=0.0, end=None):
    """Get keyframe (I-frame) timestamps from the video stream.

    Returns a sorted list of float PTS timestamps in seconds.
    Uses packet flags (fast, no decoding) with -read_intervals.
    """
    fp = _ffprobe
    cmd = [fp, "-v", "quiet",
           "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,flags",
           "-of", "csv=p=0"]
    if start > 0 or end is not None:
        end_str = f"%{end:.3f}" if end is not None else ""
        cmd += ["-read_intervals", f"{start:.3f}{end_str}"]
    cmd.append(handle)

    raw = _run(cmd, timeout=600)
    text = raw.decode("utf-8", errors="replace")

    timestamps = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        flags = parts[1]
        if "K" not in flags:
            continue
        try:
            pts = float(parts[0])
        except ValueError:
            continue
        timestamps.append(pts)

    timestamps.sort()
    return timestamps


def get_video_resolution(handle):
    """Get width, height of the first video stream."""
    fp = _ffprobe
    raw = _run([fp, "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0", handle], timeout=10)
    text = raw.decode("utf-8", errors="replace").strip()
    parts = text.split(",")
    if len(parts) < 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None, None


def extract_frame_full(handle, timestamp, width, height):
    """Extract a single frame at specified resolution as grayscale.

    Returns numpy array of shape (H, W) with dtype float32, or None.
    """
    global _hwaccel_failed
    ff = _ffmpeg
    hw = _hwaccel_flags()
    if hw:
        cmd = [ff, "-v", "quiet"] + hw + [
            "-ss", f"{timestamp:.3f}",
            "-i", handle,
            "-vframes", "1",
            "-s", f"{width}x{height}",
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "pipe:1"]
        try:
            raw = _run(cmd, timeout=30)
            if len(raw) == height * width:
                return np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32)
        except Exception:
            pass
        _hwaccel_failed = True

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


def probe_packets(handle, cancel=None, progress_cb=None):
    """Read packet DTS positions per stream using ffprobe.

    Returns dict keyed by stream_index, each value is a sorted list of
    float DTS seconds.  Only streams that have at least one valid DTS
    are included.
    """
    fp = _ffprobe
    cmd = [fp, "-v", "quiet",
           "-print_format", "csv=p=0",
           "-show_entries", "packet=stream_index,dts_time",
           handle]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_creationflags)
    packets = {}          # {stream_index: [dts_float, ...]}
    line_count = 0
    last_report = 0
    buf = b""
    try:
        while True:
            if cancel and cancel.is_cancelled:
                proc.kill()
                proc.wait()
                raise CancelledError("Cancelled")
            chunk = proc.stdout.read(131072)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                parts = line.strip().split(b",")
                if len(parts) < 2:
                    continue
                try:
                    idx = int(parts[0])
                    dts = float(parts[1])
                except (ValueError, IndexError):
                    continue
                packets.setdefault(idx, []).append(dts)
                line_count += 1
            if progress_cb and line_count - last_report >= 50000:
                progress_cb("progress", f"Reading packets... ({line_count:,} so far)")
                last_report = line_count
        proc.wait(timeout=120)
    except CancelledError:
        raise
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed (code {proc.returncode})")

    for idx in packets:
        packets[idx].sort()

    return packets


library_versions = {}
try:
    library_versions = version_info()
except Exception:
    pass
