#!/usr/bin/env python3

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading

from fflib import CancelledError

_creationflags = 0
if sys.platform == "win32":
    _creationflags = subprocess.CREATE_NO_WINDOW

_PROGRESS_RE = re.compile(r"#GUI#progress\s+([\d.]+)%")


def _find_mkvmerge():
    env_val = os.environ.get("MKVMERGE_PATH", "").strip()
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
    suffixes = (["mkvmerge.exe", "mkvmerge"]
                if sys.platform == "win32" else ["mkvmerge"])
    for d in [script_dir,
              os.path.join(base, "ffmpeg-lib", plat, arch),
              os.path.join(base, "ffmpeg-lib", arch),
              os.path.join(base, "mkvtoolnix-lib", plat, arch),
              os.path.join(base, "mkvtoolnix-lib", arch)]:
        for s in suffixes:
            p = os.path.join(d, s)
            if os.path.isfile(p):
                return os.path.abspath(p)

    found = shutil.which("mkvmerge")
    if found:
        return found
    return None


_mkvmerge = _find_mkvmerge()


def get_path():
    return {"mkvmerge": _mkvmerge or ""}



def identify(filepath, mkvmerge_path=None):
    mkvm = mkvmerge_path or _mkvmerge
    cmd = [mkvm, "--identify", "--identification-format", "json", filepath]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_creationflags)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"mkvmerge --identify failed: {err}")
    return json.loads(proc.stdout)


def _run_mkvmerge(cmd, progress_cb=None, cancel=None, progress_prefix="mux"):
    stdout_lines = []
    stderr_buf = []

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True, errors="replace",
                            creationflags=_creationflags)

    def _read_stderr():
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_buf.append(chunk)
        except Exception:
            pass

    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_err.start()

    try:
        buf = []
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                line = "".join(buf)
                buf = []
                if not line:
                    continue
                if cancel and cancel.is_cancelled:
                    proc.kill()
                    proc.wait()
                    raise CancelledError("Cancelled")
                stdout_lines.append(line)
                if progress_cb:
                    m = _PROGRESS_RE.search(line)
                    if m:
                        pct = min(99, int(float(m.group(1))))
                        progress_cb("progress", f"{progress_prefix}:{pct}")
            else:
                buf.append(ch)
    except CancelledError:
        t_err.join(timeout=2)
        raise

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    t_err.join(timeout=5)

    if proc.returncode >= 2:
        all_output = "\n".join(stdout_lines) + "\n" + "".join(stderr_buf)
        tail = "\n".join(all_output.strip().splitlines()[-20:])
        raise RuntimeError(
            f"mkvmerge failed (code {proc.returncode}):\n{tail}")


def _tids_from_probe(v1_info):
    _TYPE_MAP = {"video": "video", "audio": "audio",
                 "subtitle": "subtitles", "attachment": "attachment"}
    tid_type = {}
    si_to_tid = {}
    tid = 0
    for s in v1_info.get("streams", []):
        ctype = s.get("codec_type", "unknown")
        if ctype in ("attachment",):
            continue
        si = s["stream_index"]
        si_to_tid[si] = tid
        tid_type[tid] = _TYPE_MAP.get(ctype, ctype)
        tid += 1
    return si_to_tid, tid_type


