#!/usr/bin/env python3

import os
import re
import subprocess
import sys
import fflib
from fflib import CancelledError
from probe import get_duration, get_audio_sample_rate


def find_ffmpeg_binary():
    path = fflib.get_paths().get("ffmpeg", "")
    return path if path and os.path.isfile(path) else None


def _atempo_chain(atempo):
    if abs(atempo - 1.0) <= 0.0001:
        return []
    if atempo <= 0.01 or atempo > 200:
        raise ValueError(f"atempo out of sane range (0.01–200), got {atempo}")
    remaining = atempo
    parts = []
    for _ in range(20):
        if remaining <= 100.0:
            break
        parts.append("atempo=100.0")
        remaining /= 100.0
    for _ in range(20):
        if remaining >= 0.5:
            break
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return parts


def _build_piecewise_filter(atempo, segments, v1_sr, v1_dur,
                            v2_track=0, input_base=1):
    n = len(segments)
    tempo_parts = _atempo_chain(atempo)
    tempo_chain = ",".join(tempo_parts) if tempo_parts else ""
    base_filters = ",".join(f for f in [tempo_chain, f"aresample={v1_sr}"] if f)

    lines = []
    seg_labels = []
    prev_v2_end = None
    next_input = input_base

    for i, seg in enumerate(segments):
        off = seg["offset"]
        v1_s = seg["v1_start"]
        v1_e = min(seg["v1_end"], v1_dur) if v1_dur > 0 else seg["v1_end"]
        if v1_e == float("inf") or v1_e > 1e8:
            v1_e = v1_dur if v1_dur > 0 else 36000

        trim_start_pre = max(0.0, (v1_s - off) * atempo)
        trim_end_pre = max(trim_start_pre + 0.001, (v1_e - off) * atempo)
        seg_dur = v1_e - v1_s

        gap_v1 = 0.0
        if prev_v2_end is not None and trim_start_pre < prev_v2_end:
            gap_v1 = max(0.0, prev_v2_end / atempo + off - v1_s)
            gap_v1 = min(gap_v1, seg_dur)
            trim_start_pre = prev_v2_end
            trim_end_pre = max(trim_start_pre + 0.001, trim_end_pre)

        prev_v2_end = trim_end_pre
        out_label = f"[_seg{i}]"
        seg_labels.append(out_label)

        if gap_v1 > 0.01:
            gap_v2 = gap_v1 * atempo
            gap_idx = next_input
            next_input += 1
            gap_in = f"{gap_idx}:a:{v2_track}"
            gap_lbl = f"[_gap{i}]"
            gf = [f"[{gap_in}]atrim=end={gap_v2:.6f}",
                  "asetpts=PTS-STARTPTS", "volume=0"]
            if base_filters:
                gf.append(base_filters)
            lines.append(",".join(gf) + gap_lbl)

            aud_idx = next_input
            next_input += 1
            aud_in = f"{aud_idx}:a:{v2_track}"
            aud_lbl = f"[_aud{i}]"
            af = [f"[{aud_in}]atrim=start={trim_start_pre:.6f}:end={trim_end_pre:.6f}",
                  "asetpts=PTS-STARTPTS"]
            if base_filters:
                af.append(base_filters)
            lines.append(",".join(af) + aud_lbl)

            lines.append(
                f"{gap_lbl}{aud_lbl}concat=n=2:v=0:a=1,"
                f"apad=whole_dur={seg_dur:.6f}{out_label}")
        else:
            input_idx = next_input
            next_input += 1
            in_label = f"{input_idx}:a:{v2_track}"

            parts = [f"[{in_label}]atrim=start={trim_start_pre:.6f}:end={trim_end_pre:.6f}"]
            parts.append("asetpts=PTS-STARTPTS")
            if base_filters:
                parts.append(base_filters)

            needed_delay = max(0.0, off - v1_s)
            needed_delay = min(needed_delay, seg_dur)
            if needed_delay > 0.01:
                delay_ms = int(round(needed_delay * 1000))
                parts.append(f"adelay={delay_ms}:all=1")

            parts.append(f"apad=whole_dur={seg_dur:.6f}")
            lines.append(",".join(parts) + out_label)

    output_label = "[_v2out]"
    seg_in = "".join(seg_labels)
    lines.append(f"{seg_in}concat=n={n}:v=0:a=1{output_label}")

    return "; ".join(lines), output_label, next_input - input_base


