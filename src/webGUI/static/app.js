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
};

let _sessionId = null;
let _viewId = 0;
let _runningTaskId = null;
let _runningTaskType = null;
let _pollInterval = null;
let _sessionCache = {};
let _saveTimer = null;
let _restoring = false;

function saveUIState() {
    if (_restoring || !_sessionId) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        if (!_sessionId) return;
        const uiState = {
            v1_path: document.getElementById('v1-path-input').value,
            v2_path: document.getElementById('v2-path-input').value,
            out_path: document.getElementById('out-path-input').value,
            selected: { v1: {...state.selected.v1}, v2: {...state.selected.v2} },
            track_overrides: { ...state.trackOverrides },
            default_audio_idx: state.defaultAudioIdx,
            container_fmt: document.querySelector('input[name="container-fmt"]:checked').value,
            atempo: document.getElementById('atempo-input').value,
            offset: document.getElementById('offset-input').value,
            vocal_filter: document.getElementById('vocal-filter-cb').checked,
            v1_sync_track: document.getElementById('v1-sync-track').value,
            v2_sync_track: document.getElementById('v2-sync-track').value,
            segments: state.segments,
            gain_match: document.getElementById('gain-match-cb').checked,
            v1_lufs: state.v1Lufs,
            v2_lufs: state.v2Lufs,
            container_change: state.containerChange,
            container_ext: state.containerExt,
            log_entries: (_sessionCache[_sessionId] && _sessionCache[_sessionId].log_entries)
                ? _sessionCache[_sessionId].log_entries.slice(-200)
                : [],
        };
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
    }, 300);
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

async function apiPost(url, data) {
    const res = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    });
    if (!res.ok) {
        try { return await res.json(); } catch { return { error: `HTTP ${res.status}` }; }
    }
    try { return await res.json(); } catch { return { error: 'Invalid JSON response' }; }
}

function lockButtons(runningType) {
    document.getElementById('align-btn').disabled = true;
    document.getElementById('merge-btn').disabled = true;
    if (runningType === 'align') {
        document.getElementById('align-stop').classList.remove('hidden');
    } else if (runningType === 'merge' || runningType === 'remux') {
        document.getElementById('merge-stop').classList.remove('hidden');
    }
}

function unlockButtons() {
    document.getElementById('align-btn').disabled = false;
    document.getElementById('merge-btn').disabled = false;
    document.getElementById('align-stop').classList.add('hidden');
    document.getElementById('merge-stop').classList.add('hidden');
}

function startPoll(taskType, taskId, onUpdate, onDone, initialProgress) {
    stopPoll();
    _runningTaskId = taskId;
    _runningTaskType = taskType;
    let lastProgress = initialProgress || '';
    let delay = 500;
    function schedulePoll() {
        _pollInterval = setTimeout(async () => {
            try {
                const res = await fetch(`/api/session/${_sessionId}/task/${taskId}`);
                if (!res.ok) {
                    stopPoll();
                    onDone({ status: 'error', error: 'Task not found' });
                    return;
                }
                const task = await res.json();
                if (task.status === 'running') {
                    if (task.progress !== lastProgress) {
                        lastProgress = task.progress || '';
                        onUpdate(task);
                        delay = 500;
                    } else {
                        delay = Math.min(delay * 1.5, 3000);
                    }
                    schedulePoll();
                } else {
                    stopPoll();
                    onDone(task);
                }
            } catch (e) {
                stopPoll();
                onDone({ status: 'error', error: 'Connection lost' });
            }
        }, delay);
    }
    schedulePoll();
}

