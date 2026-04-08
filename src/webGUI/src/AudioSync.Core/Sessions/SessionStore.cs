using System.Diagnostics;
using AudioSync.Core.Tasks;

namespace AudioSync.Core.Sessions;

public sealed class SessionStoreOptions
{
    public TimeSpan IdleTtl { get; set; } = TimeSpan.FromHours(1);
    public TimeSpan MaxTtl { get; set; } = TimeSpan.FromHours(2);
    public TimeSpan PurgeInterval { get; set; } = TimeSpan.FromMinutes(5);
}

/// <summary>
/// Mirror of app.py _sessions + lock + lifecycle helpers (_new_session,
/// _start_task, _update_task, _purge_stale_sessions, _serialize_session,
/// _append_log, _get_task). All access is serialized through a single lock
/// matching the Python design.
/// </summary>
public sealed class SessionStore
{
    private readonly Dictionary<string, SessionEntry> _sessions = new();
    private readonly object _lock = new();
    private readonly SessionStoreOptions _opts;
    private long _lastPurgeTicks;

    /// <summary>
    /// Raised after a log entry has been appended to a session. Handlers run
    /// outside the store lock so subscribers may safely call back into the store.
    /// </summary>
    public event Action<string, LogEntry>? LogAppended;

    /// <summary>
    /// Raised after a task is started, updated, or finished. Subscribers should
    /// not mutate the BackgroundJob — it is shared. Raised outside the store lock.
    /// </summary>
    public event Action<string, BackgroundJob>? TaskUpdated;

    public SessionStore(SessionStoreOptions? options = null)
    {
        _opts = options ?? new SessionStoreOptions();
    }

    private static long Now() => Stopwatch.GetTimestamp();
    private static double TicksToSeconds(long ticks) => (double)ticks / Stopwatch.Frequency;

    public SessionEntry? Get(string sid)
    {
        lock (_lock) return _sessions.TryGetValue(sid, out var s) ? s : null;
    }

    public Dictionary<string, SessionEntry> Snapshot()
    {
        lock (_lock)
        {
            PurgeStale();
            return new Dictionary<string, SessionEntry>(_sessions);
        }
    }

    /// <summary>Mirror of _new_session — allocates 16-char UUID, registers session.</summary>
    public string NewSession()
    {
        var sid = Guid.NewGuid().ToString("N").Substring(0, 16);
        var now = Now();
        lock (_lock)
        {
            PurgeStale();
            _sessions[sid] = new SessionEntry
            {
                Id = sid,
                CreatedWall = DateTimeOffset.UtcNow,
                CreatedAtTicks = now,
                UpdatedAtTicks = now,
                Label = "New session",
            };
        }
        return sid;
    }

