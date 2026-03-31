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
    check_av, needs_container_change,
    probe_full,
)
from sync_engine import (
    format_timestamp,
    CancellableTask, CancelledError,
    auto_align_audio,
    find_ffmpeg_binary, merge_with_ffmpeg, remux_with_ffmpeg,
)
from _version import __version__

APP_TITLE = f"Audio Sync & Merge {__version__}"

app = Flask(__name__)

_sessions = {}
_sessions_lock = threading.Lock()
_SESSION_TTL = 3600
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
        if now - s["updated_at"] <= _SESSION_TTL:
            continue
        atid = s["active_task"]
        if atid is None:
            stale.append(sid)
        elif atid not in s["tasks"] or s["tasks"][atid]["status"] != "running":
            s["active_task"] = None
            stale.append(sid)
    for sid in stale:
        del _sessions[sid]


def _new_session():
    sid = str(uuid.uuid4())[:8]
    now = time.monotonic()
    with _sessions_lock:
        _purge_stale_sessions()
        _sessions[sid] = {
            "created_at": now,
            "updated_at": now,
            "label": "New session",
            "tasks": {},
            "active_task": None,
            "ui_state": {},
            "version": 0,
            "log_version": 0,
            "logs": [],
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
        sess["tasks"][tid] = {
            "type": task_type,
            "status": "running",
            "progress": "",
            "result": None,
            "error": None,
            "cancel": cancel,
            "params": params or {},
        }
        sess["active_task"] = tid
        sess["updated_at"] = time.monotonic()
        sess["version"] = sess.get("version", 0) + 1
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
                sess["version"] = sess.get("version", 0) + 1
            if "progress" in kwargs:
                sess["log_version"] = sess.get("log_version", 0) + 1
                sess.setdefault("logs", []).append({
                    "msg": kwargs["progress"],
                    "lv": sess["log_version"],
                })


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
        "created_at": sess["created_at"],
        "ui_state": sess.get("ui_state", {}),
        "version": sess.get("version", 0),
        "log_version": sess.get("log_version", 0),
    }


@app.route("/")
def index():
    paths = fflib.get_paths()
    versions = fflib.library_versions
    return render_template("index.html",
                           app_title=APP_TITLE,
                           all_languages=ALL_LANGUAGES,
                           lang_names=LANG_NAMES,
                           ffmpeg_path=paths["ffmpeg"],
                           ffprobe_path=paths["ffprobe"],
                           ffmpeg_version=versions.get("ffmpeg", ""),
                           ffprobe_version=versions.get("ffprobe", ""))


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


@app.route("/api/sessions")
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
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        sess.setdefault("ui_state", {}).update(data)
        sess["updated_at"] = time.monotonic()
        sess["version"] = sess.get("version", 0) + 1
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
    return jsonify({
        "version": sess.get("version", 0),
        "log_version": sess.get("log_version", 0),
    })


@app.route("/api/session/<sid>/logs")
def api_session_logs(sid):
    since = request.args.get("since", 0, type=int)
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        logs = sess.get("logs", [])
        entries = [e for e in logs if e["lv"] > since]
        lv = sess.get("log_version", 0)
    return jsonify({"log_version": lv, "entries": entries})