function stopPoll() {
    if (_pollInterval) {
        clearTimeout(_pollInterval);
        _pollInterval = null;
    }
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
    document.getElementById('align-progress').querySelector('.fill').style.width = '0%';
    document.querySelector('input[name="container-fmt"][value="mkv"]').checked = true;
    document.getElementById('atempo-input').value = '1.000000';
    document.getElementById('offset-input').value = '0.000';
    for (const k of ['r-mode','r-atempo','r-offset','r-inliers','r-fit','r-precision','r-visual']) {
        const el = document.getElementById(k);
        el.textContent = '-';
        el.style.color = '';
    }
    document.getElementById('results-detail').value = '';
    document.getElementById('segment-overrides').innerHTML = '';
    document.getElementById('merge-progress').querySelector('.fill').style.width = '0%';
    document.getElementById('log-box').value = '';
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

    const streamCounts = (result.streams || []).reduce((acc, s) => {
        acc[s.codec_type] = (acc[s.codec_type] || 0) + 1;
        return acc;
    }, {});
    const countStr = Object.entries(streamCounts).map(([k,v]) => `${v} ${k}`).join(', ');
    log(`[V${n}] ${basename(filepath)}: ${countStr}, ${result.duration_fmt}`);

    state[`v${n}`] = {
        path: filepath,
        tracks: result.tracks,
        streams: result.streams || [],
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
    document.getElementById('segment-overrides').innerHTML = '';
    document.getElementById('global-offset-row').style.display = '';
    for (const k of ['r-mode','r-atempo','r-offset','r-inliers','r-fit','r-precision','r-visual']) {
        const el = document.getElementById(k);
        el.textContent = '-';
        el.style.color = '';
    }
    document.getElementById('results-detail').value = '';
    document.getElementById('align-progress').classList.remove('progress-indeterminate');
    document.getElementById('align-progress').querySelector('.fill').style.width = '0%';
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
            hdr.textContent = s.codec_type === 'audio' ? 'Audio' : s.codec_type === 'subtitle' ? 'Subtitles' : 'Attachments';
            container.appendChild(hdr);
            lastType = s.codec_type;
        }
        const si = s.stream_index;
        const key = `${src}_s${si}`;
        if (sel[si] === undefined) sel[si] = true;
        const label = document.createElement('label');
        label.className = 'track-item';
        label.style.cursor = 'pointer';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = sel[si];
        cb.dataset.si = si;
        cb.addEventListener('change', () => { sel[si] = cb.checked; saveUIState(); });
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
    _sessionCache[_sessionId] = { label: 'New session', tasks: {}, active_task: null, ui_state: {}, version: 0, created_at: 0 };
    await refreshSessionList();
    return _sessionId;
}

