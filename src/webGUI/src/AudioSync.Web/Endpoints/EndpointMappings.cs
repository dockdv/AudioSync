using System.Reflection;
using System.Text.Json;
using AudioSync.Core.Merging;
using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;
using AudioSync.Core.Tasks;
using AudioSync.Core.Tooling;
using AudioSync.Web.Contracts;

namespace AudioSync.Web.Endpoints;

/// <summary>
/// Mirror of app.py routes — registers all /api/* endpoints on the WebApplication.
/// Mode B group: file browse / probe / sessions / align / merge / remux / tasks /
/// test-interleave / info.
/// </summary>
public static class EndpointMappings
{
    public static void MapAll(WebApplication app)
    {
        MapBrowse(app);
        MapProbe(app);
        MapSessions(app);
        MapTasks(app);
        MapInfo(app);
    }

    // ===== /api/browse + /api/file-exists =====
    private static void MapBrowse(WebApplication app)
    {
        app.MapPost("/api/browse", (PathRequest req) =>
        {
            string path = req.Path ?? "";
            if (string.IsNullOrEmpty(path))
            {
                if (OperatingSystem.IsWindows())
                {
                    var drives = new List<object>();
                    for (char c = 'A'; c <= 'Z'; c++)
                    {
                        var drive = $"{c}:\\";
                        if (Directory.Exists(drive))
                            drives.Add(new { name = $"{c}:", path = drive, is_dir = true });
                    }
                    return Results.Json(new { entries = drives, current = "" });
                }
                path = "/";
            }
            path = Path.GetFullPath(path);
            if (!Directory.Exists(path)) path = Path.GetDirectoryName(path) ?? path;
            if (!Directory.Exists(path))
                return Results.BadRequest(new { error = $"Not a directory: {path}" });

            var entries = new List<object>();
            var parent = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(parent) && parent != path)
                entries.Add(new { name = "..", path = parent, is_dir = true });
            try
            {
                foreach (var name in Directory.EnumerateFileSystemEntries(path)
                    .Select(Path.GetFileName)
                    .Where(n => n != null)
                    .OrderBy(n => n, StringComparer.OrdinalIgnoreCase))
                {
                    var full = Path.Combine(path, name!);
                    entries.Add(new { name, path = full, is_dir = Directory.Exists(full) });
                }
            }
            catch (UnauthorizedAccessException)
            {
                return Results.Json(new { error = $"Permission denied: {path}" }, statusCode: 403);
            }
            return Results.Json(new { entries, current = path });
        });

