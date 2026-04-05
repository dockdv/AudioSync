#!/usr/bin/env python3

import os
import sys
import threading
import time
import uuid

from flask import Flask, render_template, request, jsonify
import fflib
from probe import (
    LANG_NAMES, ALL_LANGUAGES,
    needs_container_change,
    probe_full,
)
from ctx import SessionContext
from sync_engine import (
    format_timestamp,
    CancellableTask, CancelledError,
    auto_align_audio,
    merge_with_ffmpeg,
)
from _version import __version__
import mkvmerge as _mkv

APP_TITLE = f"Audio Sync & Merge {__version__}"

app = Flask(__name__)

_sessions = {}
_sessions_lock = threading.Lock()
_SESSION_TTL = 3600
_SESSION_MAX_TTL = 7200
_last_purge = 0.0
_PURGE_INTERVAL = 300


def _purge_stale_sessions():
    """Must be called while holding _sessions_lock."""
    global _last_purge
    now = time.monotonic()
    if now - _last_purge < _PURGE_INTERVAL:
        return
    _last_purge = now
    stale = []
    for sid, s in _sessions.items():
        age = now - s["updated_at"]
        if age <= _SESSION_TTL:
            continue
        # Fix stale active_task pointer before deciding
        atid = s["active_task"]
        if atid and (atid not in s["tasks"]
                     or s["tasks"][atid].get("status") != "running"):
            s["active_task"] = None
        if age > _SESSION_MAX_TTL:
            stale.append(sid)
        elif s["active_task"] is None:
            stale.append(sid)
    for sid in stale:
        sess = _sessions[sid]
        for tid, t in sess["tasks"].items():
            if t.get("status") == "running" and t.get("cancel"):
                t["cancel"].cancel()
        del _sessions[sid]


def _new_session():
    sid = str(uuid.uuid4())[:16]
    now = time.monotonic()
    with _sessions_lock:
        _purge_stale_sessions()
        _sessions[sid] = {
            "created_at": now,
            "created_wall": time.time(),
            "updated_at": now,
            "label": "New session",
            "ctx": SessionContext(),
            "tasks": {},
            "active_task": None,
            "ui_state": {},
            "version": 0,
            "log": [],
            "log_idx": 0,
        }
    return sid


def _start_task(sid, task_type, params):
    tid = str(uuid.uuid4())[:8]
    cancel = CancellableTask()
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return None, None, "Session not found"
        if sess["active_task"]:
            atid = sess["active_task"]
            at = sess["tasks"].get(atid)
            if at and at["status"] == "running":
                return None, None, f"Session already has a running {at['type']} task"
        params = params or {}
        sess["tasks"][tid] = {
            "type": task_type,
            "status": "running",
            "progress": "",
            "result": None,
            "error": None,
            "cancel": cancel,
            "params": params,
        }
        sess["active_task"] = tid
        sess["updated_at"] = time.monotonic()
        sess["version"] += 1
        v1 = params.get("v1_path", "")
        v2 = params.get("v2_path", "")
        if v1 and v2:
            b1 = os.path.basename(v1)
            b2 = os.path.basename(v2)
            sess["label"] = f"{b1} \u2194 {b2}"
        elif v1:
            sess["label"] = f"{os.path.basename(v1)} (remux)"
    return tid, cancel, None


def _update_task(sid, tid, **kwargs):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess and tid in sess["tasks"]:
            sess["tasks"][tid].update(kwargs)
            sess["updated_at"] = time.monotonic()
            if kwargs.get("status") in ("done", "cancelled", "error"):
                sess["tasks"][tid]["finished_at"] = time.monotonic()
                if sess["active_task"] == tid:
                    sess["active_task"] = None
                sess["version"] += 1


def _append_log(sid, msg, source="server"):
    import datetime, time
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return
        sess["log_idx"] += 1
        ts = time.time()
        sess["log"].append({"idx": sess["log_idx"], "msg": msg, "source": source, "ts": ts})
        if len(sess["log"]) > 1000:
            sess["log"] = sess["log"][-1000:]


