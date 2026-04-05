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


def _apply_meta(cmd_list, tid, meta):
    cmd_list += ["--language", f"{tid}:{meta.get('language') or 'und'}"]
    cmd_list += ["--track-name", f"{tid}:{meta.get('title') or ''}"]


def mux_to_mkv(mctx):
    mkvm = _mkvmerge

    out_dir = os.path.dirname(mctx.out_path)
    if out_dir and not os.path.isdir(out_dir):
        raise RuntimeError(f"Output directory does not exist: {out_dir}")

    if mctx.progress_cb:
        mctx.progress_cb("status", "Muxing with mkvmerge...")

    cmd = [mkvm, "--gui-mode", "-o", mctx.out_path]

    if mctx.v1_vid_tids:
        cmd += ["--video-tracks",
                ",".join(str(t) for t in mctx.v1_vid_tids)]
    else:
        cmd += ["--no-video"]

    if mctx.v1_aud_tids:
        cmd += ["--audio-tracks",
                ",".join(str(t) for t in mctx.v1_aud_tids)]
    else:
        cmd += ["--no-audio"]

    if mctx.v1_sub_tids:
        cmd += ["--subtitle-tracks",
                ",".join(str(t) for t in mctx.v1_sub_tids)]
    else:
        cmd += ["--no-subtitles"]

    if not getattr(mctx, "v1_has_attachments", True):
        cmd += ["--no-attachments"]

    for src_idx, tid in enumerate(mctx.v1_aud_tids):
        meta = mctx.audio_src_to_meta.get(src_idx)
        if meta:
            _apply_meta(cmd, tid, meta)
        if mctx.default_audio_ft is not None:
            flag = "1" if mctx.default_audio_ft == (0, tid) else "0"
            cmd += ["--default-track-flag", f"{tid}:{flag}"]

    if mctx.v1_sub_metadata:
        for i, tid in enumerate(mctx.v1_sub_tids):
            if i < len(mctx.v1_sub_metadata):
                _apply_meta(cmd, tid, mctx.v1_sub_metadata[i])

    cmd.append(mctx.v1_path)

    file_id_v2 = 1

    file_id_v2_subs = file_id_v2

    v2_no_att = not getattr(mctx, "v2_has_attachments", False)

    if mctx.tmp_audio_path:
        v2_cmd = ["--no-video", "--no-subtitles", "--no-attachments"]
        for i, tid in enumerate(mctx.v2_aud_tids):
            meta = mctx.audio_src_to_meta.get(len(mctx.v1_aud_tids) + i)
            if meta:
                _apply_meta(v2_cmd, tid, meta)
            if mctx.default_audio_ft is not None:
                flag = "1" if mctx.default_audio_ft == (file_id_v2, tid) else "0"
                v2_cmd += ["--default-track-flag", f"{tid}:{flag}"]
        cmd += v2_cmd + [mctx.tmp_audio_path]

        if mctx.v2_sub_tids and mctx.v2_path:
            file_id_v2_subs = file_id_v2 + 1
            v2s_cmd = ["--no-video", "--no-audio", "--no-attachments"]
            v2s_cmd += ["--subtitle-tracks",
                        ",".join(str(t) for t in mctx.v2_sub_tids)]
            if mctx.v2_sub_metadata:
                for i, tid in enumerate(mctx.v2_sub_tids):
                    if i < len(mctx.v2_sub_metadata):
                        _apply_meta(v2s_cmd, tid,
                                    mctx.v2_sub_metadata[i])
            cmd += v2s_cmd + [mctx.v2_path]

    elif mctx.v2_path and mctx.v2_streamcopy:
        v2_cmd = ["--no-video"]
        if v2_no_att:
            v2_cmd += ["--no-attachments"]

        if mctx.v2_aud_tids:
            v2_cmd += ["--audio-tracks",
                       ",".join(str(t) for t in mctx.v2_aud_tids)]

        if mctx.v2_sub_tids:
            v2_cmd += ["--subtitle-tracks",
                       ",".join(str(t) for t in mctx.v2_sub_tids)]
        else:
            v2_cmd += ["--no-subtitles"]

        if mctx.offset is not None and abs(mctx.offset) > 0.001:
            delay_ms = int(round(mctx.offset * 1000))
            for tid in mctx.v2_aud_tids:
                v2_cmd += ["--sync", f"{tid}:{delay_ms}"]

        for i, tid in enumerate(mctx.v2_aud_tids):
            meta = mctx.audio_src_to_meta.get(len(mctx.v1_aud_tids) + i)
            if meta:
                _apply_meta(v2_cmd, tid, meta)
            if mctx.default_audio_ft is not None:
                flag = "1" if mctx.default_audio_ft == (file_id_v2, tid) else "0"
                v2_cmd += ["--default-track-flag", f"{tid}:{flag}"]

        if mctx.v2_sub_metadata:
            for i, tid in enumerate(mctx.v2_sub_tids):
                if i < len(mctx.v2_sub_metadata):
                    _apply_meta(v2_cmd, tid, mctx.v2_sub_metadata[i])

        cmd += v2_cmd + [mctx.v2_path]

    order_parts = []
    for tid in mctx.v1_vid_tids:
        order_parts.append(f"0:{tid}")
    for fid, tid in mctx.audio_ft_ordered:
        order_parts.append(f"{fid}:{tid}")
    for tid in mctx.v1_sub_tids:
        order_parts.append(f"0:{tid}")
    for tid in mctx.v2_sub_tids:
        order_parts.append(f"{file_id_v2_subs}:{tid}")
    for tid in mctx.v1_other_tids:
        order_parts.append(f"0:{tid}")

    if order_parts:
        cmd += ["--track-order", ",".join(order_parts)]

    _run_mkvmerge(cmd, progress_cb=mctx.progress_cb, cancel=mctx.cancel,
                  progress_prefix="mux")

    if mctx.progress_cb:
        mctx.progress_cb("progress", "mux:100")
        mctx.progress_cb("status", "Done!")


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