def _run_ffmpeg(cmd, v1_dur, progress_cb, cancel, progress_prefix="mux"):
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        universal_newlines=True, errors="replace",
        creationflags=creationflags,
    )

    time_re = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
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
                all_times = time_re.findall(line)
                if all_times and progress_cb and v1_dur > 0:
                    h, mi, s, frac_str = (int(all_times[-1][0]),
                                          int(all_times[-1][1]),
                                          int(all_times[-1][2]),
                                          all_times[-1][3])
                    pos = (h * 3600 + mi * 60 + s
                           + int(frac_str) / (10 ** len(frac_str)))
                    pct = min(99, int(pos / v1_dur * 100))
                    progress_cb("progress", f"{progress_prefix}:{pct}")
            else:
                buf.append(ch)

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-20:])
            raise RuntimeError(
                f"ffmpeg exited with code {proc.returncode}:\n{tail}")
    except CancelledError:
        raise


def remux_with_ffmpeg(v1_path, out_path,
                      v1_stream_indices=None,
                      v1_duration=0,
                      ffmpeg_path=None, metadata_args=None,
                      sub_metadata_args=None,
                      default_audio=None,
                      progress_cb=None, cancel=None):
    if not ffmpeg_path:
        ffmpeg_path = find_ffmpeg_binary()
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg binary not found")

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        raise RuntimeError(f"Output directory does not exist: {out_dir}")

    v1_dur = v1_duration or get_duration(v1_path)

    v1_info = fflib.probe(v1_path)
    v1_stream_types = {s["stream_index"]: s["codec_type"]
                       for s in v1_info.get("streams", [])}

    if v1_stream_indices is not None:
        selected = v1_stream_indices
    else:
        selected = sorted(v1_stream_types.keys())

    v1_sub = [si for si in selected if v1_stream_types.get(si) == "subtitle"]
    non_sub = [si for si in selected if v1_stream_types.get(si) != "subtitle"]
    has_subs = len(v1_sub) > 0

    base, ext = os.path.splitext(out_path)
    tmp_nosubs = base + ".tmp_nosubs.mkv"

    try:
        # Pass 1: mux everything except subtitles
        mux_target = tmp_nosubs if has_subs else out_path
        if progress_cb:
            progress_cb("status", "Pass 1: muxing video + audio...")

        cmd = [ffmpeg_path, "-y", "-hide_banner"]
        cmd += ["-i", v1_path]

        for si in non_sub:
            cmd += ["-map", f"0:{si}"]

        cmd += ["-c", "copy"]

        if metadata_args:
            audio_idx = 0
            for si in non_sub:
                if v1_stream_types.get(si) == "audio" and audio_idx < len(metadata_args):
                    meta = metadata_args[audio_idx]
                    lang = meta.get("language") or ""
                    title = meta.get("title") or ""
                    cmd += [f"-metadata:s:a:{audio_idx}", f"language={lang}"]
                    cmd += [f"-metadata:s:a:{audio_idx}", f"title={title}"]
                    audio_idx += 1

        if default_audio is not None:
            n_audio = sum(1 for si in non_sub
                          if v1_stream_types.get(si) == "audio")
            for i in range(n_audio):
                disp = "default" if i == default_audio else "0"
                cmd += [f"-disposition:a:{i}", disp]

        if v1_dur > 0:
            cmd += ["-t", f"{v1_dur:.6f}"]

        cmd += [mux_target]

        _run_ffmpeg(cmd, v1_dur, progress_cb, cancel, progress_prefix="mux")

        # Pass 2: add subtitles separately (PGS subs cause interleaving issues)
        if has_subs:
            if progress_cb:
                progress_cb("status", "Pass 2: adding subtitles...")

            cmd = [ffmpeg_path, "-y", "-hide_banner"]
            cmd += ["-i", tmp_nosubs]
            cmd += ["-i", v1_path]

            cmd += ["-map", "0"]
            for si in v1_sub:
                cmd += ["-map", f"1:{si}"]

            cmd += ["-c", "copy"]

            if sub_metadata_args:
                for i, meta in enumerate(sub_metadata_args):
                    lang = meta.get("language") or ""
                    title = meta.get("title") or ""
                    cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
                    cmd += [f"-metadata:s:s:{i}", f"title={title}"]

            if v1_dur > 0:
                cmd += ["-t", f"{v1_dur:.6f}"]

            cmd += [out_path]

            _run_ffmpeg(cmd, v1_dur, progress_cb, cancel, progress_prefix="sub")
    finally:
        if os.path.isfile(tmp_nosubs):
            try:
                os.remove(tmp_nosubs)
            except OSError:
                pass

    if progress_cb:
        progress_cb("progress", "mux:100")
        progress_cb("status", "Done!")