async function runAlign() {
    if (_runningTaskId) { alert('A task is already running. Stop it first.'); return; }
    if (!state.v1.path || !state.v2.path) { alert('Load both videos first.'); return; }

    await ensureSession();
    const myView = ++_viewId;

    lockButtons('align');
    document.getElementById('align-progress').classList.add('progress-indeterminate');
    logSeparator('Align');
    log('[Align] Starting...');

    const t1 = parseInt(document.getElementById('v1-sync-track').value) || 0;
    const t2 = parseInt(document.getElementById('v2-sync-track').value) || 0;

    const vocalFilter = document.getElementById('vocal-filter-cb').checked;

    const result = await apiPost(`/api/session/${_sessionId}/align`, {
        v1_path: state.v1.path, v2_path: state.v2.path,
        v1_track: t1, v2_track: t2,
        vocal_filter: vocalFilter,
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
        document.getElementById('align-progress').classList.remove('progress-indeterminate');
        log(`[Align] Error: ${result.error}`);
        return;
    }

    refreshSessionList();

    startPoll('align', result.task_id,
        (task) => {
            if (_viewId !== myView) return;
            log(`[Align] ${task.progress || 'Processing...'}`);
        },
        (task) => {
            if (_viewId !== myView) return;
            unlockButtons();
            document.getElementById('align-progress').classList.remove('progress-indeterminate');
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
    }

    document.getElementById('atempo-input').value = at.toFixed(6);
    document.getElementById('offset-input').value = off.toFixed(3);

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
    if (r.visual_corrected) {
        const aoSign = r.audio_offset >= 0 ? '+' : '';
        const voSign = r.visual_offset >= 0 ? '+' : '';
        vizEl.textContent = `corrected: ${aoSign}${r.audio_offset.toFixed(3)}s \u2192 ${voSign}${r.visual_offset.toFixed(3)}s`;
        vizEl.style.color = 'var(--warn)';
    } else {
        vizEl.textContent = 'confirmed';
        vizEl.style.color = 'var(--green)';
    }

    state.segments = r.segments || null;
    renderSegmentOverrides();

    const detail = document.getElementById('results-detail');
    const v1c = r.v1_coverage || [0,0], v2c = r.v2_coverage || [0,0];
    const v1i = r.v1_interval || 0, v2i = r.v2_interval || 0;

    const aoSpd = r.audio_speed || at;
    const aoOff = r.audio_offset !== undefined ? r.audio_offset : off;
    const aoSign = aoOff >= 0 ? '+' : '';
    let txt = `Audio alignment:  offset=${aoSign}${aoOff.toFixed(3)}s  speed=${(1.0/aoSpd).toFixed(6)}\n`;
    if (r.visual_corrected) {
        const voSign = r.visual_offset >= 0 ? '+' : '';
        const scoreStr = r.audio_visual_score != null ? `(score ${r.audio_visual_score.toFixed(2)}\u2192${r.visual_score.toFixed(2)})` : '';
        txt += `Visual check:     corrected \u2192 offset=${voSign}${r.visual_offset.toFixed(3)}s  speed=${(1.0/r.visual_speed).toFixed(6)}  ${scoreStr}\n`;
    } else {
        txt += `Visual check:     confirmed\n`;
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

    if (isRemuxMode()) {
        const { sorted: sortedMeta, order: audioOrder } = reorderAudioTracks(metadata, state.defaultAudioIdx);
        const params = { v1_path: v1, out_path: outPath, v1_stream_indices: v1StreamIndices, v1_duration: state.v1.duration, v1_streams: state.v1.streams, v1_tracks: state.v1.tracks, metadata: sortedMeta, sub_metadata, default_audio: 0, audio_order: audioOrder, v1_has_attachments: getSelectedIndices(1, 'attachment').length > 0 };
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
    const params = { v1_path: v1, v2_path: v2, out_path: outPath, atempo, offset, v1_stream_indices: v1StreamIndices, v2_stream_indices: v2StreamIndices, v2_sub_metadata, v1_duration: state.v1.duration, v1_streams: state.v1.streams, v1_tracks: state.v1.tracks, v2_streams: state.v2.streams, v2_tracks: state.v2.tracks, metadata: sortedMeta, sub_metadata, default_audio: 0, audio_order: audioOrder, gain_match: gainMatch, v1_lufs: state.v1Lufs, v2_lufs: state.v2Lufs, v1_has_attachments: getSelectedIndices(1, 'attachment').length > 0, v2_has_attachments: getSelectedIndices(2, 'attachment').length > 0 };
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
    const progressBar = document.getElementById('merge-progress');
    let lastMuxPct = -1, lastEncPct = -1, lastSubPct = -1;
    startPoll(taskType, taskId,
        (task) => {
            if (_viewId !== myView) return;
            const progress = task.progress || '';
            const muxMatch = progress.match(/mux:(\d+)/);
            if (isRemux) {
                const subMatch = progress.match(/sub:(\d+)/);
                if (muxMatch) {
                    const pct = parseInt(muxMatch[1]);
                    progressBar.querySelector('.fill').style.width = `${Math.floor(pct / 2)}%`;
                    if (pct !== lastMuxPct) { lastMuxPct = pct; log(`[Remux] Muxing video + audio... ${pct}%`); }
                } else if (subMatch) {
                    const pct = parseInt(subMatch[1]);
                    progressBar.querySelector('.fill').style.width = `${50 + Math.floor(pct / 2)}%`;
                    if (pct !== lastSubPct) { lastSubPct = pct; log(`[Remux] Adding subtitles... ${pct}%`); }
                } else {
                    log(`[Remux] ${progress.replace(/^status:/, '')}`);
                }
            } else {
                const encMatch = progress.match(/enc:(\d+)/);
                const subMatch = progress.match(/sub:(\d+)/);
                if (encMatch) {
                    const pct = parseInt(encMatch[1]);
                    progressBar.querySelector('.fill').style.width = `${Math.floor(pct / 3)}%`;
                    if (pct !== lastEncPct) { lastEncPct = pct; log(`[Merge] Encoding audio... ${pct}%`); }
                } else if (muxMatch) {
                    const pct = parseInt(muxMatch[1]);
                    progressBar.querySelector('.fill').style.width = `${33 + Math.floor(pct / 3)}%`;
                    if (pct !== lastMuxPct) { lastMuxPct = pct; log(`[Merge] Muxing output... ${pct}%`); }
                } else if (subMatch) {
                    const pct = parseInt(subMatch[1]);
                    progressBar.querySelector('.fill').style.width = `${66 + Math.floor(pct / 3)}%`;
                    if (pct !== lastSubPct) { lastSubPct = pct; log(`[Merge] Adding subtitles... ${pct}%`); }
                } else {
                    log(`[Merge] ${progress.replace(/^status:/, '')}`);
                }
            }
        },
        (task) => {
            if (_viewId !== myView) return;
            unlockButtons();
            if (task.status === 'done') {
                progressBar.querySelector('.fill').style.width = '100%';
                log(`[${taskLabel}] Completed in ${task.result.elapsed} \u2192 ${task.result.output}`);
                state.mergeParams = null;
                alert('Output file created!\n' + task.result.output);
            } else {
                progressBar.querySelector('.fill').style.width = '0%';
                log(`[${taskLabel}] ${task.status}: ${task.error || ''}`);
            }
            refreshSessionList();
        },
        initialProgress
    );
}

async function runMerge() {
    if (_runningTaskId) { alert('A task is already running. Stop it first.'); return; }
    const remux = isRemuxMode();

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
        prepareMerge(at, off);
        if (getSelectedIndices(2, 'audio').length === 0) { alert('Select at least one V2 audio track.'); return; }
    }

    const currentOutPath = document.getElementById('out-path-input').value.trim() || getDefaultOutputPath();
    if (state.mergeParams) {
        state.mergeParams.out_path = currentOutPath;
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
    document.getElementById('merge-progress').querySelector('.fill').style.width = '0%';
    const taskLabel = remux ? 'Remux' : 'Merge';
    logSeparator(taskLabel);
    log(`[${taskLabel}] Starting...`);

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
        group: stream.codec_type === 'subtitle' ? 'Subtitles' : 'Audio',
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
    html += `<p style="color:var(--dim);font-size:12px;margin-bottom:12px;">Edit language and title for audio and subtitle tracks.</p>`;

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
    clearTimeout(_saveTimer);
    stopPoll();
    _restoring = true;
    const myView = ++_viewId;
    _sessionId = sid;
    sessionStorage.setItem('audiosync_session', sid);
    resetUI();
    resetState();
    renderSessionList(_sessionCache);

    // Restore log entries from cache or server ui_state
    const cached = _sessionCache[sid];
    const logEntries = (cached && cached.log_entries && cached.log_entries.length)
        ? cached.log_entries
        : ((sess.ui_state && sess.ui_state.log_entries) || []);
    if (logEntries.length) {
        if (cached) cached.log_entries = logEntries;
        const box = document.getElementById('log-box');
        box.value = logEntries.join('\n') + '\n';
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

    if (v1Path) {
        document.getElementById('v1-path-input').value = v1Path;
        await loadVideo(1);
        if (_viewId !== myView) { _restoring = false; return; }
    }
    if (v2Path) {
        document.getElementById('v2-path-input').value = v2Path;
        await loadVideo(2);
        if (_viewId !== myView) { _restoring = false; return; }
    }

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

    // Vocal filter & sync tracks
    if (ui.vocal_filter !== undefined)
        document.getElementById('vocal-filter-cb').checked = ui.vocal_filter;
    if (ui.gain_match !== undefined)
        document.getElementById('gain-match-cb').checked = ui.gain_match;
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
    if (ui.atempo !== undefined)
        document.getElementById('atempo-input').value = ui.atempo;
    if (ui.offset !== undefined)
        document.getElementById('offset-input').value = ui.offset;

    if (lastMergeResult) {
        document.getElementById('merge-progress').querySelector('.fill').style.width = '100%';
        log(`[Merge] Completed: ${lastMergeResult.elapsed} \u2192 ${lastMergeResult.output}`);
    }

    // Fetch fresh session data to check actual task status
    try {
        const freshRes = await fetch(`/api/session/${sid}`);
        if (freshRes.ok) {
            const freshData = await freshRes.json();
            const cachedLogs = _sessionCache[sid] ? _sessionCache[sid].log_entries : null;
            if (cachedLogs) freshData.log_entries = cachedLogs;
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
            document.getElementById('align-progress').classList.add('progress-indeterminate');
            startPoll('align', activeTask,
                (task) => {
                    if (_viewId !== myView) return;
                    log(`[Align] ${task.progress || 'Processing...'}`);
                },
                (task) => {
                    if (_viewId !== myView) return;
                    unlockButtons();
                    document.getElementById('align-progress').classList.remove('progress-indeterminate');
                    if (task.status === 'done') {
                        showAlignResults(task.result);
                        const sr = task.result.speed_ratio, off = task.result.offset;
                        log(`[Align] Done: ${task.result.inlier_count} inliers, atempo=${sr != null ? sr.toFixed(6) : '?'}, offset=${off != null ? off.toFixed(3) : '?'}s`);
                    } else {
                        log(`[Align] ${task.status}: ${task.error || ''}`);
                    }
                    refreshSessionList();
                },
                currentProgress
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
        // Preserve log_entries from existing cache
        for (const [sid, sess] of Object.entries(sessions)) {
            if (_sessionCache[sid] && _sessionCache[sid].log_entries) {
                sess.log_entries = _sessionCache[sid].log_entries;
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
            if (cached && cached.log_entries) sess.log_entries = cached.log_entries;
            _sessionCache[_sessionId] = sess;
            renderSessionList(_sessionCache);
        }
    } catch (e) {}
}

async function init() {
    log('Ready.');
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
