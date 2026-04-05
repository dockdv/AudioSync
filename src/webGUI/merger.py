#!/usr/bin/env python3

import os
import re
import shutil
import subprocess
import sys
import tempfile
import fflib

from probe import get_duration, get_audio_sample_rate

import mkvmerge as _mkvmerge_mod


def _tids_from_probe(info):
    _TYPE_MAP = {"video": "video", "audio": "audio",
                 "subtitle": "subtitles", "attachment": "attachment"}
    tid_type = {}
    si_to_tid = {}
    tid = 0
    for s in info.get("streams", []):
        ctype = s.get("codec_type", "unknown")
        if ctype in ("attachment",):
            continue
        si = s["stream_index"]
        si_to_tid[si] = tid
        tid_type[tid] = _TYPE_MAP.get(ctype, ctype)
        tid += 1
    return si_to_tid, tid_type


def _classify_v1_streams(mctx):
    st = {s["stream_index"]: s.get("codec_type", "unknown")
          for s in mctx.v1_info.get("streams", [])}
    mctx.v1_stream_types = st

    if mctx.v1_stream_indices is not None:
        sel = mctx.v1_stream_indices
        mctx.v1_vid_si = [si for si in sel if st.get(si) == "video"]
        mctx.v1_aud_si = [si for si in sel if st.get(si) == "audio"]
        mctx.v1_sub_si = [si for si in sel if st.get(si) == "subtitle"]
        mctx.v1_other_si = [si for si in sel
                            if si not in mctx.v1_vid_si
                            and si not in mctx.v1_aud_si
                            and si not in mctx.v1_sub_si]
    else:
        all_si = sorted(st.keys())
        mctx.v1_vid_si = [si for si in all_si if st[si] == "video"]
        mctx.v1_aud_si = [si for si in all_si if st[si] == "audio"]
        mctx.v1_sub_si = [si for si in all_si if st[si] == "subtitle"]
        mctx.v1_other_si = [si for si in all_si
                            if si not in mctx.v1_vid_si
                            and si not in mctx.v1_aud_si
                            and si not in mctx.v1_sub_si]
    mctx.v1_has_subs = len(mctx.v1_sub_si) > 0


def _compute_v1_tids(mctx):
    si_to_tid, tid_type = _tids_from_probe(mctx.v1_info)

    all_tids = sorted(tid_type.keys())
    selected = set()
    if mctx.v1_stream_indices is not None:
        for si in mctx.v1_stream_indices:
            tid = si_to_tid.get(si)
            if tid is not None:
                selected.add(tid)
    else:
        selected = set(all_tids)

    mctx.v1_vid_tids = [t for t in all_tids
                        if t in selected and tid_type.get(t) == "video"]
    mctx.v1_aud_tids = [t for t in all_tids
                        if t in selected and tid_type.get(t) == "audio"]
    mctx.v1_sub_tids = [t for t in all_tids
                        if t in selected and tid_type.get(t) == "subtitles"]
    mctx.v1_other_tids = [t for t in all_tids
                          if t in selected
                          and tid_type.get(t) not in
                          ("video", "audio", "subtitles")]


def _classify_v2_streams(mctx):
    st = {s["stream_index"]: s.get("codec_type", "unknown")
          for s in mctx.v2_info.get("streams", [])}
    mctx.v2_stream_types = st

    if mctx.v2_stream_indices is not None:
        sel = mctx.v2_stream_indices
        mctx.v2_aud_si = [si for si in sel if st.get(si) == "audio"]
        mctx.v2_sub_si = [si for si in sel if st.get(si) == "subtitle"]
    else:
        all_si = sorted(st.keys())
        mctx.v2_aud_si = [si for si in all_si if st.get(si) == "audio"]
        mctx.v2_sub_si = [si for si in all_si if st.get(si) == "subtitle"]

    v2_all_audio_si = [s["stream_index"] for s in mctx.v2_info.get("streams", [])
                       if s.get("codec_type") == "audio"]
    mctx.v2_aud_indices = [v2_all_audio_si.index(si) for si in mctx.v2_aud_si
                           if si in v2_all_audio_si]