        app.MapPost("/api/file-exists", (FileExistsRequest req) =>
            Results.Json(new { exists = !string.IsNullOrEmpty(req.Path) && File.Exists(req.Path) }));
    }

    // ===== /api/probe =====
    private static void MapProbe(WebApplication app)
    {
        app.MapPost("/api/probe", async (ProbeRequest req, IMediaProber prober, CancellationToken ct) =>
        {
            if (string.IsNullOrEmpty(req.Filepath) || !File.Exists(req.Filepath))
                return Results.BadRequest(new { error = $"File not found: {req.Filepath}" });

            var info = await prober.ProbeFullAsync(req.Filepath, ct);
            var (changeNeeded, ext) = Languages.NeedsContainerChange(req.Filepath);
            return Results.Json(new
            {
                tracks = info.Tracks,
                streams = info.Streams,
                method = info.Method,
                error = info.Error,
                warning = info.Warning,
                duration = info.Duration,
                duration_fmt = SyncEngine.FormatTimestamp(info.Duration),
                container_change = changeNeeded,
                container_ext = ext,
            });
        });
    }

    // ===== /api/sessions, /api/session/* =====
    private static void MapSessions(WebApplication app)
    {
        app.MapPost("/api/sessions", (SessionStore store) =>
            Results.Json(new { session_id = store.NewSession() }));

        app.MapGet("/api/sessions", (SessionStore store) =>
        {
            var snap = store.Snapshot();
            var result = new Dictionary<string, object>();
            foreach (var (sid, sess) in snap)
                result[sid] = SerializeSession(sess);
            return Results.Json(result);
        });

        app.MapGet("/api/session/{sid}", (string sid, SessionStore store) =>
        {
            var sess = store.Get(sid);
            return sess is null
                ? Results.NotFound(new { error = "Session not found" })
                : Results.Json(SerializeSession(sess));
        });

        app.MapMethods("/api/session/{sid}/state", new[] { "PATCH" },
            async (string sid, HttpRequest http, SessionStore store) =>
            {
                using var doc = await JsonDocument.ParseAsync(http.Body);
                var sess = store.Get(sid);
                if (sess is null) return Results.NotFound(new { error = "Session not found" });

                if (doc.RootElement.ValueKind == JsonValueKind.Object)
                {
                    foreach (var prop in doc.RootElement.EnumerateObject())
                    {
                        if (prop.NameEquals("log_entries")) continue;
                        sess.UiState[prop.Name] = prop.Value.Clone();
                    }
                }
                sess.Version++;
                string? v1 = sess.UiState.TryGetValue("v1_path", out var v1e) ? v1e.GetString() : null;
                string? v2 = sess.UiState.TryGetValue("v2_path", out var v2e) ? v2e.GetString() : null;
                if (!string.IsNullOrEmpty(v1) && !string.IsNullOrEmpty(v2))
                    sess.Label = $"{Path.GetFileName(v1)} \u2194 {Path.GetFileName(v2)}";
                else if (!string.IsNullOrEmpty(v1))
                    sess.Label = Path.GetFileName(v1);
                return Results.Json(new { ok = true, version = sess.Version, label = sess.Label });
            });

        app.MapGet("/api/session/{sid}/version", (string sid, SessionStore store) =>
        {
            var sess = store.Get(sid);
            return sess is null
                ? Results.NotFound(new { error = "Session not found" })
                : Results.Json(new { version = sess.Version });
        });

        app.MapGet("/api/session/{sid}/logs", (string sid, long? after, SessionStore store) =>
        {
            var sess = store.Get(sid);
            if (sess is null) return Results.NotFound(new { error = "Session not found" });
            long a = after ?? 0;
            var entries = sess.Log.Where(e => e.Idx > a).ToList();
            return Results.Json(new { entries });
        });

        // Server-Sent Events stream of EVERY session's log entries.
        // The client opens one EventSource and receives a continuous push for
        // all sessions (active or background), so logs accumulate even when the
        // user has switched to a different session.
        app.MapGet("/api/events/stream", async (HttpContext http, SessionStore store,
            Microsoft.Extensions.Hosting.IHostApplicationLifetime lifetime) =>
        {
            http.Response.Headers["Content-Type"] = "text/event-stream";
            http.Response.Headers["Cache-Control"] = "no-cache";
            http.Response.Headers["X-Accel-Buffering"] = "no";

            // Cancel the SSE loop on EITHER client disconnect OR host shutdown.
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(
                http.RequestAborted, lifetime.ApplicationStopping);
            var ct = cts.Token;

            // Single multiplexed channel for log entries AND task state changes.
            // Discriminated by the first field on the wire ("kind": "log"|"task").
            var queue = System.Threading.Channels.Channel.CreateUnbounded<object>();
            void LogHandler(string sid, LogEntry entry) =>
                queue.Writer.TryWrite(new
                {
                    kind = "log",
                    sid,
                    idx = entry.Idx,
                    msg = entry.Msg,
                    source = entry.Source,
                    ts = entry.Ts,
                });
            void TaskHandler(string sid, BackgroundJob job) =>
                queue.Writer.TryWrite(new
                {
                    kind = "task",
                    sid,
                    tid = job.Id,
                    type = job.Type,
                    status = job.Status switch
                    {
                        JobStatus.Running => "running",
                        JobStatus.Done => "done",
                        JobStatus.Cancelled => "cancelled",
                        JobStatus.Error => "error",
                        _ => "unknown",
                    },
                    progress = job.Progress,
                    percent = job.Percent,
                    result = job.Result,
                    error = job.Error,
                });
            store.LogAppended += LogHandler;
            store.TaskUpdated += TaskHandler;

            try
            {
                // Replay current state for every existing session so a
                // (re)connecting client gets the full backlog.
                foreach (var (sid, sess) in store.Snapshot())
                {
                    foreach (var e in sess.Log) LogHandler(sid, e);
                    foreach (var t in sess.Tasks.Values) TaskHandler(sid, t);
                }

                await foreach (var ev in queue.Reader.ReadAllAsync(ct))
                {
                    var json = System.Text.Json.JsonSerializer.Serialize(ev);
                    await http.Response.WriteAsync($"data: {json}\n\n", ct);
                    await http.Response.Body.FlushAsync(ct);
                }
            }
            catch (OperationCanceledException) { }
            finally
            {
                store.LogAppended -= LogHandler;
                store.TaskUpdated -= TaskHandler;
                queue.Writer.TryComplete();
            }
        });

        app.MapPost("/api/session/{sid}/logs", (string sid, LogPostRequest req, SessionStore store) =>
        {
            var sess = store.Get(sid);
            if (sess is null) return Results.NotFound(new { error = "Session not found" });
            foreach (var msg in (req.Messages ?? new()).Take(50))
                store.AppendLog(sid, msg ?? "", "client");
            return Results.Json(new { ok = true, log_idx = sess.LogIdx });
        });

        app.MapDelete("/api/session/{sid}", (string sid, SessionStore store) =>
            store.DeleteSession(sid)
                ? Results.Json(new { ok = true })
                : Results.NotFound(new { error = "Session not found" }));

        // align / merge / remux are large — mapped in their own helpers
        SyncEndpoints.Map(app);
        MergeEndpoints.Map(app);
    }

    // ===== /api/session/{sid}/task/{tid} (status + cancel) + test-interleave =====
    private static void MapTasks(WebApplication app)
    {
        app.MapGet("/api/session/{sid}/task/{tid}", (string sid, string tid, SessionStore store) =>
        {
            var t = store.GetTask(sid, tid);
            return t is null
                ? Results.NotFound(new { error = "Task not found" })
                : Results.Json(SerializeTask(t));
        });

        app.MapPost("/api/session/{sid}/task/{tid}/cancel", (string sid, string tid, SessionStore store) =>
            store.CancelTask(sid, tid)
                ? Results.Json(new { ok = true })
                : Results.NotFound(new { error = "Task not found" }));

        app.MapPost("/api/session/{sid}/test-interleave",
            async (string sid, TestInterleaveRequest req, SessionStore store, FfLib ff, IMediaProber prober) =>
            {
                if (string.IsNullOrEmpty(req.Filepath) || !File.Exists(req.Filepath))
                    return Results.BadRequest(new { error = $"File not found: {req.Filepath}" });

                var (job, err) = store.StartTask(sid, "test-interleave",
                    new Dictionary<string, object?> { ["v1_path"] = req.Filepath });
                if (err is not null) return Results.Conflict(new { error = err });

                _ = Task.Run(async () =>
                {
                    try
                    {
                        var info = await prober.ProbeAsync(req.Filepath!, job!.Cancel.Token);
                        var streamMap = info.Streams.ToDictionary(s => s.StreamIndex, s => s);
                        var packets = await ff.ProbePacketsAsync(req.Filepath!,
                            (kind, msg) => store.UpdateTask(sid, job.Id, progress: msg),
                            job.Cancel.Token);

                        long totalPackets = packets.Values.Sum(v => (long)v.Count);
                        double duration = info.Duration;
                        var lines = new List<string>
                        {
                            $"Interleave analysis: {totalPackets:N0} packets across {packets.Count} streams"
                        };
                        var issues = new List<string>();

                        foreach (var idx in packets.Keys.OrderBy(k => k))
                        {
                            var dts = packets[idx];
                            streamMap.TryGetValue(idx, out var s);
                            string codecType = s?.CodecType ?? "unknown";
                            string codec = s?.Codec ?? "?";
                            string label = $"#{idx} {codecType} ({codec})";
                            if (dts.Count < 2)
                            {
                                lines.Add($"  {label}: {dts.Count} packet(s) - skipped");
                                continue;
                            }
                            var gaps = new double[dts.Count - 1];
                            for (int i = 0; i < gaps.Length; i++) gaps[i] = dts[i + 1] - dts[i];
                            Array.Sort(gaps);
                            double medianGap = gaps[gaps.Length / 2];
                            double maxGap = gaps[^1];
                            double firstDts = dts[0];
                            double lastDts = dts[^1];
                            double span = lastDts - firstDts;
                            lines.Add($"  {label}: {dts.Count:N0} pkts, span {firstDts:F1}s-{lastDts:F1}s, median gap {medianGap:F3}s, max gap {maxGap:F3}s");
                            if (firstDts > 5.0 && (codecType == "audio" || codecType == "subtitle"))
                                issues.Add($"ISSUE: {label} first packet at {firstDts:F1}s (late start, may stall muxer)");
                            if (maxGap > 5.0 && medianGap < 1.0 && (codecType == "audio" || codecType == "subtitle"))
                                issues.Add($"ISSUE: {label} has {maxGap:F1}s gap (vs {medianGap:F3}s median) - packets may be clustered");
                            if (codecType == "subtitle" && medianGap > 10.0)
                                issues.Add($"WARNING: {label} very sparse (median gap {medianGap:F1}s) - may affect MKV interleaving");
                            if (duration > 0 && span < duration * 0.5 && (codecType == "audio" || codecType == "subtitle"))
                                issues.Add($"WARNING: {label} covers only {span:F1}s of {duration:F1}s total");
                        }
                        if (issues.Count > 0)
                        {
                            lines.Add("");
                            lines.Add($"Found {issues.Count} issue(s):");
                            foreach (var i in issues) lines.Add($"  {i}");
                        }
                        else { lines.Add(""); lines.Add("No interleaving issues detected."); }
                        store.UpdateTask(sid, job.Id, status: JobStatus.Done,
                            result: new { lines, issue_count = issues.Count });
                    }
                    catch (OperationCanceledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (CancelledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (Exception ex) { store.UpdateTask(sid, job!.Id, status: JobStatus.Error, error: ex.Message); }
                    finally { store.EnsureTaskFinished(sid, job!.Id); }
                });
                return Results.Json(new { task_id = job!.Id });
            });
    }

    private static readonly string AppVersion = ResolveAppVersion();

    private static string ResolveAppVersion()
    {
        var info = typeof(EndpointMappings).Assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        if (!string.IsNullOrEmpty(info))
        {
            var plus = info.IndexOf('+');
            if (plus >= 0) info = info[..plus];
        }
        // SDK default (1.0.0) means no <Version> was set — treat as dev build.
        return (string.IsNullOrEmpty(info) || info == "1.0.0") ? "" : info;
    }

    // ===== / + /api/info + /api/languages =====
    private static void MapInfo(WebApplication app)
    {
        app.MapGet("/api/info", (IToolLocator locator) =>
        {
            var versions = locator.VersionInfo();
            var title = string.IsNullOrEmpty(AppVersion)
                ? "Audio Sync & Merge"
                : $"Audio Sync & Merge v{AppVersion}";
            return Results.Json(new
            {
                app_title = title,
                app_version = AppVersion,
                ffmpeg_path = locator.Ffmpeg ?? "",
                ffprobe_path = locator.Ffprobe ?? "",
                mkvmerge_path = locator.Mkvmerge ?? "",
                ffmpeg_version = versions.GetValueOrDefault("ffmpeg", ""),
                ffprobe_version = versions.GetValueOrDefault("ffprobe", ""),
                mkvmerge_version = versions.GetValueOrDefault("mkvmerge", ""),
                hwaccel = locator.Hwaccel,
            });
        });

        app.MapGet("/api/languages", () =>
        {
            return Results.Json(new
            {
                lang_names = Languages.Names,
                all_languages = Languages.All.Select(t => new[] { t.Code, t.Name }),
            });
        });
    }

    // ===== serializers =====
    public static object SerializeSession(SessionEntry s) => new
    {
        label = s.Label,
        active_task = s.ActiveTask,
        tasks = s.Tasks.ToDictionary(kv => kv.Key, kv => SerializeTask(kv.Value)),
        created_at = s.CreatedWall.ToUnixTimeSeconds(),
        ui_state = s.UiState,
        version = s.Version,
        log_idx = s.LogIdx,
    };

    public static object SerializeTask(BackgroundJob t) => new
    {
        type = t.Type,
        status = t.Status switch
        {
            JobStatus.Running => "running",
            JobStatus.Done => "done",
            JobStatus.Cancelled => "cancelled",
            JobStatus.Error => "error",
            _ => "unknown",
        },
        progress = t.Progress,
        percent = t.Percent,
        result = t.Result,
        error = t.Error,
        params_ = t.Params,
    };
}
