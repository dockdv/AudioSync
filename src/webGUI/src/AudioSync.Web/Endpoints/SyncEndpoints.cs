using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;
using AudioSync.Core.Tasks;
using AudioSync.Core.Tooling;
using AudioSync.Web.Contracts;

namespace AudioSync.Web.Endpoints;

public static class SyncEndpoints
{
    
    
    
    
    
    
    
    
    
    
    
    
    
    private sealed class AlignPhaseTracker
    {
        private int _v1Dec, _v2Dec, _v1Mel, _v2Mel, _v1En, _v2En;
        private int _melStart = 80;   
        private int _current;

        private int Hold(int p) { if (p > _current) _current = p; return _current; }

        public int? Apply(string kind, string msg)
        {
            if (kind == "status")
            {
                
                if (msg.StartsWith("Decoding V1: ", StringComparison.Ordinal)
                    || msg.StartsWith("Decoding V2: ", StringComparison.Ordinal))
                {
                    bool isV1 = msg[9] == 'V' && msg[10] == '1';
                    var pctStr = msg.Substring(13).TrimEnd('%').Trim();
                    if (int.TryParse(pctStr, out int p))
                    {
                        if (isV1) _v1Dec = p; else _v2Dec = p;
                        return Hold((_v1Dec + _v2Dec) * 80 / 200);
                    }
                }
                if (msg.StartsWith("Measuring loudness", StringComparison.Ordinal))
                {
                    _melStart = 82;
                    return Hold(82);
                }
                if (msg.StartsWith("Mel FP:", StringComparison.Ordinal))
                    return Hold(_melStart);
                if (msg.StartsWith("Falling back to energy", StringComparison.Ordinal))
                    return Hold(93);
                if (msg.StartsWith("Computing coarse offset", StringComparison.Ordinal)
                    || msg.StartsWith("Applying vocal", StringComparison.Ordinal)
                    || msg.StartsWith("Matching ", StringComparison.Ordinal))
                    return Hold(95);
                if (msg.StartsWith("RANSAC", StringComparison.Ordinal))
                    return Hold(96);
                if (msg.StartsWith("Checking for content breaks", StringComparison.Ordinal))
                    return Hold(97);
                if (msg.StartsWith("Validating segments visually", StringComparison.Ordinal))
                    return Hold(99);
                return null;
            }
            if (kind == "fp")
            {
                
                int colon = msg.IndexOf(':');
                int slash = msg.IndexOf('/');
                if (colon < 0 || slash <= colon + 1) return null;
                var cStr = msg.Substring(colon + 1, slash - colon - 1).Trim();
                var tStr = msg.Substring(slash + 1).Trim();
                if (!int.TryParse(cStr, out int c) || !int.TryParse(tStr, out int t) || t == 0) return null;
                int pct = Math.Clamp(c * 100 / t, 0, 100);
                bool isMel = msg.Contains(" Mel:", StringComparison.Ordinal);
                bool isV1 = msg.StartsWith("V1", StringComparison.Ordinal);
                if (isMel)
                {
                    if (isV1) _v1Mel = pct; else _v2Mel = pct;
                    int avg = (_v1Mel + _v2Mel) / 2;
                    return Hold(_melStart + avg * (93 - _melStart) / 100);
                }
                else
                {
                    if (isV1) _v1En = pct; else _v2En = pct;
                    int avg = (_v1En + _v2En) / 2;
                    return Hold(93 + avg * (95 - 93) / 100);
                }
            }
            return null;
        }
    }

