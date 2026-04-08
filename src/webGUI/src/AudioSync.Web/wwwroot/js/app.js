const state = {
    v1: { path: '', tracks: [], streams: [], duration: 0 },
    v2: { path: '', tracks: [], streams: [], duration: 0 },
    selected: { v1: {}, v2: {} },
    trackOverrides: {},
    defaultAudioIdx: null,
    mergeParams: null,
    segments: null,
    containerChange: false,
    containerExt: '',
    v1Lufs: null,
    v2Lufs: null,
    offsetEdited: false,
    atempoEdited: false,
};

let _sessionId = null;
let _viewId = 0;
let _runningTaskId = null;
let _runningTaskType = null;
let _pollInterval = null;
let _sessionCache = {};
let _saveTimer = null;
let _restoring = false;
let _logCursor = 0;

function _buildUIState() {
    return {
        v1_path: document.getElementById('v1-path-input').value,
        v2_path: document.getElementById('v2-path-input').value,
        out_path: document.getElementById('out-path-input').value,
        selected: { v1: {...state.selected.v1}, v2: {...state.selected.v2} },
        track_overrides: { ...state.trackOverrides },
        default_audio_idx: state.defaultAudioIdx,
        container_fmt: document.querySelector('input[name="container-fmt"]:checked').value,
        atempo: document.getElementById('atempo-input').value,
        offset: document.getElementById('offset-input').value,
        atempo_edited: state.atempoEdited,
        offset_edited: state.offsetEdited,
        vocal_filter: document.getElementById('vocal-filter-cb').checked,
        measure_lufs: document.getElementById('measure-lufs-cb').checked,
        v1_sync_track: document.getElementById('v1-sync-track').value,
        v2_sync_track: document.getElementById('v2-sync-track').value,
        segments: state.segments,
        gain_match: document.getElementById('gain-match-cb').checked,
        v1_lufs: state.v1Lufs,
        v2_lufs: state.v2Lufs,
        container_change: state.containerChange,
        container_ext: state.containerExt,
        v1_state: state.v1,
        v2_state: state.v2,
    };
}

function _persistUIState(uiState) {
    if (!_sessionId) return;
    if (_sessionCache[_sessionId]) {
        _sessionCache[_sessionId].ui_state = { ...uiState };
    }
    fetch(`/api/session/${_sessionId}/state`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(uiState),
    }).then(r => r.json()).then(data => {
        if (data.version !== undefined && _sessionCache[_sessionId]) {
            _sessionCache[_sessionId].version = data.version;
            if (data.label) {
                _sessionCache[_sessionId].label = data.label;
                renderSessionList(_sessionCache);
            }
        }
    }).catch(() => {});
}

function saveUIState() {
    if (_restoring || !_sessionId) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        _persistUIState(_buildUIState());
    }, 300);
}

function flushUIState() {
    clearTimeout(_saveTimer);
    if (_restoring || !_sessionId || !_sessionCache[_sessionId]) return;
    _persistUIState(_buildUIState());
}

function _postLogs(lines) {
    if (_sessionId) {
        fetch(`/api/session/${_sessionId}/logs`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages: lines }),
        }).then(r => r.ok ? r.json() : null)
          .then(data => {
              if (data && data.log_idx) {
                  _logCursor = Math.max(_logCursor, data.log_idx);
                  const cache = _sessionCache[_sessionId];
                  if (cache) cache.log_cursor = _logCursor;
              }
          }).catch(() => {});
    }
}

function log(msg) {
    const box = document.getElementById('log-box');
    const ts = new Date().toLocaleTimeString();
    const line = `[${ts}] ${msg}`;
    box.value += line + '\n';
    box.scrollTop = box.scrollHeight;
    if (!_restoring && _sessionId && _sessionCache[_sessionId]) {
        if (!_sessionCache[_sessionId].log_entries) _sessionCache[_sessionId].log_entries = [];
        const entries = _sessionCache[_sessionId].log_entries;
        entries.push(line);
        if (entries.length > 500) entries.splice(0, entries.length - 500);
        _postLogs([line]);
    }
}

function logSeparator(label) {
    const box = document.getElementById('log-box');
    const line = `\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 ${label} \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500`;
    box.value += '\n' + line + '\n';
    box.scrollTop = box.scrollHeight;
    if (!_restoring && _sessionId && _sessionCache[_sessionId]) {
        if (!_sessionCache[_sessionId].log_entries) _sessionCache[_sessionId].log_entries = [];
        const entries = _sessionCache[_sessionId].log_entries;
        entries.push('', line);
        if (entries.length > 500) entries.splice(0, entries.length - 500);
        _postLogs(['', line]);
    }
}