    /// <summary>
    /// Mirror of _start_task. Returns (job, error) — job is null if rejected.
    /// </summary>
    public (BackgroundJob? Job, string? Error) StartTask(
        string sid, string taskType, IReadOnlyDictionary<string, object?>? @params)
    {
        var tid = Guid.NewGuid().ToString("N").Substring(0, 8);
        @params ??= new Dictionary<string, object?>();

        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess))
                return (null, "Session not found");
            if (sess.ActiveTask != null && sess.Tasks.TryGetValue(sess.ActiveTask, out var at)
                && at.Status == JobStatus.Running)
            {
                return (null, $"Session already has a running {at.Type} task");
            }
            var job = new BackgroundJob
            {
                Id = tid,
                Type = taskType,
                Status = JobStatus.Running,
                Params = @params,
            };
            sess.Tasks[tid] = job;
            sess.ActiveTask = tid;
            sess.UpdatedAtTicks = Now();
            sess.Version++;

            var v1 = TryString(@params, "v1_path");
            var v2 = TryString(@params, "v2_path");
            if (!string.IsNullOrEmpty(v1) && !string.IsNullOrEmpty(v2))
                sess.Label = $"{Path.GetFileName(v1)} \u2194 {Path.GetFileName(v2)}";
            else if (!string.IsNullOrEmpty(v1))
                sess.Label = $"{Path.GetFileName(v1)} (remux)";

            // raise outside the lock
            var jobToFire = job;
            var sidToFire = sid;
            Task.Run(() => { try { TaskUpdated?.Invoke(sidToFire, jobToFire); } catch { } });

            return (job, null);
        }
    }

    /// <summary>Mirror of _update_task — applies status/progress/result/error fields.</summary>
    public void UpdateTask(string sid, string tid,
        JobStatus? status = null, string? progress = null, object? result = null, string? error = null,
        int? percent = null)
    {
        BackgroundJob? toFire = null;
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess)) return;
            if (!sess.Tasks.TryGetValue(tid, out var t)) return;
            if (status.HasValue) t.Status = status.Value;
            if (progress != null) t.Progress = progress;
            if (result != null) t.Result = result;
            if (error != null) t.Error = error;
            if (percent.HasValue) t.Percent = percent.Value;
            sess.UpdatedAtTicks = Now();
            if (status is JobStatus.Done or JobStatus.Cancelled or JobStatus.Error)
            {
                t.FinishedAt = DateTimeOffset.UtcNow;
                if (sess.ActiveTask == tid) sess.ActiveTask = null;
                sess.Version++;
                if (status == JobStatus.Done && t.Percent < 100) t.Percent = 100;
            }
            toFire = t;
        }
        if (toFire is not null)
        {
            try { TaskUpdated?.Invoke(sid, toFire); } catch { }
        }
    }

    /// <summary>
    /// Mirror of _ensure_task_finished — called after the task thread exits;
    /// marks the task as error if it never reached a terminal state.
    /// </summary>
    public void EnsureTaskFinished(string sid, string tid)
    {
        BackgroundJob? toFire = null;
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess)) return;
            if (!sess.Tasks.TryGetValue(tid, out var t)) return;
            if (t.Status != JobStatus.Running) return;
            t.Status = JobStatus.Error;
            t.Error = "Task died unexpectedly";
            t.FinishedAt = DateTimeOffset.UtcNow;
            if (sess.ActiveTask == tid) sess.ActiveTask = null;
            sess.Version++;
            toFire = t;
        }
        if (toFire is not null)
        {
            try { TaskUpdated?.Invoke(sid, toFire); } catch { }
        }
    }

    /// <summary>Mirror of _get_task — touches updated_at, returns task copy.</summary>
    public BackgroundJob? GetTask(string sid, string tid)
    {
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess)) return null;
            if (!sess.Tasks.TryGetValue(tid, out var t)) return null;
            sess.UpdatedAtTicks = Now();
            return t;
        }
    }

    public bool DeleteSession(string sid)
    {
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess)) return false;
            foreach (var t in sess.Tasks.Values)
                if (t.Status == JobStatus.Running)
                    try { t.Cancel.Cancel(); } catch { }
            _sessions.Remove(sid);
            return true;
        }
    }

    public bool CancelTask(string sid, string tid)
    {
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess)) return false;
            if (!sess.Tasks.TryGetValue(tid, out var t)) return false;
            if (t.Status != JobStatus.Running) return false;
            try { t.Cancel.Cancel(); } catch { }
            return true;
        }
    }

    public void AppendLog(string sid, string msg, string source = "server")
    {
        LogEntry? entry = null;
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sid, out var sess)) return;
            sess.LogIdx++;
            entry = new LogEntry
            {
                Idx = sess.LogIdx,
                Msg = msg,
                Source = source,
                Ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            };
            sess.Log.Add(entry);
            if (sess.Log.Count > 1000)
                sess.Log.RemoveRange(0, sess.Log.Count - 1000);
        }
        // Raise outside the lock so subscribers may call back safely.
        if (entry is not null)
        {
            try { LogAppended?.Invoke(sid, entry); } catch { }
        }
    }

    /// <summary>
    /// Mirror of _purge_stale_sessions. MUST be called while holding _lock —
    /// these methods all do that.
    /// </summary>
    public void PurgeStale()
    {
        var now = Now();
        if (TicksToSeconds(now - _lastPurgeTicks) < _opts.PurgeInterval.TotalSeconds && _lastPurgeTicks != 0)
            return;
        _lastPurgeTicks = now;

        var stale = new List<string>();
        foreach (var (sid, s) in _sessions)
        {
            var ageSec = TicksToSeconds(now - s.UpdatedAtTicks);
            if (ageSec <= _opts.IdleTtl.TotalSeconds) continue;

            // Fix stale active_task pointer before deciding
            if (s.ActiveTask != null)
            {
                if (!s.Tasks.TryGetValue(s.ActiveTask, out var at) || at.Status != JobStatus.Running)
                    s.ActiveTask = null;
            }
            if (ageSec > _opts.MaxTtl.TotalSeconds) stale.Add(sid);
            else if (s.ActiveTask is null) stale.Add(sid);
        }
        foreach (var sid in stale)
        {
            var sess = _sessions[sid];
            foreach (var t in sess.Tasks.Values)
                if (t.Status == JobStatus.Running)
                    try { t.Cancel.Cancel(); } catch { }
            _sessions.Remove(sid);
        }
    }

    private static string? TryString(IReadOnlyDictionary<string, object?> d, string key)
        => d.TryGetValue(key, out var v) ? v?.ToString() : null;
}