def _compute_v2_tids(mctx):
    if mctx.tmp_audio_path:
        mctx.v2_aud_tids = list(range(len(mctx.v2_aud_indices)))
    elif mctx.v2_path and mctx.v2_streamcopy:
        mctx.v2_aud_tids = list(mctx.v2_aud_si)
    else:
        mctx.v2_aud_tids = []

    if mctx.v2_sub_si and mctx.v2_path:
        mctx.v2_sub_tids = list(mctx.v2_sub_si)
    else:
        mctx.v2_sub_tids = []


def _compute_audio_ordering(mctx):
    file_id_v2 = 1
    mctx.audio_ft = ([(0, t) for t in mctx.v1_aud_tids] +
                     [(file_id_v2, t) for t in mctx.v2_aud_tids])

    if (mctx.audio_order is not None
            and len(mctx.audio_order) == len(mctx.audio_ft)):
        mctx.audio_ft_ordered = [mctx.audio_ft[i] for i in mctx.audio_order]
    else:
        mctx.audio_ft_ordered = list(mctx.audio_ft)

    mctx.default_audio_ft = None
    if (mctx.default_audio_index is not None
            and 0 <= mctx.default_audio_index < len(mctx.audio_ft_ordered)):
        mctx.default_audio_ft = mctx.audio_ft_ordered[mctx.default_audio_index]

    mctx.audio_src_to_meta = {}
    if (mctx.audio_metadata
            and mctx.audio_order is not None
            and len(mctx.audio_order) == len(mctx.audio_ft)):
        for out_pos, src_idx in enumerate(mctx.audio_order):
            if out_pos < len(mctx.audio_metadata):
                mctx.audio_src_to_meta[src_idx] = mctx.audio_metadata[out_pos]
    elif mctx.audio_metadata:
        for src_idx in range(len(mctx.audio_ft)):
            if src_idx < len(mctx.audio_metadata):
                mctx.audio_src_to_meta[src_idx] = mctx.audio_metadata[src_idx]


def prepare_merge(mctx):
    mctx.is_remux = mctx.v2_path is None
    mctx.ffmpeg_path = fflib.get_paths()["ffmpeg"]
    if not mctx.v1_info:
        mctx.v1_info = fflib.probe(mctx.v1_path)
    if not mctx.v2_info:
        mctx.v2_info = {}
    v1_audio = mctx.v1_info.get("audio", [])
    mctx.v1_sample_rate = (v1_audio[0].get("sample_rate", 48000)
                  if v1_audio else get_audio_sample_rate(mctx.v1_path, 0))
    mctx.v1_dur = (mctx.v1_duration
                   or mctx.v1_info.get("duration", 0)
                   or get_duration(mctx.v1_path))
    _classify_v1_streams(mctx)
    _compute_v1_tids(mctx)
    if not mctx.is_remux:
        _classify_v2_streams(mctx)