function formatTimestamp(sec) {
    if (!sec || isNaN(sec)) return '0:00.000';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${s.toFixed(3).padStart(6,'0')}`;
    return `${m}:${s.toFixed(3).padStart(6,'0')}`;
}

function basename(p) {
    return p.replace(/\\/g, '/').split('/').pop();
}

function escapeHtml(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function setProgress(id, pct, label) {
    const bar = document.getElementById(id);
    if (!bar) return;
    const fill = bar.querySelector('.fill');
    const txt = bar.querySelector('.pct');
    if (fill) fill.style.width = `${pct}%`;
    if (txt) txt.textContent = label != null ? label : `${Math.round(pct)}%`;
}

async function apiPost(url, data) {
    const res = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    });
    if (!res.ok) {
        const text = await res.text();
        try {
            const j = JSON.parse(text);
            if (j && (j.error || j.title || j.detail)) {
                return { error: j.error || j.detail || j.title };
            }
            return { error: `HTTP ${res.status}: ${text || res.statusText}` };
        } catch {
            return { error: `HTTP ${res.status}: ${text || res.statusText}` };
        }
    }
    try { return await res.json(); } catch { return { error: 'Invalid JSON response' }; }
}

const _LOCK_EXEMPT_IDS = new Set(['global-stop', 'close-session-btn']);

function lockButtons(runningType) {
    const root = document.querySelector('.app') || document.body;
    const els = root.querySelectorAll('input, select, textarea, button');
    els.forEach(el => {
        if (_LOCK_EXEMPT_IDS.has(el.id)) return;
        el.dataset.prevDisabled = el.disabled ? '1' : '0';
        el.disabled = true;
    });
    // Also lock session list (sidebar) so user can't switch sessions mid-task.
    document.querySelectorAll('#session-list button, #session-list input').forEach(el => {
        el.dataset.prevDisabled = el.disabled ? '1' : '0';
        el.disabled = true;
    });
    document.getElementById('global-stop').classList.remove('hidden');
}

function unlockButtons() {
    const all = document.querySelectorAll('[data-prev-disabled]');
    all.forEach(el => {
        el.disabled = el.dataset.prevDisabled === '1';
        delete el.dataset.prevDisabled;
    });
    document.getElementById('global-stop').classList.add('hidden');
}

// Logs are pushed via SSE (see _logStream below). This stub remains so the
// existing call sites don't need refactoring; it's a no-op now.
function _fetchAndDisplayLogs() { return Promise.resolve(); }

// =====================================================================
// Server-Sent Events: single multiplexed stream for log entries AND task
// state changes across ALL sessions. No client-side polling for progress.
// =====================================================================
let _eventStream = null;
const _logCursors = {};        // sid → highest log idx seen
const _taskWatchers = {};      // tid → { onUpdate, onDone }
const _taskFinished = {};      // tid → true (so a watcher registered after
                               //   the terminal event still fires onDone)
const _taskCache = {};         // tid → last task event seen (for replay/init)

function _ensureLogStream() {
    if (_eventStream) return;
    _eventStream = new EventSource('/api/events/stream');
    _eventStream.onmessage = (ev) => {
        try {
            const m = JSON.parse(ev.data);
            if (m.kind === 'log') return _handleLogEvent(m);
            if (m.kind === 'task') return _handleTaskEvent(m);
        } catch (e) { /* ignore malformed event */ }
    };
    _eventStream.onerror = () => {
        // EventSource auto-reconnects; nothing to do here
    };
}

function _handleLogEvent(m) {
    const sid = m.sid;
    const idx = m.idx;
    if (_logCursors[sid] && idx <= _logCursors[sid]) return;
    _logCursors[sid] = idx;
    if (m.source !== 'server') return;

    const ts = m.ts ? new Date(m.ts * 1000).toLocaleTimeString() : null;
    const line = ts ? `[${ts}] ${m.msg}` : m.msg;

    let cache = _sessionCache[sid];
    if (!cache) { cache = _sessionCache[sid] = {}; }
    if (!cache.log_entries) cache.log_entries = [];
    cache.log_entries.push(line);
    if (cache.log_entries.length > 1000)
        cache.log_entries.splice(0, cache.log_entries.length - 1000);
    cache.log_cursor = idx;

    if (sid === _sessionId) {
        const box = document.getElementById('log-box');
        box.value += line + '\n';
        box.scrollTop = box.scrollHeight;
        _logCursor = idx;
    }
}

function _handleTaskEvent(m) {
    _taskCache[m.tid] = m;
    const w = _taskWatchers[m.tid];
    if (m.status === 'running') {
        if (w) try { w.onUpdate(m); } catch (e) { console.error(e); }
    } else {
        _taskFinished[m.tid] = m;
        if (_runningTaskId === m.tid) {
            _runningTaskId = null;
            _runningTaskType = null;
        }
        if (w) {
            delete _taskWatchers[m.tid];
            try { w.onDone(m); } catch (e) { console.error(e); }
        }
    }
}

// Replaces the old startPoll. Registers handlers; the SSE event loop dispatches.
function watchTask(taskType, taskId, onUpdate, onDone) {
    _ensureLogStream();
    _runningTaskId = taskId;
    _runningTaskType = taskType;
    // If the SSE has already delivered a terminal event for this task, fire
    // onDone immediately (handles fast tasks that finish before we register).
    const fin = _taskFinished[taskId];
    if (fin) {
        delete _taskFinished[taskId];
        _runningTaskId = null;
        _runningTaskType = null;
        try { onDone(fin); } catch (e) { console.error(e); }
        return;
    }
    _taskWatchers[taskId] = { onUpdate, onDone };
    // If a running snapshot was already cached, replay it once so handlers
    // can initialize their UI from the latest known state.
    const cached = _taskCache[taskId];
    if (cached && cached.status === 'running') {
        try { onUpdate(cached); } catch (e) { console.error(e); }
    }
}

function stopPoll() {
    if (_runningTaskId) delete _taskWatchers[_runningTaskId];
    _runningTaskId = null;
    _runningTaskType = null;
}

async function cancelRunningTask() {
    if (!_sessionId || !_runningTaskId) return;
    const taskType = _runningTaskType;
    await fetch(`/api/session/${_sessionId}/task/${_runningTaskId}/cancel`, { method: 'POST' });
    log(`[${taskType}] Cancel requested`);
}

function resetUI() {
    document.getElementById('v1-path-input').value = '';
    document.getElementById('v2-path-input').value = '';
    document.getElementById('out-path-input').value = '';
    document.getElementById('out-path-input').dataset.lastAutoPath = '';
    document.getElementById('v1-file-info').textContent = '';
    document.getElementById('v2-file-info').textContent = '';
    document.getElementById('v1-tracks').innerHTML = '<span class="text-dim text-sm">Load Video 1...</span>';
    document.getElementById('v2-tracks').innerHTML = '<span class="text-dim text-sm">Load Video 2...</span>';
    for (const n of [1, 2]) {
        document.getElementById(`v${n}-sync-track`).innerHTML = '<option value="0">0: (default)</option>';
    }
    document.getElementById('align-progress').classList.remove('progress-indeterminate');
    setProgress('align-progress', 0, '');
    document.querySelector('input[name="container-fmt"][value="mkv"]').checked = true;
    document.getElementById('atempo-input').value = '1.000000';
    document.getElementById('offset-input').value = '0.000';
    state.offsetEdited = false;
    state.atempoEdited = false;
    for (const k of ['r-mode','r-atempo','r-offset','r-inliers','r-fit','r-precision','r-visual']) {
        const el = document.getElementById(k);
        el.textContent = '-';
        el.style.color = '';
    }
    document.getElementById('results-detail').value = '';
    document.getElementById('segment-overrides').innerHTML = '';
    setProgress('merge-progress', 0, '');
    document.getElementById('log-box').value = '';
    document.getElementById('gain-match-cb').disabled = true;
    document.getElementById('gain-match-cb').checked = false;
    document.getElementById('gain-match-label').title = 'Loudness must be measured first!';
    document.getElementById('gain-match-label').style.opacity = '0.5';
    unlockButtons();
    updateMergeButton();
    updateSyncPanels();
}

function resetState() {
    state.v1 = { path: '', tracks: [], streams: [], duration: 0 };
    state.v2 = { path: '', tracks: [], streams: [], duration: 0 };
    state.selected = { v1: {}, v2: {} };
    state.trackOverrides = {};
    state.defaultAudioIdx = null;
    state.mergeParams = null;
    state.segments = null;
    state.containerChange = false;
    state.containerExt = '';
    state.v1Lufs = null;
    state.v2Lufs = null;
}

async function loadVideo(n) {
    const pathInput = document.getElementById(`v${n}-path-input`);
    const filepath = pathInput.value.trim();
    if (!filepath) return;

    const fileInfo = document.getElementById(`v${n}-file-info`);
    fileInfo.textContent = 'Probing...';
    log(`[V${n}] Probing ${basename(filepath)}...`);

    const result = await apiPost('/api/probe', { filepath });
    if (result.error) {
        fileInfo.textContent = 'Error';
        log(`[V${n}] Probe error: ${result.error}`);
        return;
    }
    if (result.warning) {
        log(`[V${n}] ${result.warning}`);
    }

    const streamCounts = (result.streams || []).reduce((acc, s) => {
        acc[s.codec_type] = (acc[s.codec_type] || 0) + 1;
        return acc;
    }, {});
    const countStr = Object.entries(streamCounts).map(([k,v]) => `${v} ${k}`).join(', ');
    log(`[V${n}] ${basename(filepath)}: ${countStr}, ${result.duration_fmt}`);

    state[`v${n}`] = {
        path: filepath,
        tracks: result.tracks,
        streams: (result.streams || []).filter(s => !s.empty),
        duration: result.duration,
    };

    document.getElementById(`v${n}-file-info`).textContent = `${basename(filepath)} \u2014 ${result.duration_fmt}`;

    if (n === 1) {
        state.containerChange = result.container_change;
        state.containerExt = result.container_ext;
        if (result.container_change) {
            log(`[V1] Container '${result.container_ext}' does not support multi-audio, output will use .mkv`);
        }
        updateOutputPathIfDefault();
    }

    fillStreamPanel(n);
    updateSyncCombos();
    updateMergeButton();
    updateSyncPanels();
    await ensureSession();
    saveUIState();
}

async function testInterleave() {
    const filepath = state.v1.path;
    if (!filepath) { alert('Load Video 1 first.'); return; }
    if (_runningTaskId) { alert('A task is running. Stop it first.'); return; }

    await ensureSession();
    log('[Test] Analysing interleave structure...');
    lockButtons('test');

    const result = await apiPost(`/api/session/${_sessionId}/test-interleave`, { filepath });
    if (result.error) {
        log(`[Test] Error: ${result.error}`);
        unlockButtons();
        return;
    }

    watchTask('test', result.task_id,
        (task) => {},
        (task) => {
            unlockButtons();
            if (task.status === 'done' && task.result) {
                for (const line of task.result.lines) {
                    log(`[Test] ${line}`);
                }
            } else if (task.status === 'cancelled') {
                log('[Test] Cancelled.');
            } else {
                log(`[Test] Error: ${task.error || 'Unknown error'}`);
            }
        }
    );
}

async function clearVideo2() {
    if (_runningTaskId) { alert('A task is running. Stop it first.'); return; }
    state.v2 = { path: '', tracks: [], streams: [], duration: 0 };
    state.selected.v2 = {};
    state.mergeParams = null;
    state.segments = null;
    state.v2Lufs = null;
    state.defaultAudioIdx = null;
    // Remove V2 track overrides
    for (const key of Object.keys(state.trackOverrides)) {
        if (key.startsWith('v2_')) delete state.trackOverrides[key];
    }
    document.getElementById('v2-path-input').value = '';
    document.getElementById('v2-file-info').textContent = '';
    document.getElementById('v2-tracks').innerHTML = '<span class="text-dim text-sm">Load Video 2...</span>';
    document.getElementById('v2-sync-track').innerHTML = '<option value="0">0: (default)</option>';
    document.getElementById('atempo-input').value = '1.000000';
    document.getElementById('offset-input').value = '0.000';
    state.offsetEdited = false;
    state.atempoEdited = false;
    document.getElementById('segment-overrides').innerHTML = '';
    document.getElementById('global-offset-row').style.display = '';
    for (const k of ['r-mode','r-atempo','r-offset','r-inliers','r-fit','r-precision','r-visual']) {
        const el = document.getElementById(k);
        el.textContent = '-';
        el.style.color = '';
    }
    document.getElementById('results-detail').value = '';
    document.getElementById('align-progress').classList.remove('progress-indeterminate');
    setProgress('align-progress', 0, '');
    updateOutputPathIfDefault();
    updateMergeButton();
    updateSyncPanels();
    if (_sessionId) {
        await ensureSession();
        saveUIState();
    }
    log('[V2] Cleared');
}

function streamLabel(s) {
    const si = s.stream_index;
    const type = s.codec_type;
    const codec = s.codec || '?';
    const lang = s.language && s.language !== 'und' ? `[${s.language}]` : '';
    const title = s.title ? ` "${s.title}"` : '';
    if (type === 'video') {
        return `#${si} video: ${codec} ${s.width || '?'}x${s.height || '?'}`;
    } else if (type === 'audio') {
        return `#${si} audio: ${lang} ${codec}, ${s.channels || '?'}ch, ${s.sample_rate || '?'}Hz${title}`;
    } else if (type === 'subtitle') {
        return `#${si} subtitle: ${lang} ${codec}${title}`;
    } else {
        return `#${si} ${type}: ${codec}${title}`;
    }
}

