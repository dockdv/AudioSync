using System.Diagnostics;
using AudioSync.Core.Merging;
using AudioSync.Core.Sessions;
using AudioSync.Core.Tasks;
using AudioSync.Core.Tooling;
using AudioSync.Web.Contracts;

namespace AudioSync.Web.Endpoints;

public static class MergeEndpoints
{
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
        app.MapPost("/api/session/{sid}/merge", (string sid, HttpContext http, SessionStore store, IMerger merger)
            => RunMergeOrRemux(sid, http, store, merger, isRemux: false));

        app.MapPost("/api/session/{sid}/remux", (string sid, HttpContext http, SessionStore store, IMerger merger)
            => RunMergeOrRemux(sid, http, store, merger, isRemux: true));
    }

    private static async Task<IResult> RunMergeOrRemux(string sid, HttpContext http, SessionStore store, IMerger merger, bool isRemux)
    {
        MergeRequest req = new();
        try
        {
            if (http.Request.ContentLength is null or > 0)
            {
                var parsed = await http.Request.ReadFromJsonAsync<MergeRequest>(
                    new System.Text.Json.JsonSerializerOptions
                    {
                        PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
                        PropertyNameCaseInsensitive = true,
                        NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals,
                    });
                if (parsed is not null) req = parsed;
            }
        }
        catch (System.Text.Json.JsonException) { }
        catch { }

        var sess = store.Get(sid);
        if (sess is null) return Results.NotFound(new { error = "Session not found" });

        var build = MergeContextBuilder.Build(sess, isRemux, req.DurationLimit, req.OutPath);
        if (build.Error is not null) return Results.BadRequest(new { error = build.Error });

        var taskKind = isRemux ? "remux" : "merge";
        var taskInfo = new Dictionary<string, object?> { ["v1_path"] = build.V1Path, ["out_path"] = build.OutPath };
        if (!isRemux) taskInfo["v2_path"] = build.V2Path;
        var (job, err) = store.StartTask(sid, taskKind, taskInfo);
        if (err is not null) return Results.Conflict(new { error = err });

        _ = Task.Run(async () =>
        {
            var sw = Stopwatch.StartNew();
            var tracker = new PhaseTracker();
            void Cb(string kind, string msg)
            {
                var pct = tracker.Apply(kind, msg);
                store.UpdateTask(sid, job!.Id, progress: $"{kind}:{msg}", percent: pct);
                if (kind != "progress")
                    store.AppendLog(sid, $"{kind}:{msg}");
            }
            try
            {
                await merger.MergeAsync(build.Ctx!, Cb, job!.Cancel.Token);
                sw.Stop();
                int mins = (int)(sw.Elapsed.TotalSeconds / 60);
                int secs = (int)sw.Elapsed.TotalSeconds % 60;
                store.UpdateTask(sid, job.Id, status: JobStatus.Done,
                    result: new { elapsed = $"{mins}m {secs}s", output = build.OutPath });
            }
            catch (OperationCanceledException) { store.AppendLog(sid, "Task cancelled."); store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
            catch (CancelledException) { store.AppendLog(sid, "Task cancelled."); store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
            catch (Exception ex) { store.UpdateTask(sid, job!.Id, status: JobStatus.Error, error: ex.Message); }
            finally { store.EnsureTaskFinished(sid, job!.Id); }
        });
        return Results.Json(new { task_id = job!.Id });
    }
}