def set_v2_mode(mctx, tmp_audio_path=None, streamcopy=False):
    mctx.tmp_audio_path = tmp_audio_path
    mctx.v2_streamcopy = streamcopy
    _compute_v2_tids(mctx)
    _compute_audio_ordering(mctx)


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
                            v2_track=0, input_base=1, gain_db=None):
    n = len(segments)
    tempo_parts = _atempo_chain(atempo)
    tempo_chain = ",".join(tempo_parts) if tempo_parts else ""
    gain_filter = f"volume={gain_db:.2f}dB" if gain_db is not None and abs(gain_db) > 0.01 else ""
    base_filters = ",".join(f for f in [tempo_chain, f"aresample={v1_sr}", gain_filter] if f)

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
            gf = [f"[{gap_in}]asetpts=PTS-STARTPTS",
                  f"atrim=end={gap_v2:.6f}",
                  "asetpts=PTS-STARTPTS", "volume=0"]
            if base_filters:
                gf.append(base_filters)
            lines.append(",".join(gf) + gap_lbl)

            aud_idx = next_input
            next_input += 1
            aud_in = f"{aud_idx}:a:{v2_track}"
            aud_lbl = f"[_aud{i}]"
            af = [f"[{aud_in}]asetpts=PTS-STARTPTS",
                  f"atrim=start={trim_start_pre:.6f}:end={trim_end_pre:.6f}",
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

            parts = [f"[{in_label}]asetpts=PTS-STARTPTS",
                     f"atrim=start={trim_start_pre:.6f}:end={trim_end_pre:.6f}",
                     "asetpts=PTS-STARTPTS"]
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
    fflib._run(cmd, discard_stdout=True, timeout=None, cancel=cancel,
               progress_cb=progress_cb, duration=v1_dur,
               progress_prefix=progress_prefix)


def merge_with_ffmpeg(mctx):
    prepare_merge(mctx)

    out_dir = os.path.dirname(mctx.out_path)
    if out_dir and not os.path.isdir(out_dir):
        raise RuntimeError(f"Output directory does not exist: {out_dir}")

    tmp_dir = tempfile.mkdtemp(dir=os.path.dirname(mctx.out_path) or ".",
                               prefix=".audiosync_tmp_")
    tmp_audio = os.path.join(tmp_dir, "audio.mka")
    tmp_nosubs = os.path.join(tmp_dir, "nosubs.mkv")

    try:
        streamcopy_v2 = False
        if not mctx.is_remux:
            n_segments = len(mctx.segments) if mctx.segments else 1
            use_piecewise = (mctx.segments is not None and n_segments > 1)

            v2_gains = None
            if (mctx.gain_match
                    and mctx.v1_lufs is not None
                    and mctx.v2_lufs is not None):
                gain = max(-20.0, min(20.0, mctx.v1_lufs - mctx.v2_lufs))
                if abs(gain) > 0.01:
                    v2_gains = {tidx: gain for tidx in mctx.v2_aud_indices}
                    if mctx.progress_cb:
                        mctx.progress_cb(
                            "status",
                            f"Gain match: V1={mctx.v1_lufs:.1f} LUFS, "
                            f"V2={mctx.v2_lufs:.1f} LUFS → {gain:+.1f} dB")

            streamcopy_v2 = _can_streamcopy_v2(
                mctx.atempo, use_piecewise, v2_gains)

            if streamcopy_v2:
                if mctx.progress_cb:
                    mctx.progress_cb("status",
                                     "Stream-copy mode: skipping audio re-encode")
            else:
                _merge_pass1_audio(
                    mctx.ffmpeg_path, mctx.v2_path, tmp_audio,
                    mctx.atempo, mctx.offset, mctx.v2_aud_indices,
                    mctx.v1_sample_rate, mctx.v1_dur, mctx.segments, use_piecewise,
                    mctx.progress_cb, mctx.cancel,
                    v2_gains=v2_gains, v2_info=mctx.v2_info)

        if not mctx.is_remux and not streamcopy_v2:
            set_v2_mode(mctx,tmp_audio_path=tmp_audio)
        elif not mctx.is_remux and streamcopy_v2:
            set_v2_mode(mctx,streamcopy=True)
        else:
            set_v2_mode(mctx,)

        use_mkvmerge = mctx.out_path.lower().endswith(".mkv")

        if use_mkvmerge:
            _mkvmerge_mod.mux_to_mkv(mctx)
        else:
            _mux_pass(mctx, tmp_nosubs if mctx.v1_has_subs else mctx.out_path)

            if mctx.v1_has_subs:
                _merge_pass3_subs(
                    mctx, tmp_nosubs, mctx.out_path)
    finally:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if mctx.progress_cb:
        mctx.progress_cb("progress", "mux:100")
        mctx.progress_cb("status", "Done!")


def _get_v2_bitrates(v2_path, v2_indices, v2_info=None):
    try:
        info = v2_info if v2_info else fflib.probe(v2_path)
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


def _can_streamcopy_v2(atempo, use_piecewise, v2_gains):
    if abs(atempo - 1.0) > 0.0001:
        return False
    if use_piecewise:
        return False
    if v2_gains:
        return False
    return True


def _merge_pass1_audio(ffmpeg_path, v2_path, tmp_audio, atempo, offset,
                       v2_indices, v1_sr, v1_dur, segments, use_piecewise,
                       progress_cb, cancel, v2_gains=None, v2_info=None):
    if progress_cb:
        progress_cb("status", "Pass 1: encoding audio...")

    v2_bitrates = _get_v2_bitrates(v2_path, v2_indices, v2_info=v2_info)

    v2_tracks = v2_info.get("audio", []) if v2_info else []

    cmd = [ffmpeg_path, "-y", "-hide_banner"]

    if use_piecewise:
        fc_parts = []
        output_labels = []
        running_base = 0
        for i, tidx in enumerate(v2_indices):
            track_st = 0.0
            for t in v2_tracks:
                if t.get("index") == tidx:
                    track_st = t.get("start_time", 0.0)
                    break
            track_segments = segments
            if track_st > 0.001:
                track_segments = [
                    {**seg, "offset": seg["offset"] + track_st}
                    for seg in segments
                ]
            track_gain = (v2_gains or {}).get(tidx)
            fg, out_label, n_inputs = _build_piecewise_filter(
                atempo, track_segments, v1_sr, v1_dur,
                v2_track=tidx, input_base=running_base,
                gain_db=track_gain)
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
            track_st = 0.0
            for t in v2_tracks:
                if t.get("index") == tidx:
                    track_st = t.get("start_time", 0.0)
                    break
            track_delay = offset + track_st

            filters = ["asetpts=PTS-STARTPTS"]
            if track_delay < -0.001:
                trim_sec = abs(track_delay)
                if abs(atempo - 1.0) > 0.0001:
                    trim_sec = abs(track_delay) * atempo
                filters.append(f"atrim=start={trim_sec:.6f}")
                filters.append("asetpts=PTS-STARTPTS")
            filters.extend(_atempo_chain(atempo))
            if track_delay > 0.001:
                delay_ms = int(round(track_delay * 1000))
                filters.append(f"adelay={delay_ms}:all=1")
            filters.append(f"aresample={v1_sr}")
            track_gain = (v2_gains or {}).get(tidx)
            if track_gain is not None and abs(track_gain) > 0.01:
                filters.append(f"volume={track_gain:.2f}dB")
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


def _mux_pass(mctx, out_path):
    is_streamcopy = mctx.v2_streamcopy
    label = ("muxing..." if (mctx.tmp_audio_path or is_streamcopy)
             else "muxing video + audio...")
    if mctx.progress_cb:
        pass_num = "2" if mctx.tmp_audio_path else "1"
        mctx.progress_cb("status", f"Pass {pass_num}: {label}")

    v1_vid = mctx.v1_vid_si
    v1_aud = mctx.v1_aud_si
    v1_rest = list(mctx.v1_other_si) + list(mctx.v1_sub_si)

    if mctx.v1_has_subs:
        v1_rest = [si for si in v1_rest
                   if mctx.v1_stream_types.get(si) != "subtitle"]

    cmd = [mctx.ffmpeg_path, "-y", "-hide_banner"]
    cmd += ["-i", mctx.v1_path]

    v2_input_map = {}
    if mctx.tmp_audio_path:
        cmd += ["-i", mctx.tmp_audio_path]
    elif is_streamcopy:
        v2_tracks = mctx.v2_info.get("audio", [])
        next_input = 1
        for tidx in mctx.v2_aud_indices:
            track_st = 0.0
            for t in v2_tracks:
                if t.get("index") == tidx:
                    track_st = t.get("start_time", 0.0)
                    break
            track_delay = mctx.offset + track_st
            if track_delay < -0.001:
                cmd += ["-ss", f"{abs(track_delay):.6f}"]
            elif track_delay > 0.001:
                cmd += ["-itsoffset", f"{track_delay:.6f}"]
            cmd += ["-i", mctx.v2_path]
            v2_input_map[tidx] = next_input
            next_input += 1

    for si in v1_vid:
        cmd += ["-map", f"0:{si}"]

    audio_maps = [f"0:{si}" for si in v1_aud]
    if mctx.tmp_audio_path:
        audio_maps += [f"1:a:{i}" for i in range(len(mctx.v2_aud_indices))]
    elif is_streamcopy:
        audio_maps += [f"{v2_input_map[tidx]}:a:{tidx}"
                       for tidx in mctx.v2_aud_indices]
    if (mctx.audio_order is not None
            and len(mctx.audio_order) == len(audio_maps)):
        audio_maps = [audio_maps[i] for i in mctx.audio_order]
    for m in audio_maps:
        cmd += ["-map", m]

    for si in v1_rest:
        cmd += ["-map", f"0:{si}"]

    cmd += ["-c", "copy"]

    if mctx.audio_metadata:
        for i, meta in enumerate(mctx.audio_metadata):
            lang = meta.get("language") or ""
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
            cmd += [f"-metadata:s:a:{i}", f"title={title}"]

    if mctx.default_audio_index is not None:
        n_audio = len(audio_maps)
        for i in range(n_audio):
            disp = "default" if i == mctx.default_audio_index else "0"
            cmd += [f"-disposition:a:{i}", disp]

    if mctx.v1_dur > 0:
        cmd += ["-t", f"{mctx.v1_dur:.6f}"]

    cmd += [out_path]

    _run_ffmpeg(cmd, mctx.v1_dur, mctx.progress_cb, mctx.cancel,
                progress_prefix="mux")


def _merge_pass3_subs(mctx, nosubs_path, out_path):
    if mctx.progress_cb:
        mctx.progress_cb("status", "Pass 3: adding subtitles...")

    cmd = [mctx.ffmpeg_path, "-y", "-hide_banner"]
    cmd += ["-i", nosubs_path]
    cmd += ["-i", mctx.v1_path]

    cmd += ["-map", "0"]
    for si in mctx.v1_sub_si:
        cmd += ["-map", f"1:{si}"]

    next_input = 2
    if mctx.v2_sub_si and mctx.v2_path:
        cmd += ["-i", mctx.v2_path]
        for si in mctx.v2_sub_si:
            cmd += ["-map", f"{next_input}:{si}"]

    cmd += ["-c", "copy"]

    sub_idx = 0
    if mctx.v1_sub_metadata:
        for i, meta in enumerate(mctx.v1_sub_metadata):
            lang = meta.get("language") or ""
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:s:{sub_idx}", f"language={lang}"]
            cmd += [f"-metadata:s:s:{sub_idx}", f"title={title}"]
            sub_idx += 1

    if mctx.v2_sub_metadata:
        for meta in mctx.v2_sub_metadata:
            lang = meta.get("language") or ""
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:s:{sub_idx}", f"language={lang}"]
            cmd += [f"-metadata:s:s:{sub_idx}", f"title={title}"]
            sub_idx += 1

    if mctx.v1_dur > 0:
        cmd += ["-t", f"{mctx.v1_dur:.6f}"]

    cmd += [out_path]

    _run_ffmpeg(cmd, mctx.v1_dur, mctx.progress_cb, mctx.cancel,
                progress_prefix="sub")