@app.route("/api/session/<sid>/align", methods=["POST"])
def api_align(sid):
    data = request.get_json() or {}
    v1 = data.get("v1_path", "")
    v2 = data.get("v2_path", "")
    t1 = data.get("v1_track", 0)
    t2 = data.get("v2_track", 0)
    vocal_filter = data.get("vocal_filter", False)

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

        try:
            r = auto_align_audio(v1, v2, track1=t1, track2=t2,
                                 progress_cb=cb, cancel=cancel,
                                 vocal_filter=vocal_filter)
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

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/session/<sid>/merge", methods=["POST"])
def api_merge(sid):
    data = request.get_json() or {}
    v1 = data.get("v1_path", "")
    v2 = data.get("v2_path", "")
    out = data.get("out_path", "")
    atempo = data.get("atempo", 1.0)
    offset = data.get("offset", 0.0)
    v1_n_audio = data.get("v1_n_audio", 1)
    v1_stream_indices = data.get("v1_stream_indices", None)
    v2_indices = data.get("v2_indices", [0])
    v1_duration = data.get("v1_duration", 0)
    metadata = data.get("metadata", [])
    sub_metadata = data.get("sub_metadata", [])
    default_audio = data.get("default_audio", None)
    audio_order = data.get("audio_order", None)
    segments = data.get("segments", None)

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not v2 or not os.path.isfile(v2):
        return jsonify({"error": f"V2 not found: {v2}"}), 400
    if not out:
        return jsonify({"error": "Output path is required"}), 400

    tid, cancel, err = _start_task(sid, "merge", {
        "v1_path": v1, "v2_path": v2, "out_path": out,
        "atempo": atempo, "offset": offset,
        "v1_n_audio": v1_n_audio, "v1_stream_indices": v1_stream_indices,
        "v2_indices": v2_indices,
        "v1_duration": v1_duration, "metadata": metadata,
        "sub_metadata": sub_metadata, "default_audio": default_audio,
        "audio_order": audio_order, "segments": segments,
    })
    if err:
        return jsonify({"error": err}), 409

    def go():
        t0 = time.monotonic()

        def progress_cb(kind, msg):
            _update_task(sid, tid, progress=f"{kind}:{msg}")

        try:
            merge_with_ffmpeg(
                v1_path=v1, v2_path=v2, out_path=out,
                atempo=atempo, offset=offset,
                v1_n_audio=v1_n_audio, v2_indices=v2_indices,
                v1_duration=v1_duration,
                segments=segments,
                v1_stream_indices=v1_stream_indices,
                metadata_args=metadata,
                sub_metadata_args=sub_metadata,
                default_audio=default_audio,
                audio_order=audio_order,
                progress_cb=progress_cb, cancel=cancel,
            )
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

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/session/<sid>/remux", methods=["POST"])
def api_remux(sid):
    data = request.get_json() or {}
    v1 = data.get("v1_path", "")
    out = data.get("out_path", "")
    v1_stream_indices = data.get("v1_stream_indices", None)
    v1_duration = data.get("v1_duration", 0)
    metadata = data.get("metadata", [])
    sub_metadata = data.get("sub_metadata", [])
    default_audio = data.get("default_audio", None)
    audio_order = data.get("audio_order", None)

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not out:
        return jsonify({"error": "Output path is required"}), 400

    tid, cancel, err = _start_task(sid, "remux", {
        "v1_path": v1, "out_path": out,
        "v1_stream_indices": v1_stream_indices,
        "v1_duration": v1_duration, "metadata": metadata,
        "sub_metadata": sub_metadata, "default_audio": default_audio,
        "audio_order": audio_order,
    })
    if err:
        return jsonify({"error": err}), 409

    def go():
        t0 = time.monotonic()

        def progress_cb(kind, msg):
            _update_task(sid, tid, progress=f"{kind}:{msg}")

        try:
            remux_with_ffmpeg(
                v1_path=v1, out_path=out,
                v1_stream_indices=v1_stream_indices,
                v1_duration=v1_duration,
                metadata_args=metadata,
                sub_metadata_args=sub_metadata,
                default_audio=default_audio,
                audio_order=audio_order,
                progress_cb=progress_cb, cancel=cancel,
            )
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


if __name__ == "__main__":
    print("=" * 50)
    print("  Audio Sync & Merge -- Web Interface")
    print("=" * 50)

    libs = check_av()
    info = libs.get("fflib", (False, "", ""))
    if info[0]:
        print(f"  fflib: {info[1]}")
    else:
        print(f"  WARNING: fflib not available -- {info[1]}")

    ffmpeg = find_ffmpeg_binary()
    print(f"  ffmpeg:  {ffmpeg or 'not found'}")
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
