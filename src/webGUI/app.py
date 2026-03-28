#!/usr/bin/env python3

import os
import sys
import threading
import time
import uuid

from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

import fflib
from sync_engine import (
    LANG_NAMES, ALL_LANGUAGES,
    check_av, needs_container_change,
    probe_audio_tracks, get_duration,
    format_timestamp,
    CancellableTask, CancelledError,
    auto_align_audio,
    find_ffmpeg_binary, merge_with_ffmpeg,
)
from _version import __version__

APP_TITLE = f"Audio Sync & Merge {__version__}"

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024  # 16 GB

_tasks = {}
_tasks_lock = threading.Lock()
_TASK_TTL = 300


def _new_task(task_type):
    tid = str(uuid.uuid4())[:8]
    cancel = CancellableTask()
    with _tasks_lock:
        now = time.monotonic()
        stale = [k for k, v in _tasks.items()
                 if v.get("finished_at") and now - v["finished_at"] > _TASK_TTL]
        for k in stale:
            del _tasks[k]
        for v in _tasks.values():
            if v["type"] == task_type and v["status"] == "running":
                return None, None
        _tasks[tid] = {
            "type": task_type,
            "status": "running",
            "progress": "",
            "result": None,
            "error": None,
            "cancel": cancel,
        }
    return tid, cancel


def _update_task(tid, **kwargs):
    with _tasks_lock:
        if tid in _tasks:
            _tasks[tid].update(kwargs)
            if kwargs.get("status") in ("done", "cancelled", "error"):
                _tasks[tid]["finished_at"] = time.monotonic()


def _get_task(tid):
    with _tasks_lock:
        t = _tasks.get(tid)
        if t:
            return {k: v for k, v in t.items() if k != "cancel"}
    return None


@app.route("/")
def index():
    paths = fflib.get_paths()
    return render_template("index.html",
                           app_title=APP_TITLE,
                           all_languages=ALL_LANGUAGES,
                           lang_names=LANG_NAMES,
                           ffmpeg_path=paths["ffmpeg"],
                           ffprobe_path=paths["ffprobe"])


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


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "uploads")

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    safe_name = secure_filename(f.filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(UPLOAD_DIR, safe_name)
    f.save(dest)
    return jsonify({"path": dest, "filename": safe_name})


@app.route("/api/file-exists", methods=["POST"])
def api_file_exists():
    data = request.get_json() or {}
    path = data.get("path", "")
    return jsonify({"exists": os.path.isfile(path)})


@app.route("/api/probe", methods=["POST"])
def api_probe():
    data = request.get_json()
    filepath = data.get("filepath", "")
    if not filepath or not os.path.isfile(filepath):
        return jsonify({"error": f"File not found: {filepath}"}), 400

    tracks, method, error = probe_audio_tracks(filepath)
    duration = get_duration(filepath)

    change_needed, ext = needs_container_change(filepath)

    return jsonify({
        "tracks": tracks,
        "method": method,
        "error": error,
        "duration": duration,
        "duration_fmt": format_timestamp(duration),
        "container_change": change_needed,
        "container_ext": ext,
    })


@app.route("/api/align", methods=["POST"])
def api_align():
    data = request.get_json()
    v1 = data.get("v1_path", "")
    v2 = data.get("v2_path", "")
    t1 = data.get("v1_track", 0)
    t2 = data.get("v2_track", 0)

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not v2 or not os.path.isfile(v2):
        return jsonify({"error": f"V2 not found: {v2}"}), 400

    tid, cancel = _new_task("align")
    if tid is None:
        return jsonify({"error": "An align task is already running"}), 409

    def go():
        def cb(kind, msg):
            _update_task(tid, progress=msg)

        try:
            r = auto_align_audio(v1, v2, track1=t1, track2=t2,
                                 progress_cb=cb, cancel=cancel)
            result = {}
            for k, v in r.items():
                if k == "inlier_pairs":
                    result[k] = [(float(a), float(b), float(c))
                                 for a, b, c in v]
                elif isinstance(v, tuple):
                    result[k] = [float(x) for x in v]
                else:
                    try:
                        result[k] = float(v)
                    except (TypeError, ValueError):
                        result[k] = v
            _update_task(tid, status="done", result=result)
        except CancelledError:
            _update_task(tid, status="cancelled", error="Cancelled")
        except Exception as e:
            _update_task(tid, status="error", error=str(e))

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/merge", methods=["POST"])
def api_merge():
    data = request.get_json()
    v1 = data.get("v1_path", "")
    v2 = data.get("v2_path", "")
    out = data.get("out_path", "")
    atempo = data.get("atempo", 1.0)
    offset = data.get("offset", 0.0)
    v1_n_audio = data.get("v1_n_audio", 1)
    v2_indices = data.get("v2_indices", [0])
    v1_duration = data.get("v1_duration", 0)
    metadata = data.get("metadata", [])

    if not v1 or not os.path.isfile(v1):
        return jsonify({"error": f"V1 not found: {v1}"}), 400
    if not v2 or not os.path.isfile(v2):
        return jsonify({"error": f"V2 not found: {v2}"}), 400

    tid, cancel = _new_task("merge")
    if tid is None:
        return jsonify({"error": "A merge task is already running"}), 409

    def go():
        t0 = time.monotonic()

        def progress_cb(kind, msg):
            _update_task(tid, progress=f"{kind}:{msg}")

        try:
            merge_with_ffmpeg(
                v1_path=v1, v2_path=v2, out_path=out,
                atempo=atempo, offset=offset,
                v1_n_audio=v1_n_audio, v2_indices=v2_indices,
                v1_duration=v1_duration,
                metadata_args=metadata,
                progress_cb=progress_cb, cancel=cancel,
            )
            elapsed = time.monotonic() - t0
            mins, secs = divmod(int(elapsed), 60)
            _update_task(tid, status="done",
                         result={"elapsed": f"{mins}m {secs}s",
                                 "output": out})
        except CancelledError:
            _update_task(tid, status="cancelled", error="Cancelled")
        except Exception as e:
            _update_task(tid, status="error", error=str(e))

    threading.Thread(target=go, daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/task/<tid>")
def api_task_status(tid):
    t = _get_task(tid)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(t)


@app.route("/api/task/<tid>/cancel", methods=["POST"])
def api_task_cancel(tid):
    with _tasks_lock:
        t = _tasks.get(tid)
        if t and t.get("cancel"):
            t["cancel"].cancel()
            return jsonify({"ok": True})
    return jsonify({"error": "Task not found"}), 404


if __name__ == "__main__":
    print("=" * 50)
    print("  Audio Sync & Merge -- Web Interface")
    print("=" * 50)

    libs = check_av()
    info = libs.get("pyav", (False, "", ""))
    if info[0]:
        print(f"  libAV: {info[1]}")
    else:
        print(f"  WARNING: libAV not available -- {info[1]}")

    ffmpeg = find_ffmpeg_binary()
    print(f"  ffmpeg:  {ffmpeg or 'not found'}")
    print()
    print("  Open http://localhost:5000 in your browser")
    print("=" * 50)

    from waitress import serve
    serve(app, host="0.0.0.0", port=5000)