def merge_with_ffmpeg(v1_path, v2_path, out_path, atempo, offset,
                      v1_n_audio, v2_indices, v1_duration,
                      segments=None,
                      v1_stream_indices=None,
                      ffmpeg_path=None, metadata_args=None,
                      sub_metadata_args=None,
                      default_audio=None,
                      progress_cb=None, cancel=None):
    if not ffmpeg_path:
        ffmpeg_path = find_ffmpeg_binary()
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg binary not found")

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        raise RuntimeError(f"Output directory does not exist: {out_dir}")

    v1_sr = get_audio_sample_rate(v1_path, 0)
    v1_dur = v1_duration or get_duration(v1_path)

    n_segments = len(segments) if segments else 1
    use_piecewise = (segments is not None and n_segments > 1)

    base, ext = os.path.splitext(out_path)
    tmp_audio = base + ".tmp.mka"
    tmp_nosubs = base + ".tmp_nosubs.mkv"

    v1_info = fflib.probe(v1_path)
    v1_stream_types = {s["stream_index"]: s["codec_type"]
                       for s in v1_info.get("streams", [])}
    if v1_stream_indices is not None:
        v1_sub = [si for si in v1_stream_indices
                  if v1_stream_types.get(si) == "subtitle"]
    else:
        v1_sub = [si for si in sorted(v1_stream_types.keys())
                  if v1_stream_types[si] == "subtitle"]
    has_subs = len(v1_sub) > 0

    try:
        _merge_pass1_audio(ffmpeg_path, v2_path, tmp_audio, atempo, offset,
                           v2_indices, v1_sr, v1_dur, segments, use_piecewise,
                           progress_cb, cancel)

        mux_target = tmp_nosubs if has_subs else out_path
        _merge_pass2_mux(ffmpeg_path, v1_path, tmp_audio, mux_target,
                         v1_n_audio, v2_indices, v1_dur,
                         v1_stream_indices, metadata_args,
                         progress_cb, cancel, skip_subs=has_subs,
                         default_audio=default_audio)

        if has_subs:
            _merge_pass3_subs(ffmpeg_path, mux_target, v1_path, out_path,
                              v1_sub, v1_dur,
                              progress_cb, cancel,
                              sub_metadata_args=sub_metadata_args)
    finally:
        for tmp in (tmp_audio, tmp_nosubs):
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    if progress_cb:
        progress_cb("progress", "mux:100")
        progress_cb("status", "Done!")


def _get_v2_bitrates(v2_path, v2_indices):
    try:
        info = fflib.probe(v2_path)
        tracks = info.get("audio", [])
        bitrates = {}
        for tidx in v2_indices:
            if tidx < len(tracks):
                br = int(tracks[tidx].get("bit_rate", 0) or 0)
                bitrates[tidx] = br
            else:
                bitrates[tidx] = 0
        if all(br == 0 for br in bitrates.values()):
            duration = info.get("duration", 0)
            if duration > 0:
                try:
                    file_size = os.path.getsize(v2_path)
                    n_audio = max(1, len(tracks))
                    avg_br = int(file_size * 8 / duration / n_audio)
                    for tidx in bitrates:
                        bitrates[tidx] = avg_br
                except OSError:
                    pass
        return bitrates
    except Exception:
        return {tidx: 0 for tidx in v2_indices}


def _pick_aac_bitrate(source_br):
    if source_br <= 0:
        return "192k"
    capped = min(source_br, 192000)
    capped = max(capped, 64000)
    return f"{capped // 1000}k"


def _merge_pass1_audio(ffmpeg_path, v2_path, tmp_audio, atempo, offset,
                       v2_indices, v1_sr, v1_dur, segments, use_piecewise,
                       progress_cb, cancel):
    if progress_cb:
        progress_cb("status", "Pass 1: encoding audio...")

    v2_bitrates = _get_v2_bitrates(v2_path, v2_indices)

    cmd = [ffmpeg_path, "-y", "-hide_banner"]

    if use_piecewise:
        fc_parts = []
        output_labels = []
        running_base = 0
        for i, tidx in enumerate(v2_indices):
            fg, out_label, n_inputs = _build_piecewise_filter(
                atempo, segments, v1_sr, v1_dur,
                v2_track=tidx, input_base=running_base)
            if len(v2_indices) > 1:
                fg = fg.replace("[_", f"[_t{i}_")
                out_label = out_label.replace("[_", f"[_t{i}_")
            fc_parts.append(fg)
            output_labels.append(out_label)
            running_base += n_inputs

        for _ in range(running_base):
            cmd += ["-i", v2_path]

        cmd += ["-filter_complex", "; ".join(fc_parts)]
        for out_label in output_labels:
            cmd += ["-map", out_label]

        for i, tidx in enumerate(v2_indices):
            br = _pick_aac_bitrate(v2_bitrates.get(tidx, 0))
            cmd += [f"-c:a:{i}", "aac", f"-b:a:{i}", br]
    else:
        cmd += ["-i", v2_path]

        for tidx in v2_indices:
            cmd += ["-map", f"0:a:{tidx}"]

        for i, tidx in enumerate(v2_indices):
            filters = []
            if offset < -0.001:
                trim_sec = abs(offset)
                if abs(atempo - 1.0) > 0.0001:
                    trim_sec = abs(offset) * atempo
                filters.append(f"atrim=start={trim_sec:.6f}")
                filters.append("asetpts=PTS-STARTPTS")
            filters.extend(_atempo_chain(atempo))
            if offset > 0.001:
                delay_ms = int(round(offset * 1000))
                filters.append(f"adelay={delay_ms}:all=1")
            filters.append(f"aresample={v1_sr}")
            if v1_dur > 0:
                filters.append(f"apad=whole_dur={v1_dur:.6f}")
            filter_str = ",".join(filters)
            cmd += [f"-filter:a:{i}", filter_str]
            br = _pick_aac_bitrate(v2_bitrates.get(tidx, 0))
            cmd += [f"-c:a:{i}", "aac", f"-b:a:{i}", br]

    if v1_dur > 0:
        cmd += ["-t", f"{v1_dur:.6f}"]

    cmd += [tmp_audio]

    _run_ffmpeg(cmd, v1_dur, progress_cb, cancel, progress_prefix="enc")