def _ensure_task_finished(sid, tid):
    """Safety net: mark task as error if it's still running when its thread exits."""
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess and tid in sess["tasks"]:
            t = sess["tasks"][tid]
            if t["status"] == "running":
                t["status"] = "error"
                t["error"] = "Task died unexpectedly"
                t["finished_at"] = time.monotonic()
                if sess["active_task"] == tid:
                    sess["active_task"] = None
                sess["version"] += 1


def _get_task(sid, tid):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess and tid in sess["tasks"]:
            sess["updated_at"] = time.monotonic()
            t = sess["tasks"][tid]
            return {k: v for k, v in t.items() if k != "cancel"}
    return None


def _serialize_session(sess):
    tasks = {}
    for tid, t in sess["tasks"].items():
        tasks[tid] = {k: v for k, v in t.items() if k != "cancel"}
    return {
        "label": sess["label"],
        "active_task": sess["active_task"],
        "tasks": tasks,
        "created_at": sess.get("created_wall", sess["created_at"]),
        "ui_state": sess.get("ui_state", {}),
        "version": sess.get("version", 0),
        "log_idx": sess.get("log_idx", 0),
    }


@app.route("/")
def index():
    paths = fflib.get_paths()
    versions = fflib.library_versions
    mkv_ver = _mkv.version_info().get("mkvmerge", "")
    return render_template("index.html",
                           app_title=APP_TITLE,
                           all_languages=ALL_LANGUAGES,
                           lang_names=LANG_NAMES,
                           ffmpeg_path=paths["ffmpeg"],
                           ffprobe_path=paths["ffprobe"],
                           ffmpeg_version=versions.get("ffmpeg", ""),
                           ffprobe_version=versions.get("ffprobe", ""),
                           hwaccel=paths.get("hwaccel", "none"),
                           mkvmerge_path=_mkv.get_path().get("mkvmerge", ""),
                           mkvmerge_version=mkv_ver)


@app.route("/api/browse", methods=["POST"])
def api_browse():
    data = request.get_json() or {}
    path = data.get("path", "")

    if not path:
        if sys.platform == "win32":
            import string
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    drives.append({"name": f"{letter}:", "path": drive,
                                   "is_dir": True})
            return jsonify({"entries": drives, "current": ""})
        else:
            path = "/"

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        return jsonify({"error": f"Not a directory: {path}"}), 400

    entries = []
    parent = os.path.dirname(path)
    if parent != path:
        entries.append({"name": "..", "path": parent, "is_dir": True})

    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            entries.append({"name": name, "path": full, "is_dir": is_dir})
    except PermissionError:
        return jsonify({"error": f"Permission denied: {path}"}), 403

    return jsonify({"entries": entries, "current": path})


@app.route("/api/file-exists", methods=["POST"])
def api_file_exists():
    data = request.get_json() or {}
    path = data.get("path", "")
    return jsonify({"exists": os.path.isfile(path)})


@app.route("/api/probe", methods=["POST"])
def api_probe():
    data = request.get_json() or {}
    filepath = data.get("filepath", "")
    if not filepath or not os.path.isfile(filepath):
        return jsonify({"error": f"File not found: {filepath}"}), 400

    tracks, all_streams, duration, method, error = probe_full(filepath)

    change_needed, ext = needs_container_change(filepath)

    return jsonify({
        "tracks": tracks,
        "streams": all_streams,
        "method": method,
        "error": error,
        "duration": duration,
        "duration_fmt": format_timestamp(duration),
        "container_change": change_needed,
        "container_ext": ext,
    })


@app.route("/api/sessions", methods=["POST"])
def api_session_create():
    sid = _new_session()
    return jsonify({"session_id": sid})


@app.route("/api/sessions", methods=["GET"])
def api_sessions_list():
    with _sessions_lock:
        _purge_stale_sessions()
        result = {}
        for sid, sess in _sessions.items():
            result[sid] = _serialize_session(sess)
    return jsonify(result)