function overrideLabel(key) {
    const ovr = state.trackOverrides[key] || {};
    const lang = ovr.language || '';
    const langStr = lang && LANG_NAMES[lang] ? ` \u2192 ${LANG_NAMES[lang]}` : '';
    const titleStr = ovr.title ? ` "${ovr.title}"` : '';
    return langStr + titleStr;
}

function fillStreamPanel(n) {
    const container = document.getElementById(`v${n}-tracks`);
    container.innerHTML = '';
    const streams = (state[`v${n}`].streams || []).filter(s =>
        (n === 1 && s.codec_type === 'video') ||
        s.codec_type === 'audio' || s.codec_type === 'subtitle' || s.codec_type === 'attachment');
    const sel = state.selected[`v${n}`];
    const src = `v${n}`;

    if (!streams.length) {
        container.innerHTML = '<span class="text-dim text-sm">No streams found</span>';
        return;
    }

    const selBar = document.createElement('div');
    selBar.style.cssText = 'margin-bottom:4px;font-size:11px;display:flex;gap:8px;';
    const selAll = document.createElement('a');
    selAll.textContent = 'Select all';
    selAll.href = '#';
    selAll.style.color = 'var(--ac)';
    selAll.addEventListener('click', e => { e.preventDefault(); container.querySelectorAll('input[type=checkbox]').forEach(cb => { const si = parseInt(cb.dataset.si); cb.checked = true; sel[si] = true; }); saveUIState(); });
    const selNone = document.createElement('a');
    selNone.textContent = 'Select none';
    selNone.href = '#';
    selNone.style.color = 'var(--ac)';
    selNone.addEventListener('click', e => { e.preventDefault(); container.querySelectorAll('input[type=checkbox]').forEach(cb => { const si = parseInt(cb.dataset.si); cb.checked = false; sel[si] = false; }); saveUIState(); });
    selBar.appendChild(selAll);
    selBar.appendChild(selNone);
    container.appendChild(selBar);

    let lastType = '';
    streams.forEach(s => {
        if (s.codec_type !== lastType) {
            const hdr = document.createElement('p');
            hdr.style.cssText = 'color:var(--dim);font-size:11px;margin:8px 0 4px;font-weight:600;';
            hdr.textContent = s.codec_type === 'video' ? 'Video' : s.codec_type === 'audio' ? 'Audio' : s.codec_type === 'subtitle' ? 'Subtitles' : 'Attachments';
            container.appendChild(hdr);
            lastType = s.codec_type;
        }
        const si = s.stream_index;
        const key = `${src}_s${si}`;
        const isVideo = s.codec_type === 'video';
        if (isVideo) sel[si] = true;
        else if (sel[si] === undefined) sel[si] = true;
        const label = document.createElement('label');
        label.className = 'track-item';
        label.style.cursor = 'pointer';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = sel[si];
        cb.dataset.si = si;
        if (isVideo) {
            cb.disabled = true;
            cb.title = 'Video track is always included';
        } else {
            cb.addEventListener('change', () => { sel[si] = cb.checked; saveUIState(); });
        }
        label.appendChild(cb);
        label.appendChild(document.createTextNode(` ${streamLabel(s)}${overrideLabel(key)}`));
        container.appendChild(label);
    });
}

function updateSyncCombos() {
    for (const n of [1, 2]) {
        const combo = document.getElementById(`v${n}-sync-track`);
        const tracks = state[`v${n}`].tracks;
        combo.innerHTML = '';
        if (tracks && tracks.length > 0) {
            tracks.forEach(t => {
                const opt = document.createElement('option');
                opt.value = t.index;
                opt.textContent = `${t.index}: ${streamLabel(t)}`;
                combo.appendChild(opt);
            });
        } else {
            const opt = document.createElement('option');
            opt.value = 0;
            opt.textContent = '0: (default)';
            combo.appendChild(opt);
        }
    }
}

function getSelectedIndices(n, codecType) {
    const sel = state.selected[`v${n}`] || {};
    const streams = state[`v${n}`].streams || [];
    if (!codecType) {
        const selected = Object.entries(sel).filter(([_, v]) => v).map(([k]) => parseInt(k)).filter(x => !isNaN(x)).sort((a, b) => a - b);
        // Always include video streams (not shown in UI checkboxes)
        const videoIndices = streams.filter(s => s.codec_type === 'video').map(s => s.stream_index);
        for (const vi of videoIndices) {
            if (!selected.includes(vi)) selected.push(vi);
        }
        return selected.sort((a, b) => a - b);
    }
    return streams.filter(s => s.codec_type === codecType && sel[s.stream_index]).map(s => s.stream_index);
}


function collectMetadata(n, codecType) {
    const result = [];
    const sel = state.selected[`v${n}`] || {};
    for (const s of state[`v${n}`].streams || []) {
        if (s.codec_type !== codecType) continue;
        if (!sel[s.stream_index]) continue;
        const key = `v${n}_s${s.stream_index}`;
        const ovr = state.trackOverrides[key] || {};
        result.push({ language: ovr.language || s.language || 'und', title: ovr.title !== undefined ? ovr.title : (s.title || '') });
    }
    return result;
}

async function ensureSession() {
    if (_sessionId) return _sessionId;
    const res = await apiPost('/api/sessions', {});
    _sessionId = res.session_id;
    sessionStorage.setItem('audiosync_session', _sessionId);
    _sessionCache[_sessionId] = { label: 'New session', tasks: {}, active_task: null, ui_state: {}, version: 0, created_at: 0, log_entries: [], log_cursor: 0 };
    _logCursor = 0;
    await refreshSessionList();
    return _sessionId;
}

async function runAlign() {
    if (_runningTaskId) { alert('A task is already running. Stop it first.'); return; }
    if (!state.v1.path || !state.v2.path) { alert('Load both videos first.'); return; }

    await ensureSession();
    const myView = ++_viewId;

    lockButtons('align');
    setProgress('align-progress', 0, '0%');
    logSeparator('Align');
    log('[Align] Starting...');

    const t1 = parseInt(document.getElementById('v1-sync-track').value) || 0;
    const t2 = parseInt(document.getElementById('v2-sync-track').value) || 0;

    const vocalFilter = document.getElementById('vocal-filter-cb').checked;
    const measureLufs = document.getElementById('measure-lufs-cb').checked;

    const result = await apiPost(`/api/session/${_sessionId}/align`, {
        v1_path: state.v1.path, v2_path: state.v2.path,
        v1_track: t1, v2_track: t2,
        vocal_filter: vocalFilter,
        measure_lufs: measureLufs,
        v1_streams: state.v1.streams,
        v2_streams: state.v2.streams,
        v1_tracks: state.v1.tracks,
        v2_tracks: state.v2.tracks,
        v1_duration: state.v1.duration,
        v2_duration: state.v2.duration,
    });

    if (_viewId !== myView) return;

    if (result.error) {
        unlockButtons();
        setProgress('align-progress', 0, '');
        log(`[Align] Error: ${result.error}`);
        return;
    }

    refreshSessionList();

    watchTask('align', result.task_id,
        (task) => {
            if (_viewId !== myView) return;
            if (typeof task.percent === 'number' && task.percent >= 0)
                setProgress('align-progress', task.percent);
        },
        (task) => {
            if (_viewId !== myView) return;
            unlockButtons();
            setProgress('align-progress', task.status === 'done' ? 100 : 0, task.status === 'done' ? 'Done' : '');
            if (task.status === 'done') {
                showAlignResults(task.result);
                if (task.result.warnings && task.result.warnings.length > 0) {
                    for (const w of task.result.warnings)
                        log(`[Align] WARNING: ${w}`);
                }
                const sr = task.result.speed_ratio, off = task.result.offset;
                log(`[Align] Done: ${task.result.inlier_count} inliers, atempo=${sr != null ? sr.toFixed(6) : '?'}, offset=${off != null ? off.toFixed(3) : '?'}s`);
            } else {
                log(`[Align] ${task.status}: ${task.error || ''}`);
            }
            refreshSessionList();
        }
    );
}

