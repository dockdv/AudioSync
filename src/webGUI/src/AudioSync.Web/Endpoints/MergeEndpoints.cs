using System.Diagnostics;
using AudioSync.Core.Merging;
using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;
using AudioSync.Core.Tasks;
using AudioSync.Core.Tooling;
using AudioSync.Web.Contracts;

namespace AudioSync.Web.Endpoints;

public static class MergeEndpoints
{
    /// <summary>
    /// Tracks merge phase progression and computes a global 0..99 percent.
    /// Phases are announced once via "phases:<csv>"; per-phase events are
    /// "<phase>:<n>" where n is 0..100. Each phase gets an even slice of 0..99.
    /// </summary>
    private sealed class PhaseTracker
    {
        private List<string> _phases = new() { "mux" };
        public int? Apply(string kind, string msg)
        {
            if (kind != "progress") return null;
            if (msg.StartsWith("phases:", StringComparison.Ordinal))
            {
                _phases = msg.Substring(7).Split(',', StringSplitOptions.RemoveEmptyEntries).ToList();
                if (_phases.Count == 0) _phases.Add("mux");
                return 0;
            }
            int colon = msg.IndexOf(':');
            if (colon <= 0) return null;
            string name = msg.Substring(0, colon);
            if (!int.TryParse(msg.AsSpan(colon + 1), out int n)) return null;
            int idx = _phases.IndexOf(name);
            if (idx < 0) return null;
            double bandStart = idx * 99.0 / _phases.Count;
            double bandEnd = (idx + 1) * 99.0 / _phases.Count;
            double bar = bandStart + (n / 100.0) * (bandEnd - bandStart);
            return (int)Math.Round(bar);
        }
    }