@app.route("/api/session/<sid>")
def api_session_get(sid):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        sess["updated_at"] = time.monotonic()
        data = _serialize_session(sess)
    return jsonify(data)


@app.route("/api/session/<sid>/state", methods=["PATCH"])
def api_session_state(sid):
    data = request.get_json() or {}
    data.pop("log_entries", None)
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        sess.setdefault("ui_state", {}).update(data)
        sess["updated_at"] = time.monotonic()
        sess["version"] += 1
        v1 = data.get("v1_path", "")
        v2 = data.get("v2_path", "")
        if v1 and v2:
            sess["label"] = f"{os.path.basename(v1)} \u2194 {os.path.basename(v2)}"
        elif v1:
            sess["label"] = os.path.basename(v1)
        ver = sess["version"]
        label = sess["label"]
    return jsonify({"ok": True, "version": ver, "label": label})


@app.route("/api/session/<sid>/version")
def api_session_version(sid):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        ver = sess.get("version", 0)
    return jsonify({"version": ver})


@app.route("/api/session/<sid>/logs")
def api_session_logs_get(sid):
    after = int(request.args.get("after", 0))
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        entries = [e for e in sess["log"] if e["idx"] > after]
    return jsonify({"entries": entries})


@app.route("/api/session/<sid>/logs", methods=["POST"])
def api_session_logs_post(sid):
    data = request.get_json() or {}
    messages = data.get("messages", [])
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        for msg in messages[:50]:
            sess["log_idx"] += 1
            sess["log"].append({"idx": sess["log_idx"], "msg": str(msg),
                                "source": "client"})
        if len(sess["log"]) > 1000:
            sess["log"] = sess["log"][-1000:]
        idx = sess["log_idx"]
    return jsonify({"ok": True, "log_idx": idx})