function showAlignResults(r) {
    const at = r.speed_ratio, off = r.offset;
    const a = r.linear_a, b = r.linear_b;
    const ni = r.inlier_count, nt = r.total_candidates;
    const st = r.sync_tracks || [0, 0];

    state.v1Lufs = r.v1_lufs ?? null;
    state.v2Lufs = r.v2_lufs ?? null;
    if (state.v1Lufs != null && state.v2Lufs != null) {
        log(`[Align] Loudness: V1=${state.v1Lufs.toFixed(1)} LUFS, V2=${state.v2Lufs.toFixed(1)} LUFS (delta=${(state.v1Lufs - state.v2Lufs).toFixed(1)} dB)`);
        document.getElementById('gain-match-cb').disabled = false;
        const gml = document.getElementById('gain-match-label');
        gml.title = '';
        gml.style.opacity = '';
    } else {
        document.getElementById('gain-match-cb').disabled = true;
        document.getElementById('gain-match-cb').checked = false;
        const gml = document.getElementById('gain-match-label');
        gml.title = 'Loudness must be measured first!';
        gml.style.opacity = '0.5';
    }

    document.getElementById('atempo-input').value = at.toFixed(6);
    document.getElementById('offset-input').value = off.toFixed(3);
    state.offsetEdited = false;
    state.atempoEdited = false;

    let modeText = `AUDIO (tracks ${st[0]}\u2194${st[1]})`;
    if (r.mode === 'audio-xcorr') modeText = `XCORR (tracks ${st[0]}\u2194${st[1]})`;
    document.getElementById('r-mode').textContent = modeText;
    document.getElementById('r-mode').style.color = 'var(--blue)';
    document.getElementById('r-atempo').textContent = at.toFixed(6);
    document.getElementById('r-offset').textContent = `${off >= 0 ? '+' : ''}${off.toFixed(3)}s`;
    document.getElementById('r-inliers').textContent = `${ni}/${nt}`;
    document.getElementById('r-fit').textContent = `t1=${a.toFixed(6)}*t2+${b.toFixed(3)}`;

    const rmean = r.residual_mean || 0, rmax = r.residual_max || 0, rend = r.residual_end || 0;
    const precEl = document.getElementById('r-precision');
    precEl.textContent = `avg=${rmean.toFixed(3)}s  max=${rmax.toFixed(3)}s  end=${rend.toFixed(3)}s`;
    precEl.style.color = rmax < 0.5 ? 'var(--green)' : rmax < 1.5 ? 'var(--warn)' : 'var(--err)';

    const vizEl = document.getElementById('r-visual');
    if (r.visual_refined_offset != null) {
        const vrSign = r.visual_refined_offset >= 0 ? '+' : '';
        vizEl.textContent = `fine-tuned: ${vrSign}${r.visual_refined_offset.toFixed(3)}s`;
        vizEl.style.color = 'var(--blue)';
    } else {
        vizEl.textContent = 'no match';
        vizEl.style.color = 'var(--dim)';
    }

    state.segments = r.segments || null;
    renderSegmentOverrides();

    const detail = document.getElementById('results-detail');
    const v1c = r.v1_coverage || [0,0], v2c = r.v2_coverage || [0,0];
    const v1i = r.v1_interval || 0, v2i = r.v2_interval || 0;

    const aoSpd = r.audio_speed || at;
    const aoOff = r.audio_offset !== undefined ? r.audio_offset : off;
    const fmt = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(3)}s`;
    let txt = `Audio coarse offset: ${fmt(aoOff)}\n`;
    if (r.ransac_offset != null) {
        txt += `Audio fine offset:   ${fmt(r.ransac_offset)}\n`;
    }
    if (r.visual_refined_offset != null) {
        txt += `Visual fine offset:  ${fmt(r.visual_refined_offset)}\n`;
    }
    txt += `Speed:               ${(1.0/aoSpd).toFixed(6)}\n`;
    if (r.v1_fps > 0 && r.v2_fps > 0) {
        const v1f = r.v1_fps > 0 ? r.v1_fps.toFixed(3) : '?';
        const v2f = r.v2_fps > 0 ? r.v2_fps.toFixed(3) : '?';
        txt += `Framerate:        V1=${v1f}  V2=${v2f}`;
        if (r.fps_adjusted) txt += '  (atempo snapped to fps ratio)';
        txt += '\n';
    }
    if (r.v2_start_delay > 0.01) {
        txt += `V2 start delay:   ${r.v2_start_delay.toFixed(3)}s\n`;
    }
    txt += '\u2500'.repeat(38) + '\n';
    txt += `V1 hop: ${v1i.toFixed(2)}s  V2 hop: ${v2i.toFixed(2)}s\n`;
    txt += `V1: ${formatTimestamp(v1c[0])} - ${formatTimestamp(v1c[1])}\n`;
    txt += `V2: ${formatTimestamp(v2c[0])} - ${formatTimestamp(v2c[1])}\n`;
    txt += `Residual: avg=${rmean.toFixed(3)}s max=${rmax.toFixed(3)}s\n`;

    const segs = r.segments || [];
    if (segs.length > 1) {
        txt += '\u2500'.repeat(38) + '\n';
        txt += `SEGMENTS: ${segs.length} (content breaks detected)\n`;
        for (let i = 0; i < segs.length; i++) {
            const s = segs[i];
            const sEnd = s.v1_end >= 1e9 ? 'end' : formatTimestamp(s.v1_end);
            txt += `  #${i+1}: ${formatTimestamp(s.v1_start)} - ${sEnd}  offset=${s.offset >= 0 ? '+' : ''}${s.offset.toFixed(3)}s  (${s.n_inliers} matches)\n`;
        }
    }

    txt += '\u2500'.repeat(38) + '\n';
    const pairs = r.inlier_pairs || [];
    const step = Math.max(1, Math.floor(pairs.length / 10));
    txt += `${'V1'.padStart(10)} ${'V2'.padStart(10)} ${'Sim'.padStart(6)}\n`;
    for (let i = 0; i < pairs.length; i += step) {
        const [t1, t2, sim] = pairs[i];
        txt += `${formatTimestamp(t1).padStart(10)} ${formatTimestamp(t2).padStart(10)} ${sim.toFixed(3)}\n`;
    }
    detail.value = txt;

    const segMsg = segs.length > 1 ? ` (${segs.length} segments)` : '';
    log(`[Align] Alignment complete${segMsg}`);
}

function reorderAudioTracks(metadata, defaultIdx) {
    if (metadata.length <= 1) return { sorted: metadata, order: metadata.map((_, i) => i) };
    const defIdx = (defaultIdx != null && defaultIdx < metadata.length) ? defaultIdx : 0;
    const defLang = metadata[defIdx].language || 'und';
    const defGroup = [];
    const rest = [];
    for (let i = 0; i < metadata.length; i++) {
        if (i === defIdx) {
            defGroup.unshift({ meta: metadata[i], orig: i });
        } else if ((metadata[i].language || 'und') === defLang) {
            defGroup.push({ meta: metadata[i], orig: i });
        } else {
            rest.push({ meta: metadata[i], orig: i });
        }
    }
    rest.sort((a, b) => {
        const na = LANG_NAMES[a.meta.language] || a.meta.language || '';
        const nb = LANG_NAMES[b.meta.language] || b.meta.language || '';
        return na.localeCompare(nb);
    });
    const combined = [...defGroup, ...rest];
    return { sorted: combined.map(e => e.meta), order: combined.map(e => e.orig) };
}

function prepareMerge(atempo, offset) {
    const v1 = state.v1.path, v2 = state.v2.path;
    if (!v1) return;
    const outInput = document.getElementById('out-path-input').value.trim();
    const outPath = outInput || getDefaultOutputPath();
    const v1StreamIndices = getSelectedIndices(1);
    const metadata = collectMetadata(1, 'audio');
    const sub_metadata = collectMetadata(1, 'subtitle');
    const v1_vid_metadata = collectMetadata(1, 'video');

    if (isRemuxMode()) {
        const { sorted: sortedMeta, order: audioOrder } = reorderAudioTracks(metadata, state.defaultAudioIdx);
        const params = { v1_path: v1, out_path: outPath, v1_stream_indices: v1StreamIndices, v1_duration: state.v1.duration, v1_streams: state.v1.streams, v1_tracks: state.v1.tracks, metadata: sortedMeta, sub_metadata, v1_vid_metadata, default_audio: 0, audio_order: audioOrder, v1_has_attachments: getSelectedIndices(1, 'attachment').length > 0 };
        state.mergeParams = params;
        log(`[Remux] Ready \u2192 ${basename(outPath)}`);
        return;
    }
    const v2AudioMeta = collectMetadata(2, 'audio');
    const allAudioMeta = [...metadata, ...v2AudioMeta];
    const v2StreamIndices = getSelectedIndices(2);
    const v2_sub_metadata = collectMetadata(2, 'subtitle');
    const { sorted: sortedMeta, order: audioOrder } = reorderAudioTracks(allAudioMeta, state.defaultAudioIdx);
    const gainMatch = document.getElementById('gain-match-cb').checked;
    const params = { v1_path: v1, v2_path: v2, out_path: outPath, atempo, offset, v1_stream_indices: v1StreamIndices, v2_stream_indices: v2StreamIndices, v2_sub_metadata, v1_duration: state.v1.duration, v1_streams: state.v1.streams, v1_tracks: state.v1.tracks, v2_streams: state.v2.streams, v2_tracks: state.v2.tracks, metadata: sortedMeta, sub_metadata, v1_vid_metadata, default_audio: 0, audio_order: audioOrder, gain_match: gainMatch, v1_lufs: state.v1Lufs, v2_lufs: state.v2Lufs, v1_has_attachments: getSelectedIndices(1, 'attachment').length > 0, v2_has_attachments: getSelectedIndices(2, 'attachment').length > 0 };
    if (state.segments && state.segments.length > 1) {
        params.segments = state.segments;
    }
    state.mergeParams = params;
    const segMsg = (state.segments && state.segments.length > 1) ? ` (${state.segments.length} segments)` : '';
    log(`[Merge] Ready: atempo=${atempo.toFixed(6)}, offset=${offset.toFixed(3)}s${segMsg} \u2192 ${basename(outPath)}`);
}