def mux_to_mkv(v1_path, out_path,
               v1_info=None,
               tmp_audio=None,
               v2_path=None, v2_indices=None, v2_offset=None,
               v1_stream_indices=None,
               metadata_args=None, sub_metadata_args=None,
               default_audio=None, audio_order=None,
               v1_duration=0,
               progress_cb=None, cancel=None,
               mkvmerge_path=None):
    mkvm = mkvmerge_path or _mkvmerge
    v2_indices = v2_indices or [0]

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        raise RuntimeError(f"Output directory does not exist: {out_dir}")

    if progress_cb:
        progress_cb("status", "Muxing with mkvmerge...")

    si_to_tid, tid_type = _tids_from_probe(v1_info or {})

    all_tids = sorted(tid_type.keys())

    if v1_stream_indices is not None:
        selected_tids = set()
        for si in v1_stream_indices:
            tid = si_to_tid.get(si)
            if tid is not None:
                selected_tids.add(tid)
    else:
        selected_tids = set(all_tids)

    v1_vid_tids = [tid for tid in all_tids
                   if tid in selected_tids and tid_type.get(tid) == "video"]
    v1_aud_tids = [tid for tid in all_tids
                   if tid in selected_tids and tid_type.get(tid) == "audio"]
    v1_sub_tids = [tid for tid in all_tids
                   if tid in selected_tids and tid_type.get(tid) == "subtitles"]
    v1_other_tids = [tid for tid in all_tids
                     if tid in selected_tids
                     and tid_type.get(tid) not in ("video", "audio",
                                                   "subtitles")]

    cmd = [mkvm, "--gui-mode", "-o", out_path]

    if v1_vid_tids:
        cmd += ["--video-tracks", ",".join(str(t) for t in v1_vid_tids)]
    else:
        cmd += ["--no-video"]

    if v1_aud_tids:
        cmd += ["--audio-tracks", ",".join(str(t) for t in v1_aud_tids)]
    else:
        cmd += ["--no-audio"]

    if v1_sub_tids:
        cmd += ["--subtitle-tracks", ",".join(str(t) for t in v1_sub_tids)]
    else:
        cmd += ["--no-subtitles"]

    n_v1_audio = len(v1_aud_tids)
    v2_aud_tids = []
    file_id_v2 = 1

    if tmp_audio:
        v2_aud_tids = list(range(len(v2_indices)))
    elif v2_path:
        v2_aud_tids = list(v2_indices)

    all_audio_tids = list(v1_aud_tids) + list(v2_aud_tids)
    if audio_order is not None and len(audio_order) == len(all_audio_tids):
        all_audio_tids_ordered = [all_audio_tids[i] for i in audio_order]
    else:
        all_audio_tids_ordered = all_audio_tids

    src_to_meta = {}
    if metadata_args and audio_order is not None and len(audio_order) == len(all_audio_tids):
        for out_pos, src_idx in enumerate(audio_order):
            if out_pos < len(metadata_args):
                src_to_meta[src_idx] = metadata_args[out_pos]
    elif metadata_args:
        for src_idx in range(len(all_audio_tids)):
            if src_idx < len(metadata_args):
                src_to_meta[src_idx] = metadata_args[src_idx]

    default_tid = None
    if default_audio is not None and 0 <= default_audio < len(all_audio_tids_ordered):
        default_tid = all_audio_tids_ordered[default_audio]

    for src_idx, tid in enumerate(v1_aud_tids):
        meta = src_to_meta.get(src_idx)
        if meta:
            cmd += ["--language", f"{tid}:{meta.get('language') or 'und'}"]
            if meta.get("title"):
                cmd += ["--track-name", f"{tid}:{meta['title']}"]
        if default_audio is not None:
            flag = "1" if tid == default_tid else "0"
            cmd += ["--default-track-flag", f"{tid}:{flag}"]

    if sub_metadata_args:
        for i, tid in enumerate(v1_sub_tids):
            if i < len(sub_metadata_args):
                meta = sub_metadata_args[i]
                cmd += ["--language", f"{tid}:{meta.get('language') or 'und'}"]
                if meta.get("title"):
                    cmd += ["--track-name", f"{tid}:{meta['title']}"]

    cmd.append(v1_path)

    if tmp_audio:
        v2_cmd = ["--no-video", "--no-subtitles"]
        for i, tid in enumerate(v2_aud_tids):
            meta = src_to_meta.get(n_v1_audio + i)
            if meta:
                v2_cmd += ["--language", f"{tid}:{meta.get('language') or 'und'}"]
                if meta.get("title"):
                    v2_cmd += ["--track-name", f"{tid}:{meta['title']}"]
            if default_audio is not None:
                flag = "1" if tid == default_tid else "0"
                v2_cmd += ["--default-track-flag", f"{tid}:{flag}"]
        cmd += v2_cmd + [tmp_audio]

    elif v2_path:
        v2_cmd = ["--no-video", "--no-subtitles"]

        if v2_aud_tids:
            v2_cmd += ["--audio-tracks",
                       ",".join(str(t) for t in v2_aud_tids)]

        if v2_offset is not None and abs(v2_offset) > 0.001:
            delay_ms = int(round(v2_offset * 1000))
            for tid in v2_aud_tids:
                v2_cmd += ["--sync", f"{tid}:{delay_ms}"]

        for i, tid in enumerate(v2_aud_tids):
            meta = src_to_meta.get(n_v1_audio + i)
            if meta:
                v2_cmd += ["--language", f"{tid}:{meta.get('language') or 'und'}"]
                if meta.get("title"):
                    v2_cmd += ["--track-name", f"{tid}:{meta['title']}"]
            if default_audio is not None:
                flag = "1" if tid == default_tid else "0"
                v2_cmd += ["--default-track-flag", f"{tid}:{flag}"]

        cmd += v2_cmd + [v2_path]

    order_parts = []

    for tid in v1_vid_tids:
        order_parts.append(f"0:{tid}")

    for tid in all_audio_tids_ordered:
        if tid in v1_aud_tids:
            order_parts.append(f"0:{tid}")
        elif tid in v2_aud_tids:
            order_parts.append(f"{file_id_v2}:{tid}")

    for tid in v1_sub_tids:
        order_parts.append(f"0:{tid}")

    for tid in v1_other_tids:
        order_parts.append(f"0:{tid}")

    if order_parts:
        cmd += ["--track-order", ",".join(order_parts)]

    _run_mkvmerge(cmd, progress_cb=progress_cb, cancel=cancel,
                  progress_prefix="mux")

    if progress_cb:
        progress_cb("progress", "mux:100")
        progress_cb("status", "Done!")


def version_info():
    try:
        mkvm = _mkvmerge
        proc = subprocess.run([mkvm, "--version"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=_creationflags, timeout=10)
        output = proc.stdout.decode("utf-8", errors="replace").strip()
        parts = output.split()
        ver = parts[1] if len(parts) >= 2 else output
        return {"mkvmerge": ver}
    except Exception:
        return {}