@app.route("/api/session/<sid>/align", methods=["POST"])
def api_align(sid):
    data = request.get_json() or {}
    v1 = data.get("v1_path", "")
    v2 = data.get("v2_path", "")
    t1 = data.get("v1_track", 0)
    t2 = data.get("v2_track", 0)
    vocal_filter = data.get("vocal_filter", False)
    measure_lufs = data.get("measure_lufs", False)
    v1_info = {"streams": data.get("v1_streams", []),
                "audio": data.get("v1_tracks", []),
                "duration": data.get("v1_duration", 0)}
    v2_info = {"streams": data.get("v2_streams", []),
                "audio": data.get("v2_tracks", []),
                "duration": data.get("v2_duration", 0)}

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not v2 or not os.path.isfile(v2):
        return jsonify({"error": f"V2 not found: {v2}"}), 400

    tid, cancel, err = _start_task(sid, "align", {
        "v1_path": v1, "v2_path": v2,
        "v1_track": t1, "v2_track": t2,
        "vocal_filter": vocal_filter,
    })
    if err:
        return jsonify({"error": err}), 409

    def go():
        def cb(kind, msg):
            _update_task(sid, tid, progress=msg)
            _append_log(sid, msg)

        try:
            with _sessions_lock:
                sess = _sessions.get(sid)
                if not sess:
                    return
                ctx = sess["ctx"]
                ctx.v1_path = v1
                ctx.v2_path = v2
                ctx.align_track1 = t1
                ctx.align_track2 = t2
                ctx.vocal_filter = vocal_filter
                ctx.measure_lufs = measure_lufs
                ctx.v1_info = v1_info
                ctx.v2_info = v2_info
                ctx.progress_cb = cb
                ctx.cancel = cancel
            r = auto_align_audio(ctx)
            result = {}
            for k, v in r.items():
                if k == "inlier_pairs":
                    result[k] = [(float(a), float(b), float(c))
                                 for a, b, c in v]
                elif k == "segments":
                    segs = []
                    for seg in (v or []):
                        s = dict(seg)
                        if s.get("v1_end", 0) == float("inf"):
                            s["v1_end"] = 1e9
                        segs.append(s)
                    result[k] = segs
                elif isinstance(v, tuple):
                    result[k] = [float(x) for x in v]
                else:
                    try:
                        result[k] = float(v)
                    except (TypeError, ValueError):
                        result[k] = v
            _update_task(sid, tid, status="done", result=result)
        except CancelledError:
            _update_task(sid, tid, status="cancelled", error="Cancelled")
        except Exception as e:
            _update_task(sid, tid, status="error", error=str(e))
        finally:
            _ensure_task_finished(sid, tid)

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/session/<sid>/merge", methods=["POST"])
def api_merge(sid):
    data = request.get_json() or {}
    v1 = data.get("v1_path", "")
    v2 = data.get("v2_path", "")
    out = data.get("out_path", "")

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not v2 or not os.path.isfile(v2):
        return jsonify({"error": f"V2 not found: {v2}"}), 400
    if not out:
        return jsonify({"error": "Output path is required"}), 400

    tid, cancel, err = _start_task(sid, "merge", {
        "v1_path": v1, "v2_path": v2, "out_path": out,
    })
    if err:
        return jsonify({"error": err}), 409

    def go():
        t0 = time.monotonic()

        def progress_cb(kind, msg):
            _update_task(sid, tid, progress=f"{kind}:{msg}")
            _append_log(sid, f"{kind}:{msg}")

        try:
            with _sessions_lock:
                sess = _sessions.get(sid)
                if not sess:
                    return
                ctx = sess["ctx"]
                ctx.v1_path = v1
                ctx.v2_path = v2
                ctx.out_path = out
                if data.get("atempo") is not None:
                    ctx.atempo = data["atempo"]
                if data.get("offset") is not None:
                    ctx.offset = data["offset"]
                if data.get("segments") is not None:
                    ctx.segments = data["segments"]
                if data.get("v1_lufs") is not None:
                    ctx.v1_lufs = data["v1_lufs"]
                if data.get("v2_lufs") is not None:
                    ctx.v2_lufs = data["v2_lufs"]
                ctx.v1_stream_indices = data.get("v1_stream_indices")
                ctx.v2_stream_indices = data.get("v2_stream_indices")
                ctx.v1_duration = data.get("v1_duration", 0)
                ctx.audio_metadata = data.get("metadata")
                ctx.v1_sub_metadata = data.get("sub_metadata")
                ctx.v2_sub_metadata = data.get("v2_sub_metadata")
                ctx.default_audio_index = data.get("default_audio")
                ctx.audio_order = data.get("audio_order")
                ctx.gain_match = data.get("gain_match", False)
                ctx.v1_has_attachments = data.get("v1_has_attachments", True)
                ctx.v2_has_attachments = data.get("v2_has_attachments", False)
                v1_info = {"streams": data.get("v1_streams", []),
                            "audio": data.get("v1_tracks", []),
                            "duration": ctx.v1_duration}
                if v1_info.get("streams"):
                    ctx.v1_info = v1_info
                v2_info = {"streams": data.get("v2_streams", []),
                            "audio": data.get("v2_tracks", [])}
                if v2_info.get("streams") or v2_info.get("audio"):
                    ctx.v2_info = v2_info
                ctx.progress_cb = progress_cb
                ctx.cancel = cancel
            merge_with_ffmpeg(ctx)
            elapsed = time.monotonic() - t0
            mins, secs = divmod(int(elapsed), 60)
            _update_task(sid, tid, status="done",
                         result={"elapsed": f"{mins}m {secs}s",
                                 "output": out})
        except CancelledError:
            _update_task(sid, tid, status="cancelled", error="Cancelled")
        except Exception as e:
            _update_task(sid, tid, status="error", error=str(e))
        finally:
            _ensure_task_finished(sid, tid)

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/session/<sid>/remux", methods=["POST"])
def api_remux(sid):
    data = request.get_json() or {}
    v1 = data.get("v1_path", "")
    out = data.get("out_path", "")

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not out:
        return jsonify({"error": "Output path is required"}), 400

    tid, cancel, err = _start_task(sid, "remux", {
        "v1_path": v1, "out_path": out,
    })
    if err:
        return jsonify({"error": err}), 409

    def go():
        t0 = time.monotonic()

        def progress_cb(kind, msg):
            _update_task(sid, tid, progress=f"{kind}:{msg}")
            _append_log(sid, f"{kind}:{msg}")

        try:
            with _sessions_lock:
                sess = _sessions.get(sid)
                if not sess:
                    return
                ctx = sess["ctx"]
                ctx.v1_path = v1
                ctx.v2_path = None
                ctx.v2_info = None
                ctx.out_path = out
                ctx.atempo = None
                ctx.offset = None
                ctx.segments = None
                ctx.v2_stream_indices = None
                ctx.v2_sub_metadata = None
                ctx.gain_match = False
                ctx.v1_has_attachments = data.get("v1_has_attachments", True)
                ctx.v2_has_attachments = False
                ctx.v1_lufs = None
                ctx.v2_lufs = None
                ctx.v1_stream_indices = data.get("v1_stream_indices")
                ctx.v1_duration = data.get("v1_duration", 0)
                ctx.audio_metadata = data.get("metadata")
                ctx.v1_sub_metadata = data.get("sub_metadata")
                ctx.default_audio_index = data.get("default_audio")
                ctx.audio_order = data.get("audio_order")
                v1_info = {"streams": data.get("v1_streams", []),
                            "audio": data.get("v1_tracks", []),
                            "duration": ctx.v1_duration}
                if v1_info.get("streams"):
                    ctx.v1_info = v1_info
                ctx.progress_cb = progress_cb
                ctx.cancel = cancel
            merge_with_ffmpeg(ctx)
            elapsed = time.monotonic() - t0
            mins, secs = divmod(int(elapsed), 60)
            _update_task(sid, tid, status="done",
                         result={"elapsed": f"{mins}m {secs}s",
                                 "output": out})
        except CancelledError:
            _update_task(sid, tid, status="cancelled", error="Cancelled")
        except Exception as e:
            _update_task(sid, tid, status="error", error=str(e))
        finally:
            _ensure_task_finished(sid, tid)

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/session/<sid>/task/<tid>")
def api_task_status(sid, tid):
    t = _get_task(sid, tid)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(t)