def _merge_pass2_mux(ffmpeg_path, v1_path, tmp_audio, out_path,
                     v1_n_audio, v2_indices, v1_dur,
                     v1_stream_indices, metadata_args,
                     progress_cb, cancel, skip_subs=False,
                     default_audio=None):
    if progress_cb:
        progress_cb("status", "Pass 2: muxing...")

    v1_info = fflib.probe(v1_path)
    v1_stream_types = {s["stream_index"]: s["codec_type"]
                       for s in v1_info.get("streams", [])}

    if v1_stream_indices is not None:
        v1_vid = [si for si in v1_stream_indices
                  if v1_stream_types.get(si) == "video"]
        v1_aud = [si for si in v1_stream_indices
                  if v1_stream_types.get(si) == "audio"]
        v1_rest = [si for si in v1_stream_indices
                   if si not in v1_vid and si not in v1_aud]
    else:
        all_si = sorted(v1_stream_types.keys())
        v1_vid = [si for si in all_si if v1_stream_types[si] == "video"]
        v1_aud = [si for si in all_si if v1_stream_types[si] == "audio"]
        v1_rest = [si for si in all_si
                   if si not in v1_vid and si not in v1_aud]

    if skip_subs:
        v1_rest = [si for si in v1_rest
                   if v1_stream_types.get(si) != "subtitle"]

    cmd = [ffmpeg_path, "-y", "-hide_banner"]
    cmd += ["-i", v1_path]
    cmd += ["-i", tmp_audio]

    for si in v1_vid:
        cmd += ["-map", f"0:{si}"]
    for si in v1_aud:
        cmd += ["-map", f"0:{si}"]
    for i in range(len(v2_indices)):
        cmd += ["-map", f"1:a:{i}"]
    for si in v1_rest:
        cmd += ["-map", f"0:{si}"]

    cmd += ["-c", "copy"]

    if metadata_args:
        for i, meta in enumerate(metadata_args):
            lang = meta.get("language") or ""
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
            cmd += [f"-metadata:s:a:{i}", f"title={title}"]

    if default_audio is not None:
        n_audio = len(v1_aud) + len(v2_indices)
        for i in range(n_audio):
            disp = "default" if i == default_audio else "0"
            cmd += [f"-disposition:a:{i}", disp]

    if v1_dur > 0:
        cmd += ["-t", f"{v1_dur:.6f}"]

    cmd += [out_path]

    _run_ffmpeg(cmd, v1_dur, progress_cb, cancel, progress_prefix="mux")


def _merge_pass3_subs(ffmpeg_path, nosubs_path, v1_path, out_path,
                      v1_sub_indices, v1_dur,
                      progress_cb, cancel,
                      sub_metadata_args=None):
    if progress_cb:
        progress_cb("status", "Pass 3: adding subtitles...")

    cmd = [ffmpeg_path, "-y", "-hide_banner"]
    cmd += ["-i", nosubs_path]
    cmd += ["-i", v1_path]

    cmd += ["-map", "0"]
    for si in v1_sub_indices:
        cmd += ["-map", f"1:{si}"]

    cmd += ["-c", "copy"]

    if sub_metadata_args:
        for i, meta in enumerate(sub_metadata_args):
            lang = meta.get("language") or ""
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
            cmd += [f"-metadata:s:s:{i}", f"title={title}"]

    if v1_dur > 0:
        cmd += ["-t", f"{v1_dur:.6f}"]

    cmd += [out_path]

    _run_ffmpeg(cmd, v1_dur, progress_cb, cancel, progress_prefix="sub")