    public static void Map(WebApplication app)
    {
        app.MapPost("/api/session/{sid}/merge",
            async (string sid, HttpContext http, SessionStore store, IMerger merger) =>
            {
                MergeRequest req;
                try
                {
                    var parsed = await http.Request.ReadFromJsonAsync<MergeRequest>(
                        new System.Text.Json.JsonSerializerOptions
                        {
                            PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
                            PropertyNameCaseInsensitive = true,
                            NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals,
                        });
                    if (parsed is null) return Results.BadRequest(new { error = "Empty body" });
                    req = parsed;
                }
                catch (System.Text.Json.JsonException jex)
                {
                    return Results.BadRequest(new { error = $"JSON: {jex.Message} (path={jex.Path}, line={jex.LineNumber}, pos={jex.BytePositionInLine})" });
                }
                catch (Exception ex)
                {
                    return Results.BadRequest(new { error = $"Bind: {ex.GetType().Name}: {ex.Message}" });
                }
                if (string.IsNullOrEmpty(req.V1Path) || !File.Exists(req.V1Path))
                    return Results.BadRequest(new { error = $"V1 not found: {req.V1Path}" });
                if (string.IsNullOrEmpty(req.V2Path) || !File.Exists(req.V2Path))
                    return Results.BadRequest(new { error = $"V2 not found: {req.V2Path}" });
                if (string.IsNullOrEmpty(req.OutPath))
                    return Results.BadRequest(new { error = "Output path is required" });

                var (job, err) = store.StartTask(sid, "merge", new Dictionary<string, object?>
                {
                    ["v1_path"] = req.V1Path, ["v2_path"] = req.V2Path, ["out_path"] = req.OutPath,
                });
                if (err is not null) return Results.Conflict(new { error = err });

                _ = Task.Run(async () =>
                {
                    var sw = Stopwatch.StartNew();
                    var tracker = new PhaseTracker();
                    void Cb(string kind, string msg)
                    {
                        var pct = tracker.Apply(kind, msg);
                        store.UpdateTask(sid, job!.Id, progress: $"{kind}:{msg}", percent: pct);
                        // Per-percent progress ticks already drive the progress bar; keep them out of the log feed.
                        if (kind != "progress")
                            store.AppendLog(sid, $"{kind}:{msg}");
                    }
                    try
                    {
                        var sess = store.Get(sid);
                        if (sess is null) return;
                        var ctx = sess.Ctx;
                        ApplyMergeRequest(ctx, req);
                        await merger.MergeAsync(ctx, Cb, job!.Cancel.Token);
                        sw.Stop();
                        int mins = (int)(sw.Elapsed.TotalSeconds / 60);
                        int secs = (int)sw.Elapsed.TotalSeconds % 60;
                        store.UpdateTask(sid, job.Id, status: JobStatus.Done,
                            result: new { elapsed = $"{mins}m {secs}s", output = req.OutPath });
                    }
                    catch (OperationCanceledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (CancelledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (Exception ex) { store.UpdateTask(sid, job!.Id, status: JobStatus.Error, error: ex.Message); }
                    finally { store.EnsureTaskFinished(sid, job!.Id); }
                });
                return Results.Json(new { task_id = job!.Id });
            });

        app.MapPost("/api/session/{sid}/remux",
            async (string sid, HttpContext http, SessionStore store, IMerger merger) =>
            {
                RemuxRequest req;
                try
                {
                    var parsed = await http.Request.ReadFromJsonAsync<RemuxRequest>(
                        new System.Text.Json.JsonSerializerOptions
                        {
                            PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
                            PropertyNameCaseInsensitive = true,
                            NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals,
                        });
                    if (parsed is null) return Results.BadRequest(new { error = "Empty body" });
                    req = parsed;
                }
                catch (System.Text.Json.JsonException jex)
                {
                    return Results.BadRequest(new { error = $"JSON: {jex.Message} (path={jex.Path}, line={jex.LineNumber}, pos={jex.BytePositionInLine})" });
                }
                catch (Exception ex)
                {
                    return Results.BadRequest(new { error = $"Bind: {ex.GetType().Name}: {ex.Message}" });
                }
                if (string.IsNullOrEmpty(req.V1Path) || !File.Exists(req.V1Path))
                    return Results.BadRequest(new { error = $"V1 not found: {req.V1Path}" });
                if (string.IsNullOrEmpty(req.OutPath))
                    return Results.BadRequest(new { error = "Output path is required" });

                var (job, err) = store.StartTask(sid, "remux", new Dictionary<string, object?>
                {
                    ["v1_path"] = req.V1Path, ["out_path"] = req.OutPath,
                });
                if (err is not null) return Results.Conflict(new { error = err });

                _ = Task.Run(async () =>
                {
                    var sw = Stopwatch.StartNew();
                    var tracker = new PhaseTracker();
                    void Cb(string kind, string msg)
                    {
                        var pct = tracker.Apply(kind, msg);
                        store.UpdateTask(sid, job!.Id, progress: $"{kind}:{msg}", percent: pct);
                        // Per-percent progress ticks already drive the progress bar; keep them out of the log feed.
                        if (kind != "progress")
                            store.AppendLog(sid, $"{kind}:{msg}");
                    }
                    try
                    {
                        var sess = store.Get(sid);
                        if (sess is null) return;
                        var ctx = sess.Ctx;
                        ApplyRemuxRequest(ctx, req);
                        await merger.MergeAsync(ctx, Cb, job!.Cancel.Token);
                        sw.Stop();
                        int mins = (int)(sw.Elapsed.TotalSeconds / 60);
                        int secs = (int)sw.Elapsed.TotalSeconds % 60;
                        store.UpdateTask(sid, job.Id, status: JobStatus.Done,
                            result: new { elapsed = $"{mins}m {secs}s", output = req.OutPath });
                    }
                    catch (OperationCanceledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (CancelledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (Exception ex) { store.UpdateTask(sid, job!.Id, status: JobStatus.Error, error: ex.Message); }
                    finally { store.EnsureTaskFinished(sid, job!.Id); }
                });
                return Results.Json(new { task_id = job!.Id });
            });
    }

    private static void ApplyMergeRequest(SessionContext ctx, MergeRequest req)
    {
        ctx.V1Path = req.V1Path;
        ctx.V2Path = req.V2Path;
        ctx.OutPath = req.OutPath;
        if (req.Atempo.HasValue) ctx.Atempo = req.Atempo.Value;
        if (req.Offset.HasValue) ctx.Offset = req.Offset.Value;
        if (req.Segments is not null)
        {
            ctx.Segments = req.Segments.Select(s => new DetectedSegment
            {
                V1Start = s.V1Start,
                V1End = s.V1End >= 1e9 ? double.PositiveInfinity : s.V1End,
                Offset = s.Offset,
                NInliers = s.NInliers,
            }).ToList();
        }
        if (req.V1Lufs.HasValue) ctx.V1Lufs = req.V1Lufs.Value;
        if (req.V2Lufs.HasValue) ctx.V2Lufs = req.V2Lufs.Value;
        ctx.V1StreamIndices = req.V1StreamIndices;
        ctx.V2StreamIndices = req.V2StreamIndices;
        ctx.V1Duration = req.V1Duration;
        ctx.AudioMetadata = MapAudioMeta(req.Metadata);
        ctx.V1SubMetadata = MapTrackMeta(req.SubMetadata);
        ctx.V2SubMetadata = MapTrackMeta(req.V2SubMetadata);
        ctx.V1VidMetadata = MapTrackMeta(req.V1VidMetadata);
        ctx.DurationLimit = req.DurationLimit;
        ctx.DefaultAudioIndex = req.DefaultAudio;
        ctx.AudioOrder = req.AudioOrder is null ? null : new List<int>(req.AudioOrder);
        ctx.GainMatch = req.GainMatch;
        ctx.V1HasAttachments = req.V1HasAttachments;
        ctx.V2HasAttachments = req.V2HasAttachments;
        if ((req.V1Streams?.Count ?? 0) > 0)
            ctx.V1Info = new ProbeResult { Streams = req.V1Streams!, Audio = req.V1Tracks ?? new(), Duration = req.V1Duration };
        if ((req.V2Streams?.Count ?? 0) > 0 || (req.V2Tracks?.Count ?? 0) > 0)
            ctx.V2Info = new ProbeResult { Streams = req.V2Streams ?? new(), Audio = req.V2Tracks ?? new() };
    }

    private static void ApplyRemuxRequest(SessionContext ctx, RemuxRequest req)
    {
        ctx.V1Path = req.V1Path;
        ctx.V2Path = null;
        ctx.V2Info = null;
        ctx.OutPath = req.OutPath;
        ctx.Segments = null;
        ctx.V2StreamIndices = null;
        ctx.V2SubMetadata = null;
        ctx.GainMatch = false;
        ctx.V1HasAttachments = req.V1HasAttachments;
        ctx.V2HasAttachments = false;
        ctx.V1Lufs = null;
        ctx.V2Lufs = null;
        ctx.V1StreamIndices = req.V1StreamIndices;
        ctx.V1Duration = req.V1Duration;
        ctx.AudioMetadata = MapAudioMeta(req.Metadata);
        ctx.V1SubMetadata = MapTrackMeta(req.SubMetadata);
        ctx.V1VidMetadata = MapTrackMeta(req.V1VidMetadata);
        ctx.DurationLimit = req.DurationLimit;
        ctx.DefaultAudioIndex = req.DefaultAudio;
        ctx.AudioOrder = req.AudioOrder is null ? null : new List<int>(req.AudioOrder);
        if ((req.V1Streams?.Count ?? 0) > 0)
            ctx.V1Info = new ProbeResult { Streams = req.V1Streams!, Audio = req.V1Tracks ?? new(), Duration = req.V1Duration };
    }

    private static List<AudioMetadata>? MapAudioMeta(List<AudioMetaDto>? src)
        => src?.Select((m, i) => new AudioMetadata
        {
            Tid = i, Language = m.Language ?? "und", Title = m.Title ?? "",
        }).ToList();

    private static List<TrackMetadata>? MapTrackMeta(List<TrackMetaDto>? src)
        => src?.Select((m, i) => new TrackMetadata
        {
            Tid = i, Language = m.Language ?? "und", Title = m.Title ?? "",
        }).ToList();
}