function renderSegmentOverrides() {
    const container = document.getElementById('segment-overrides');
    const segs = state.segments;
    const globalRow = document.getElementById('global-offset-row');
    if (!segs || segs.length <= 1) {
        container.innerHTML = '';
        if (globalRow) globalRow.style.display = '';
        return;
    }
    if (globalRow) globalRow.style.display = 'none';
    let html = '<div class="text-dim text-sm mt-4" style="margin-bottom:4px">Segment offsets:</div>';
    for (let i = 0; i < segs.length; i++) {
        const s = segs[i];
        const sEnd = s.v1_end >= 1e9 ? 'end' : formatTimestamp(s.v1_end);
        html += `<div class="row" style="gap:4px">`;
        html += `<span class="text-sm text-dim" style="min-width:90px">#${i+1} (${formatTimestamp(s.v1_start)}-${sEnd}):</span>`;
        html += `<input type="text" class="seg-offset-input" data-seg="${i}" value="${s.offset.toFixed(3)}" style="width:100px">`;
        html += `<span class="text-sm text-dim">s</span></div>`;
    }
    container.innerHTML = html;
}

function updateParams() {
    if (isRemuxMode()) {
        prepareMerge(1.0, 0);
        log('[Remux] Parameters updated');
        saveUIState();
        return;
    }
    const at = parseFloat(document.getElementById('atempo-input').value);
    const off = parseFloat(document.getElementById('offset-input').value);
    if (isNaN(at) || isNaN(off)) { alert('Enter valid numbers.'); return; }
    const segInputs = document.querySelectorAll('.seg-offset-input');
    if (segInputs.length > 0 && state.segments && state.segments.length > 1) {
        for (const inp of segInputs) {
            const idx = parseInt(inp.dataset.seg);
            const val = parseFloat(inp.value);
            if (isNaN(val)) { alert(`Invalid offset for segment #${idx+1}.`); return; }
            state.segments[idx].offset = val;
        }
    }
    prepareMerge(at, off);
    log('[Merge] Parameters updated');
    saveUIState();
}

function startMergePoll(taskType, taskId, myView, initialProgress) {
    const isRemux = taskType === 'remux';
    const taskLabel = isRemux ? 'Remux' : 'Merge';
    // Server computes percent (0..100) on every progress event; client just renders it.
    watchTask(taskType, taskId,
        (task) => {
            if (_viewId !== myView) return;
            if (typeof task.percent === 'number' && task.percent >= 0)
                setProgress('merge-progress', task.percent);
        },
        (task) => {
            if (_viewId !== myView) return;
            unlockButtons();
            if (task.status === 'done') {
                setProgress('merge-progress', 100, 'Done');
                log(`[${taskLabel}] Completed in ${task.result.elapsed} \u2192 ${task.result.output}`);
                state.mergeParams = null;
                alert('Output file created!\n' + task.result.output);
            } else {
                setProgress('merge-progress', 0, '');
                log(`[${taskLabel}] ${task.status}: ${task.error || ''}`);
            }
            refreshSessionList();
        }
    );
}

async function runCreateSample() {
    return runMerge(300);
}

async function runMerge(durationLimit) {
    if (_runningTaskId) { alert('A task is already running. Stop it first.'); return; }
    const remux = isRemuxMode();
    const isSample = durationLimit && durationLimit > 0;

    if (remux) {
        if (!state.v1.path) { alert('Load Video 1 first.'); return; }
        prepareMerge(1.0, 0);
    } else {
        const at = parseFloat(document.getElementById('atempo-input').value);
        const off = parseFloat(document.getElementById('offset-input').value);
        if (isNaN(at) || isNaN(off) || !state.v1.path || !state.v2.path) {
            alert('Run Auto-Align first or enter valid values, and load both videos.');
            return;
        }
        // Pick up any per-segment offset edits from inputs (formerly Update Parameters).
        const segInputs = document.querySelectorAll('.seg-offset-input');
        if (segInputs.length > 0 && state.segments && state.segments.length > 1) {
            for (const inp of segInputs) {
                const idx = parseInt(inp.dataset.seg);
                const val = parseFloat(inp.value);
                if (isNaN(val)) { alert(`Invalid offset for segment #${idx+1}.`); return; }
                state.segments[idx].offset = val;
            }
        }
        prepareMerge(at, off);
        if (getSelectedIndices(2, 'audio').length === 0) { alert('Select at least one V2 audio track.'); return; }
    }

    let currentOutPath = document.getElementById('out-path-input').value.trim() || getDefaultOutputPath();
    if (isSample) {
        const dot = currentOutPath.lastIndexOf('.');
        currentOutPath = (dot > 0)
            ? currentOutPath.slice(0, dot) + '.sample' + currentOutPath.slice(dot)
            : currentOutPath + '.sample';
    }
    if (state.mergeParams) {
        state.mergeParams.out_path = currentOutPath;
        state.mergeParams.duration_limit = isSample ? durationLimit : null;
    }

    if (!remux && state.containerChange && !confirm(`Container '${state.containerExt}' doesn't support multi-audio.\nOutput will use .mkv.\n\nContinue?`)) return;

    const existsResult = await apiPost('/api/file-exists', { path: state.mergeParams.out_path });
    if (existsResult.exists) {
        if (!confirm(`Output file already exists:\n${basename(state.mergeParams.out_path)}\n\nOverwrite?`)) return;
    } else {
        const msg = remux ? 'Remux the file now?' : 'Merge the audio tracks now?';
        if (!confirm(msg)) return;
    }

    await ensureSession();
    const myView = ++_viewId;

    lockButtons('merge');
    setProgress('merge-progress', 0, '');
    const taskLabel = isSample ? 'Sample' : (remux ? 'Remux' : 'Merge');
    logSeparator(taskLabel);
    log(`[${taskLabel}] Starting${isSample ? ` (\u2248 ${Math.round(durationLimit/60)} min)` : ''}...`);

    const body = { ...state.mergeParams };
    const endpoint = remux ? 'remux' : 'merge';
    const result = await apiPost(`/api/session/${_sessionId}/${endpoint}`, body);

    if (_viewId !== myView) return;

    if (result.error) {
        unlockButtons();
        log(`[${taskLabel}] Error: ${result.error}`);
        return;
    }

    refreshSessionList();
    startMergePoll(remux ? 'remux' : 'merge', result.task_id, myView);
}

const VIDEO_EXTS = new Set(['.mp4','.mkv','.avi','.mov','.webm','.ts','.flv','.wmv','.m4v','.mts','.m2ts']);