    public static void Map(WebApplication app)
    {
        app.MapPost("/api/session/{sid}/align",
            (string sid, AlignRequest req, SessionStore store, ISyncEngine engine) =>
            {
                if (string.IsNullOrEmpty(req.V1Path) || !File.Exists(req.V1Path))
                    return Results.BadRequest(new { error = $"V1 not found: {req.V1Path}" });
                if (string.IsNullOrEmpty(req.V2Path) || !File.Exists(req.V2Path))
                    return Results.BadRequest(new { error = $"V2 not found: {req.V2Path}" });

                var (job, err) = store.StartTask(sid, "align", new Dictionary<string, object?>
                {
                    ["v1_path"] = req.V1Path, ["v2_path"] = req.V2Path,
                    ["v1_track"] = req.V1Track, ["v2_track"] = req.V2Track,
                    ["vocal_filter"] = req.VocalFilter,
                });
                if (err is not null) return Results.Conflict(new { error = err });

                _ = Task.Run(async () =>
                {
                    var tracker = new AlignPhaseTracker();
                    void Cb(string kind, string msg)
                    {
                        var pct = tracker.Apply(kind, msg);
                        store.UpdateTask(sid, job!.Id, progress: msg, percent: pct);
                        
                        bool isDecodeProgress = kind == "status"
                            && (msg.StartsWith("Decoding V1: ", StringComparison.Ordinal)
                                || msg.StartsWith("Decoding V2: ", StringComparison.Ordinal));
                        if (!isDecodeProgress && kind != "fp")
                            store.AppendLog(sid, msg);
                    }
                    try
                    {
                        var sess = store.Get(sid);
                        if (sess is null) return;
                        var ctx = sess.Ctx;
                        ctx.V1Path = req.V1Path;
                        ctx.V2Path = req.V2Path;
                        ctx.AlignTrack1 = req.V1Track;
                        ctx.AlignTrack2 = req.V2Track;
                        ctx.VocalFilter = req.VocalFilter;
                        ctx.MeasureLufs = req.MeasureLufs;
                        ctx.V1Info = new ProbeResult
                        {
                            Streams = req.V1Streams ?? new(),
                            Audio = req.V1Tracks ?? new(),
                            Duration = req.V1Duration,
                        };
                        ctx.V2Info = new ProbeResult
                        {
                            Streams = req.V2Streams ?? new(),
                            Audio = req.V2Tracks ?? new(),
                            Duration = req.V2Duration,
                        };

                        var r = await engine.AutoAlignAudioAsync(ctx, Cb, job!.Cancel.Token);
                        
                        var segs = (r.Segments ?? new()).Select(s => new
                        {
                            v1_start = s.V1Start,
                            v1_end = double.IsPositiveInfinity(s.V1End) ? 1e9 : s.V1End,
                            offset = s.Offset,
                            n_inliers = s.NInliers,
                        }).ToList();
                        store.UpdateTask(sid, job.Id, status: JobStatus.Done, result: new
                        {
                            speed_ratio = r.SpeedRatio,
                            offset = r.Offset,
                            linear_a = r.LinearA,
                            linear_b = r.LinearB,
                            inlier_count = r.InlierCount,
                            total_candidates = r.TotalCandidates,
                            inlier_pairs = r.InlierPairs.Select(p => new[] { p.T1, p.T2, p.Sim }),
                            v1_coverage = new[] { r.V1Coverage.Lo, r.V1Coverage.Hi },
                            v2_coverage = new[] { r.V2Coverage.Lo, r.V2Coverage.Hi },
                            v1_interval = r.V1Interval,
                            v2_interval = r.V2Interval,
                            mode = r.Mode,
                            sync_tracks = new[] { r.SyncTracks.T1, r.SyncTracks.T2 },
                            residual_mean = r.ResidualMean,
                            residual_max = r.ResidualMax,
                            residual_end = r.ResidualEnd,
                            coarse_offset = r.CoarseOffset,
                            segments = segs,
                            warnings = r.Warnings,
                            audio_offset = r.AudioOffset,
                            audio_speed = r.AudioSpeed,
                            v1_lufs = r.V1Lufs,
                            v2_lufs = r.V2Lufs,
                            v2_start_delay = r.V2StartDelay,
                            v1_fps = r.V1Fps,
                            v2_fps = r.V2Fps,
                            fps_adjusted = r.FpsAdjusted,
                            visual_refined_offset = r.VisualRefinedOffset,
                            ransac_offset = r.RansacOffset,
                            detail_text = SyncEngine.BuildAlignDetail(r),
                        });
                    }
                    catch (OperationCanceledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (CancelledException) { store.UpdateTask(sid, job!.Id, status: JobStatus.Cancelled, error: "Cancelled"); }
                    catch (Exception ex) { store.UpdateTask(sid, job!.Id, status: JobStatus.Error, error: ex.Message); }
                    finally { store.EnsureTaskFinished(sid, job!.Id); }
                });

                return Results.Json(new { task_id = job!.Id });
            });
    }
}