@app.route("/api/session/<sid>", methods=["DELETE"])
def api_session_delete(sid):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        for tid, t in sess["tasks"].items():
            if t.get("status") == "running" and t.get("cancel"):
                t["cancel"].cancel()
        del _sessions[sid]
    return jsonify({"ok": True})


@app.route("/api/session/<sid>/task/<tid>/cancel", methods=["POST"])
def api_task_cancel(sid, tid):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess and tid in sess["tasks"]:
            t = sess["tasks"][tid]
            if t.get("cancel"):
                t["cancel"].cancel()
                return jsonify({"ok": True})
    return jsonify({"error": "Task not found"}), 404


@app.route("/api/session/<sid>/test-interleave", methods=["POST"])
def api_test_interleave(sid):
    data = request.get_json() or {}
    filepath = data.get("filepath", "")
    if not filepath or not os.path.isfile(filepath):
        return jsonify({"error": f"File not found: {filepath}"}), 400

    tid, cancel, err = _start_task(sid, "test-interleave", {
        "v1_path": filepath,
    })
    if err:
        return jsonify({"error": err}), 409

    def go():
        try:
            def cb(kind, msg):
                _update_task(sid, tid, progress=msg)
                _append_log(sid, msg)

            cb("status", "Probing stream info...")
            info = fflib.probe(filepath)
            stream_map = {}
            for s in info.get("streams", []):
                stream_map[s["stream_index"]] = s

            cb("status", "Reading packet timestamps...")
            packets = fflib.probe_packets(filepath, cancel=cancel,
                                          progress_cb=cb)

            total_packets = sum(len(v) for v in packets.values())
            duration = info.get("duration", 0)
            lines = []
            lines.append(f"Interleave analysis: {total_packets:,} packets across {len(packets)} streams")

            issues = []
            for idx in sorted(packets.keys()):
                dts_list = packets[idx]
                s = stream_map.get(idx, {})
                codec_type = s.get("codec_type", "unknown")
                codec = s.get("codec", "?")
                label = f"#{idx} {codec_type} ({codec})"

                if len(dts_list) < 2:
                    lines.append(f"  {label}: {len(dts_list)} packet(s) - skipped")
                    continue

                gaps = [dts_list[i+1] - dts_list[i] for i in range(len(dts_list)-1)]
                gaps_sorted = sorted(gaps)
                median_gap = gaps_sorted[len(gaps_sorted) // 2]
                max_gap = gaps_sorted[-1]
                first_dts = dts_list[0]
                last_dts = dts_list[-1]
                span = last_dts - first_dts

                lines.append(f"  {label}: {len(dts_list):,} pkts, "
                             f"span {first_dts:.1f}s-{last_dts:.1f}s, "
                             f"median gap {median_gap:.3f}s, max gap {max_gap:.3f}s")

                # Detect late first packet
                if first_dts > 5.0 and codec_type in ("audio", "subtitle"):
                    issues.append(f"ISSUE: {label} first packet at {first_dts:.1f}s "
                                  f"(late start, may stall muxer)")

                # Detect clustering: max gap >> median gap
                if max_gap > 5.0 and median_gap < 1.0 and codec_type in ("audio", "subtitle"):
                    issues.append(f"ISSUE: {label} has {max_gap:.1f}s gap "
                                  f"(vs {median_gap:.3f}s median) - "
                                  f"packets may be clustered")

                # Detect sparse streams that could stall interleaving
                if codec_type == "subtitle" and median_gap > 10.0:
                    issues.append(f"WARNING: {label} very sparse "
                                  f"(median gap {median_gap:.1f}s) - "
                                  f"may affect MKV interleaving")

                # Check if stream spans much less than file duration
                if duration > 0 and span < duration * 0.5 and codec_type in ("audio", "subtitle"):
                    issues.append(f"WARNING: {label} covers only "
                                  f"{span:.1f}s of {duration:.1f}s total")

            if issues:
                lines.append("")
                lines.append(f"Found {len(issues)} issue(s):")
                for issue in issues:
                    lines.append(f"  {issue}")
            else:
                lines.append("")
                lines.append("No interleaving issues detected.")

            _update_task(sid, tid, status="done",
                         result={"lines": lines, "issue_count": len(issues)})
        except CancelledError:
            _update_task(sid, tid, status="cancelled", error="Cancelled")
        except Exception as e:
            _update_task(sid, tid, status="error", error=str(e))
        finally:
            _ensure_task_finished(sid, tid)

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


if __name__ == "__main__":
    print("=" * 50)
    print("  Audio Sync & Merge -- Web Interface")
    print("=" * 50)

    paths = fflib.get_paths()
    mkv_path = _mkv.get_path().get("mkvmerge", "")

    print(f"  ffmpeg:   {paths.get('ffmpeg') or 'NOT FOUND'}")
    print(f"  ffprobe:  {paths.get('ffprobe') or 'NOT FOUND'}")
    print(f"  mkvmerge: {mkv_path or 'NOT FOUND'}")
    print(f"  hwaccel:  {paths.get('hwaccel', 'none')}")

    missing = []
    if not paths.get("ffmpeg"):
        missing.append("ffmpeg  (set FFMPEG_PATH or place in script dir)")
    if not paths.get("ffprobe"):
        missing.append("ffprobe (set FFPROBE_PATH or place in script dir)")
    if not mkv_path:
        missing.append("mkvmerge (set MKVMERGE_PATH or place in script dir)")

    if missing:
        print()
        print("  ERROR: Required binaries not found:")
        for m in missing:
            print(f"    - {m}")
        sys.exit(1)

    print()
    print("  Open http://localhost:5000 in your browser")
    print("=" * 50)

    import logging
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)

    import signal

    def _shutdown(signum, frame):
        print("\nShutting down...")
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    from waitress import serve
    serve(app, host="0.0.0.0", port=5000, threads=8)