async function browseServer(n) {
    const overlay = document.createElement('div');
    overlay.className = 'fb-overlay';
    overlay.innerHTML = `
        <div class="fb-dialog">
            <div class="fb-header">
                <h3>Browse \u2014 Video ${n}</h3>
                <button class="btn-sm" onclick="this.closest('.fb-overlay').remove()">Close</button>
            </div>
            <div class="fb-path">
                <span class="text-sm">Path:</span>
                <input type="text" id="fb-path-input" placeholder="Enter path and press Enter...">
                <button class="btn-sm" id="fb-go-btn">Go</button>
            </div>
            <div class="fb-list" id="fb-list"></div>
            <div class="fb-footer">
                <button onclick="this.closest('.fb-overlay').remove()">Cancel</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

    const pathInput = overlay.querySelector('#fb-path-input');
    const listEl = overlay.querySelector('#fb-list');

    async function navigateTo(path) {
        listEl.innerHTML = '<div style="padding:10px;color:var(--dim)">Loading...</div>';
        const result = await apiPost('/api/browse', { path });
        if (result.error) {
            listEl.innerHTML = `<div style="padding:10px;color:var(--err)">${escapeHtml(result.error)}</div>`;
            return;
        }
        pathInput.value = result.current || '';
        listEl.innerHTML = '';

        for (const entry of result.entries) {
            const div = document.createElement('div');
            div.className = 'fb-item' + (entry.is_dir ? ' dir' : '');
            const ext = entry.name.includes('.') ? entry.name.substring(entry.name.lastIndexOf('.')).toLowerCase() : '';
            const isVideo = VIDEO_EXTS.has(ext);

            if (entry.is_dir) {
                div.innerHTML = `<span class="icon">\uD83D\uDCC1</span><span>${escapeHtml(entry.name)}</span>`;
                div.addEventListener('click', () => navigateTo(entry.path));
            } else {
                div.innerHTML = `<span class="icon">${isVideo ? '\uD83C\uDFAC' : '\uD83D\uDCC4'}</span><span>${escapeHtml(entry.name)}</span>`;
                if (isVideo) {
                    div.style.cursor = 'pointer';
                    div.addEventListener('click', () => {
                        document.getElementById(`v${n}-path-input`).value = entry.path;
                        overlay.remove();
                        loadVideo(n);
                    });
                } else {
                    div.style.opacity = '0.4';
                    div.style.cursor = 'default';
                }
            }
            listEl.appendChild(div);
        }
    }

    pathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') navigateTo(pathInput.value.trim());
    });
    overlay.querySelector('#fb-go-btn').addEventListener('click', () => {
        navigateTo(pathInput.value.trim());
    });

    const existingPath = document.getElementById(`v${n}-path-input`).value.trim();
    navigateTo(existingPath);
}

async function browseOutputDir() {
    const overlay = document.createElement('div');
    overlay.className = 'fb-overlay';
    overlay.innerHTML = `
        <div class="fb-dialog">
            <div class="fb-header">
                <h3>Browse \u2014 Output Location</h3>
                <button class="btn-sm" onclick="this.closest('.fb-overlay').remove()">Close</button>
            </div>
            <div class="fb-path">
                <span class="text-sm">Path:</span>
                <input type="text" id="fb-path-input" placeholder="Enter path and press Enter...">
                <button class="btn-sm" id="fb-go-btn">Go</button>
            </div>
            <div class="fb-list" id="fb-list"></div>
            <div class="fb-footer">
                <span class="text-sm text-dim" id="fb-selected-name" style="flex:1"></span>
                <button class="btn-accent" id="fb-select-btn">Select</button>
                <button onclick="this.closest('.fb-overlay').remove()">Cancel</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

    const pathInput = overlay.querySelector('#fb-path-input');
    const listEl = overlay.querySelector('#fb-list');
    const selectedLabel = overlay.querySelector('#fb-selected-name');
    let currentDir = '';

    async function navigateTo(path) {
        listEl.innerHTML = '<div style="padding:10px;color:var(--dim)">Loading...</div>';
        const result = await apiPost('/api/browse', { path });
        if (result.error) {
            listEl.innerHTML = `<div style="padding:10px;color:var(--err)">${escapeHtml(result.error)}</div>`;
            return;
        }
        currentDir = result.current || '';
        pathInput.value = currentDir;
        selectedLabel.textContent = currentDir;
        listEl.innerHTML = '';

        for (const entry of result.entries) {
            const div = document.createElement('div');
            div.className = 'fb-item' + (entry.is_dir ? ' dir' : '');
            if (entry.is_dir) {
                div.innerHTML = `<span class="icon">\uD83D\uDCC1</span><span>${escapeHtml(entry.name)}</span>`;
                div.addEventListener('click', () => navigateTo(entry.path));
            } else {
                const ext = entry.name.includes('.') ? entry.name.substring(entry.name.lastIndexOf('.')).toLowerCase() : '';
                const isVideo = VIDEO_EXTS.has(ext);
                div.innerHTML = `<span class="icon">${isVideo ? '\uD83C\uDFAC' : '\uD83D\uDCC4'}</span><span>${escapeHtml(entry.name)}</span>`;
                div.style.cursor = 'pointer';
                div.addEventListener('click', () => {
                    document.getElementById('out-path-input').value = entry.path;
                    overlay.remove();
                });
            }
            listEl.appendChild(div);
        }
    }

    pathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const val = pathInput.value.trim();
            const lastPart = val.replace(/\\/g, '/').split('/').pop() || '';
            if (val && /\.\w{1,4}$/.test(lastPart)) {
                document.getElementById('out-path-input').value = val;
                overlay.remove();
            } else {
                navigateTo(val);
            }
        }
    });
    overlay.querySelector('#fb-go-btn').addEventListener('click', () => {
        navigateTo(pathInput.value.trim());
    });
    overlay.querySelector('#fb-select-btn').addEventListener('click', () => {
        const outInput = document.getElementById('out-path-input');
        const current = outInput.value.trim();
        if (currentDir) {
            const fname = (current ? basename(current) : null) || basename(getDefaultOutputPath());
            if (fname) {
                const sep = currentDir.endsWith('/') || currentDir.endsWith('\\') ? '' : (currentDir.includes('\\') ? '\\' : '/');
                outInput.value = currentDir + sep + fname;
            }
        }
        overlay.remove();
    });

    const existingPath = document.getElementById('out-path-input').value.trim() || state.v1.path || '';
    navigateTo(existingPath);
}

function isRemuxMode() {
    return !state.v2.path;
}

function getDefaultOutputPath() {
    const v1 = state.v1.path;
    if (!v1) return '';
    const dotIdx = v1.lastIndexOf('.');
    const base = dotIdx > 0 ? v1.substring(0, dotIdx) : v1;
    const origExt = dotIdx > 0 ? v1.substring(dotIdx) : '';
    const container = document.querySelector('input[name="container-fmt"]:checked').value;
    const outExt = container === 'mkv' ? '.mkv' : origExt;
    const suffix = isRemuxMode() ? '_remuxed' : '_merged';
    return `${base}${suffix}${outExt}`;
}

function onContainerChange() {
    updateOutputPathIfDefault();
    saveUIState();
}

function updateSyncPanels() {
    const bothLoaded = !!(state.v1.path && state.v2.path);
    for (const id of ['panel-align', 'panel-override', 'panel-results']) {
        document.getElementById(id).classList.toggle('panel-disabled', !bothLoaded);
    }
}

function updateMergeButton() {
    const btn = document.getElementById('merge-btn');
    const title = document.getElementById('merge-panel-title');
    if (isRemuxMode()) {
        btn.textContent = 'Run Remux';
        title.textContent = 'Remux';
    } else {
        btn.textContent = 'Run Merge';
        title.textContent = 'Merge';
    }
}

function updateOutputPathIfDefault() {
    const outInput = document.getElementById('out-path-input');
    const current = outInput.value.trim();
    if (!current || current === outInput.dataset.lastAutoPath) {
        const newPath = getDefaultOutputPath();
        outInput.value = newPath;
        outInput.dataset.lastAutoPath = newPath;
    }
}

function buildLangOptions(selectedCode) {
    return ALL_LANGUAGES.map(([code, name]) =>
        `<option value="${code}"${code === selectedCode ? ' selected' : ''}>${code} \u2014 ${name}</option>`
    ).join('');
}

function buildEditorTrack(key, stream) {
    const ovr = state.trackOverrides[key] || {};
    return {
        key,
        group: stream.codec_type === 'video' ? 'Video' : stream.codec_type === 'subtitle' ? 'Subtitles' : 'Audio',
        label: streamLabel(stream),
        origLang: stream.language || 'und',
        origTitle: stream.title || '',
        curLang: ovr.language || stream.language || 'und',
        curTitle: ovr.title !== undefined ? ovr.title : (stream.title || ''),
    };
}

function buildEditorTracksForSource(n) {
    const tracks = [];
    const sel = state.selected[`v${n}`] || {};
    for (const s of state[`v${n}`].streams || []) {
        if (n === 1 && s.codec_type === 'video') {
            tracks.push(buildEditorTrack(`v${n}_s${s.stream_index}`, s));
            continue;
        }
        if ((s.codec_type === 'audio' || s.codec_type === 'subtitle') && sel[s.stream_index])
            tracks.push(buildEditorTrack(`v${n}_s${s.stream_index}`, s));
    }
    return tracks;
}

function openV1MetadataEditor() {
    openMetadataEditor('Video 1 Track Metadata', buildEditorTracksForSource(1));
}

function openV2MetadataEditor() {
    openMetadataEditor('Video 2 Track Metadata', buildEditorTracksForSource(2));
}

function openMetadataEditor(title, tracks) {
    if (!tracks.length) { alert('No tracks selected.'); return; }

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:1000;display:flex;align-items:center;justify-content:center;';
    let html = `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:20px;min-width:500px;max-width:700px;max-height:80vh;overflow-y:auto;">`;
    html += `<h3 style="color:var(--ac);margin-bottom:12px;">${title}</h3>`;
    html += `<p style="color:var(--dim);font-size:12px;margin-bottom:12px;">Edit language and title for video, audio and subtitle tracks.</p>`;

    let lastGroup = '';
    tracks.forEach(t => {
        if (t.group !== lastGroup) {
            html += `<p style="color:var(--dim);font-size:11px;margin:8px 0 4px;font-weight:600;">${t.group}</p>`;
            lastGroup = t.group;
        }
        const lbl = escapeHtml(t.label);
        html += `<div class="row" style="margin:3px 0;"><span style="font-family:Consolas;font-size:11px;min-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${lbl}</span>`;
        html += `<select class="meta-lang" data-key="${t.key}" data-orig-lang="${t.origLang}" style="width:150px;font-size:11px;">${buildLangOptions(t.curLang)}</select>`;
        html += `<input type="text" class="meta-title" data-key="${t.key}" data-orig-title="${escapeHtml(t.origTitle)}" value="${escapeHtml(t.curTitle)}" style="width:160px;font-size:11px;" placeholder="Title"></div>`;
    });

    html += `<div style="margin-top:16px;display:flex;gap:8px;">`;
    html += `<button class="btn-sm" id="meta-clear-btn">Clear</button>`;
    html += `<button class="btn-sm" id="meta-reset-btn">Reset</button>`;
    html += `<button class="btn-sm" id="meta-auto-btn">Auto</button>`;
    html += `<span style="flex:1"></span>`;
    html += `<button onclick="this.closest('div[style*=fixed]').remove()">Cancel</button>`;
    html += `<button class="btn-green" id="meta-ok-btn">OK</button></div></div>`;
    overlay.innerHTML = html;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

    document.getElementById('meta-clear-btn').addEventListener('click', () => {
        overlay.querySelectorAll('.meta-title').forEach(inp => { inp.value = ''; });
    });

    document.getElementById('meta-reset-btn').addEventListener('click', () => {
        overlay.querySelectorAll('.meta-lang').forEach(sel => { sel.value = sel.dataset.origLang || 'und'; });
        overlay.querySelectorAll('.meta-title').forEach(inp => { inp.value = inp.dataset.origTitle || ''; });
    });

    document.getElementById('meta-auto-btn').addEventListener('click', () => {
        overlay.querySelectorAll('.meta-lang').forEach(sel => {
            const lang = sel.value;
            const titleInput = overlay.querySelector(`.meta-title[data-key="${sel.dataset.key}"]`);
            if (titleInput && lang && lang !== 'und') {
                titleInput.value = LANG_NAMES[lang] || lang;
            }
        });
    });

    document.getElementById('meta-ok-btn').addEventListener('click', () => {
        overlay.querySelectorAll('.meta-lang').forEach(sel => {
            const key = sel.dataset.key;
            if (!state.trackOverrides[key]) state.trackOverrides[key] = {};
            state.trackOverrides[key].language = sel.value;
        });
        overlay.querySelectorAll('.meta-title').forEach(inp => {
            const key = inp.dataset.key;
            if (!state.trackOverrides[key]) state.trackOverrides[key] = {};
            state.trackOverrides[key].title = inp.value.trim();
        });
        overlay.remove();
        if (state.v1.streams.length) fillStreamPanel(1);
        if (state.v2.streams.length) fillStreamPanel(2);
        const at = parseFloat(document.getElementById('atempo-input').value);
        const off = parseFloat(document.getElementById('offset-input').value);
        if (!isNaN(at) && !isNaN(off)) prepareMerge(at, off);
        log(`[Metadata] Updated ${Object.keys(state.trackOverrides).length} track(s)`);
        saveUIState();
    });
}

function openDefaultAudioEditor() {
    const remux = isRemuxMode();
    const audioTracks = [];
    for (const s of state.v1.streams || []) {
        if (s.codec_type !== 'audio' || !state.selected.v1[s.stream_index]) continue;
        const key = `v1_s${s.stream_index}`;
        audioTracks.push({ label: `V1 ${streamLabel(s)}${overrideLabel(key)}`, idx: audioTracks.length });
    }
    if (!remux) {
        for (const s of state.v2.streams || []) {
            if (s.codec_type !== 'audio' || !state.selected.v2[s.stream_index]) continue;
            const key = `v2_s${s.stream_index}`;
            audioTracks.push({ label: `V2 ${streamLabel(s)}${overrideLabel(key)}`, idx: audioTracks.length });
        }
    }
    if (!audioTracks.length) { alert('No audio tracks selected.'); return; }

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:1000;display:flex;align-items:center;justify-content:center;';
    let html = `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:20px;min-width:400px;max-width:600px;max-height:80vh;overflow-y:auto;">`;
    html += `<h3 style="color:var(--ac);margin-bottom:12px;">Default Audio Track</h3>`;
    html += `<p style="color:var(--dim);font-size:12px;margin-bottom:12px;">Select which audio track should be marked as default in the output.</p>`;

    audioTracks.forEach(t => {
        const checked = (state.defaultAudioIdx === t.idx) ? ' checked' : (state.defaultAudioIdx === null && t.idx === 0 ? ' checked' : '');
        html += `<label style="display:flex;align-items:center;gap:8px;margin:4px 0;cursor:pointer;font-family:Consolas;font-size:11px;">`;
        html += `<input type="radio" name="default-audio" value="${t.idx}"${checked}>`;
        html += `${escapeHtml(t.label)}</label>`;
    });

    html += `<div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end;">`;
    html += `<button onclick="this.closest('div[style*=fixed]').remove()">Cancel</button>`;
    html += `<button class="btn-green" id="default-audio-ok-btn">OK</button></div></div>`;
    overlay.innerHTML = html;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

    document.getElementById('default-audio-ok-btn').addEventListener('click', () => {
        const sel = overlay.querySelector('input[name="default-audio"]:checked');
        state.defaultAudioIdx = sel ? parseInt(sel.value) : 0;
        overlay.remove();
        log(`[Default Audio] Track ${state.defaultAudioIdx} selected as default`);
        saveUIState();
    });
}

document.getElementById('out-path-input').addEventListener('change', () => {
    document.getElementById('out-path-input').dataset.lastAutoPath = '';
    saveUIState();
});
document.getElementById('v1-path-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadVideo(1); });
document.getElementById('v2-path-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadVideo(2); });

async function newSession() {
    clearTimeout(_saveTimer);
    stopPoll();
    ++_viewId;
    _sessionId = null;
    sessionStorage.removeItem('audiosync_session');
    resetUI();
    resetState();
    renderSessionList(_sessionCache);
    log('Ready.');
}

async function closeSession() {
    clearTimeout(_saveTimer);
    if (!_sessionId) return;
    if (_runningTaskId) {
        if (!confirm('A task is currently running. Stop it and close this session?')) return;
    }
    const sid = _sessionId;
    await fetch(`/api/session/${sid}`, { method: 'DELETE' });
    delete _sessionCache[sid];
    stopPoll();
    ++_viewId;
    _sessionId = null;
    sessionStorage.removeItem('audiosync_session');
    resetUI();
    resetState();
    renderSessionList(_sessionCache);
    log(`Session closed.`);
}

async function switchToSession(sid, sess) {
    flushUIState();
    stopPoll();
    _restoring = true;
    const myView = ++_viewId;
    _sessionId = sid;
    sessionStorage.setItem('audiosync_session', sid);
    resetUI();
    resetState();
    renderSessionList(_sessionCache);

    // Logs are continuously pushed via SSE into _sessionCache[sid].log_entries
    // for every session, so on switch we just render the cached buffer.
    const cached = _sessionCache[sid];
    _logCursor = (cached && cached.log_cursor) || 0;
    if (cached && cached.log_entries && cached.log_entries.length) {
        const box = document.getElementById('log-box');
        box.value = cached.log_entries.join('\n') + '\n';
        box.scrollTop = box.scrollHeight;
    }

    const ui = sess.ui_state || {};
    let lastAlignResult = null;
    let lastMergeResult = null;
    let activeTask = null;
    let activeTaskType = null;

    for (const [tid, t] of Object.entries(sess.tasks || {})) {
        if (t.type === 'align' && t.status === 'done' && t.result) {
            lastAlignResult = t.result;
        }
        if ((t.type === 'merge' || t.type === 'remux') && t.status === 'done' && t.result) {
            lastMergeResult = t.result;
        }
        if (t.status === 'running') {
            activeTask = tid;
            activeTaskType = t.type;
        }
    }

    /* --- Load videos from ui_state (or fall back to task params) --- */
    const v1Path = ui.v1_path || _extractFromTasks(sess, 'v1_path');
    const v2Path = ui.v2_path || _extractFromTasks(sess, 'v2_path');

    if (v1Path && ui.v1_state && ui.v1_state.path === v1Path) {
        state.v1 = ui.v1_state;
        document.getElementById('v1-path-input').value = v1Path;
        document.getElementById('v1-file-info').textContent = basename(v1Path);
        fillStreamPanel(1);
    } else if (v1Path) {
        document.getElementById('v1-path-input').value = v1Path;
        await loadVideo(1);
        if (_viewId !== myView) { _restoring = false; return; }
    }
    if (v2Path && ui.v2_state && ui.v2_state.path === v2Path) {
        state.v2 = ui.v2_state;
        document.getElementById('v2-path-input').value = v2Path;
        document.getElementById('v2-file-info').textContent = basename(v2Path);
        fillStreamPanel(2);
    } else if (v2Path) {
        document.getElementById('v2-path-input').value = v2Path;
        await loadVideo(2);
        if (_viewId !== myView) { _restoring = false; return; }
    }
    updateSyncCombos();
    updateMergeButton();
    updateSyncPanels();

    /* --- Restore all UI state --- */

    // Stream selections
    if (ui.selected) {
        for (const n of ['v1', 'v2']) {
            for (const [si, val] of Object.entries(ui.selected[n] || {}))
                state.selected[n][parseInt(si)] = val;
        }
        for (const n of [1, 2]) {
            document.querySelectorAll(`#v${n}-tracks input[type=checkbox]`).forEach(cb => {
                const si = parseInt(cb.dataset.si);
                const val = (ui.selected[`v${n}`] || {})[si];
                if (val !== undefined) cb.checked = val;
            });
        }
    }

    // Track metadata overrides
    if (ui.track_overrides && Object.keys(ui.track_overrides).length) {
        state.trackOverrides = { ...ui.track_overrides };
        if (state.v1.streams.length) fillStreamPanel(1);
        if (state.v2.streams.length) fillStreamPanel(2);
    }

    // Default audio track
    if (ui.default_audio_idx !== undefined && ui.default_audio_idx !== null) {
        state.defaultAudioIdx = ui.default_audio_idx;
    }

    // Container format
    if (ui.container_fmt) {
        const radio = document.querySelector(`input[name="container-fmt"][value="${ui.container_fmt}"]`);
        if (radio) radio.checked = true;
    }

    // Vocal filter, measure loudness & sync tracks
    if (ui.vocal_filter !== undefined)
        document.getElementById('vocal-filter-cb').checked = ui.vocal_filter;
    if (ui.measure_lufs !== undefined)
        document.getElementById('measure-lufs-cb').checked = ui.measure_lufs;
    if (ui.v1_lufs != null && ui.v2_lufs != null) {
        document.getElementById('gain-match-cb').disabled = false;
        document.getElementById('gain-match-label').title = '';
        document.getElementById('gain-match-label').style.opacity = '';
        if (ui.gain_match !== undefined)
            document.getElementById('gain-match-cb').checked = ui.gain_match;
    }
    if (ui.v1_lufs !== undefined) state.v1Lufs = ui.v1_lufs;
    if (ui.v2_lufs !== undefined) state.v2Lufs = ui.v2_lufs;
    if (ui.container_change !== undefined) state.containerChange = ui.container_change;
    if (ui.container_ext !== undefined) state.containerExt = ui.container_ext;
    if (ui.v1_sync_track !== undefined)
        document.getElementById('v1-sync-track').value = ui.v1_sync_track;
    if (ui.v2_sync_track !== undefined)
        document.getElementById('v2-sync-track').value = ui.v2_sync_track;

    // Output path
    const outPath = ui.out_path || _extractFromTasks(sess, 'out_path');
    if (outPath)
        document.getElementById('out-path-input').value = outPath;

    // Segment overrides from ui_state
    if (ui.segments && ui.segments.length > 1) {
        state.segments = ui.segments;
    }

    /* --- Restore task results (display only) --- */

    if (lastAlignResult) {
        showAlignResults(lastAlignResult);
        // If ui_state has user-edited segments, override what showAlignResults set
        if (ui.segments && ui.segments.length > 1) {
            state.segments = ui.segments;
            renderSegmentOverrides();
        }
        const rsr = lastAlignResult.speed_ratio, roff = lastAlignResult.offset;
        log(`[Align] Restored: atempo=${rsr != null ? rsr.toFixed(6) : '?'}, offset=${roff != null ? roff.toFixed(3) : '?'}s`);
    }

    // Restore manual atempo/offset edits (overrides align result values)
    if (ui.atempo_edited && ui.atempo !== undefined)
        document.getElementById('atempo-input').value = ui.atempo;
    if (ui.offset_edited && ui.offset !== undefined)
        document.getElementById('offset-input').value = ui.offset;
    state.atempoEdited = !!ui.atempo_edited;
    state.offsetEdited = !!ui.offset_edited;

    if (lastMergeResult) {
        setProgress('merge-progress', 100, 'Done');
        log(`[Merge] Completed: ${lastMergeResult.elapsed} \u2192 ${lastMergeResult.output}`);
    }

    // Fetch fresh session data to check actual task status
    try {
        const freshRes = await fetch(`/api/session/${sid}`);
        if (freshRes.ok) {
            const freshData = await freshRes.json();
            const old = _sessionCache[sid];
            if (old) {
                if (old.log_entries) freshData.log_entries = old.log_entries;
                if (old.log_cursor) freshData.log_cursor = old.log_cursor;
            }
            _sessionCache[sid] = freshData;
            for (const [tid, t] of Object.entries(freshData.tasks || {})) {
                if (t.status === 'running') {
                    activeTask = tid;
                    activeTaskType = t.type;
                }
                // Show results for tasks that completed while we were away
                if (t.type === 'align' && t.status === 'done' && t.result && !lastAlignResult) {
                    lastAlignResult = t.result;
                    showAlignResults(lastAlignResult);
                    if (ui.segments && ui.segments.length > 1) {
                        state.segments = ui.segments;
                        renderSegmentOverrides();
                    }
                }
            }
        }
    } catch (e) {}
    if (_viewId !== myView) { _restoring = false; return; }

    if (activeTask && activeTaskType) {
        lockButtons(activeTaskType);
        const freshTasks = (_sessionCache[sid] && _sessionCache[sid].tasks) || sess.tasks || {};
        const activeTaskData = freshTasks[activeTask] || {};
        const currentProgress = activeTaskData.progress || '';

        if (activeTaskType === 'align') {
            setProgress('align-progress', 0, '0%');
            watchTask('align', activeTask,
                (task) => {
                    if (_viewId !== myView) return;
                    if (typeof task.percent === 'number' && task.percent >= 0)
                        setProgress('align-progress', task.percent);
                },
                (task) => {
                    if (_viewId !== myView) return;
                    unlockButtons();
                    setProgress('align-progress', task.status === 'done' ? 100 : 0, task.status === 'done' ? 'Done' : '');
                    if (task.status === 'done') {
                        showAlignResults(task.result);
                        const sr = task.result.speed_ratio, off = task.result.offset;
                        log(`[Align] Done: ${task.result.inlier_count} inliers, atempo=${sr != null ? sr.toFixed(6) : '?'}, offset=${off != null ? off.toFixed(3) : '?'}s`);
                    } else {
                        log(`[Align] ${task.status}: ${task.error || ''}`);
                    }
                    refreshSessionList();
                }
            );
        } else if (activeTaskType === 'merge' || activeTaskType === 'remux') {
            startMergePoll(activeTaskType, activeTask, myView, currentProgress);
        }
    }
    _restoring = false;
}

function _extractFromTasks(sess, key) {
    const entries = Object.entries(sess.tasks || {});
    for (let i = entries.length - 1; i >= 0; i--) {
        const p = entries[i][1].params || {};
        if (p[key]) return p[key];
    }
    return '';
}

function renderSessionList(sessions) {
    const container = document.getElementById('session-list');
    container.innerHTML = '';

    const newBtn = document.createElement('div');
    newBtn.className = 'session-item' + (_sessionId === null ? ' active' : '');
    newBtn.innerHTML = '<div class="session-label" style="color:var(--ac);">+ New Session</div>';
    newBtn.addEventListener('click', newSession);
    container.appendChild(newBtn);

    const sorted = Object.entries(sessions).sort((a, b) => (b[1].created_at || 0) - (a[1].created_at || 0));
    for (const [sid, sess] of sorted) {
        const div = document.createElement('div');
        div.className = 'session-item' + (_sessionId === sid ? ' active' : '');

        const hasRunning = sess.active_task !== null;
        const dotClass = hasRunning ? 'running' : 'idle';
        const statusText = hasRunning ? 'running' : 'idle';

        div.innerHTML =
            `<div class="session-label">${escapeHtml(sess.label)}</div>` +
            `<div class="session-status"><span class="status-dot ${dotClass}"></span>${statusText}</div>`;
        div.addEventListener('click', () => {
            switchToSession(sid, _sessionCache[sid] || sess);
        });
        container.appendChild(div);
    }
}

async function refreshSessionList() {
    try {
        const res = await fetch('/api/sessions');
        const sessions = await res.json();
        for (const [sid, sess] of Object.entries(sessions)) {
            const old = _sessionCache[sid];
            if (old) {
                if (old.log_entries) sess.log_entries = old.log_entries;
                if (old.log_cursor) sess.log_cursor = old.log_cursor;
            }
        }
        _sessionCache = sessions;
        renderSessionList(_sessionCache);
    } catch (e) {}
}

async function pollActiveSession() {
    if (!_sessionId) return;
    try {
        const res = await fetch(`/api/session/${_sessionId}/version`);
        const { version } = await res.json();
        const cached = _sessionCache[_sessionId];

        if (!cached || cached.version !== version) {
            const fullRes = await fetch(`/api/session/${_sessionId}`);
            const sess = await fullRes.json();
            if (cached) {
                if (cached.log_entries) sess.log_entries = cached.log_entries;
                if (cached.log_cursor) sess.log_cursor = cached.log_cursor;
            }
            _sessionCache[_sessionId] = sess;
            renderSessionList(_sessionCache);
        }
        // Keep log cache warm
        if (!_runningTaskId) await _fetchAndDisplayLogs();
    } catch (e) {}
}

async function init() {
    log('Ready.');
    _ensureLogStream();
    await refreshSessionList();

    const savedSid = sessionStorage.getItem('audiosync_session');
    if (savedSid && _sessionCache[savedSid]) {
        switchToSession(savedSid, _sessionCache[savedSid]);
    } else if (savedSid) {
        sessionStorage.removeItem('audiosync_session');
    }
}

init();
setInterval(pollActiveSession, 10000);
document.getElementById('offset-input').addEventListener('input', () => { state.offsetEdited = true; });
document.getElementById('atempo-input').addEventListener('input', () => { state.atempoEdited = true; });
