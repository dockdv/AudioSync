using AudioSync.Core.Sync;
using AudioSync.Core.Tooling;

namespace AudioSync.Core.Visual;





public sealed class VisualMatcher : IVisualMatcher
{
    private const double FrameTol = 0.083;
    private const double SimAcceptHigh = 0.8;
    private const double SimAcceptLow = 0.5;
    private const double DarkThreshold = 12.0;

    private readonly FfLib _ff;

    public VisualMatcher(FfLib ff) { _ff = ff; }

    

    private async Task<(double[]? Frame, int H, int W)> ExtractFrameSafeAsync(
        string path, double t, CancellationToken ct)
    {
        try
        {
            var bytes = await _ff.ExtractFrameAsync(path, t, FfLib.FrameW, FfLib.FrameH, ct).ConfigureAwait(false);
            if (bytes is null) return (null, 0, 0);
            return (CutDetector.ToFrame(bytes), FfLib.FrameH, FfLib.FrameW);
        }
        catch (OperationCanceledException) { throw; }
        catch (IOException) { return (null, 0, 0); }
        catch (InvalidOperationException) { return (null, 0, 0); }
    }

    private async Task<double> CompareAtAsync(
        string v1Path, string v2Path, double t1, double t2, CancellationToken ct)
    {
        var f1Task = ExtractFrameSafeAsync(v1Path, t1, ct);
        var f2Task = ExtractFrameSafeAsync(v2Path, t2, ct);
        var f1 = await f1Task.ConfigureAwait(false);
        var f2 = await f2Task.ConfigureAwait(false);
        return PerceptualHash.FrameSimilarity(f1.Frame, f1.H, f1.W, f2.Frame, f2.H, f2.W);
    }

    

    public async Task<bool> ValidateSegmentsVisualAsync(
        string v1Path, string v2Path,
        IList<DetectedSegment> segments,
        double coarseOffset, double speed,
        double dur1, double dur2,
        CancellationToken ct = default)
    {
        if (segments.Count < 2) return true;
        for (int si = 0; si < segments.Count - 1; si++)
        {
            double boundary = segments[si].V1End;
            if (double.IsPositiveInfinity(boundary) || boundary >= 1e9) continue;

            var probesBefore = new[] { 30.0, 15.0, 5.0 }.Select(d => boundary - d).Where(t => t > 0).ToList();
            var probesAfter = new[] { 5.0, 15.0, 30.0 }.Select(d => boundary + d).Where(t => t < dur1).ToList();

            int matchBefore = 0, matchAfter = 0;
            int totalBefore = 0, totalAfter = 0;

            foreach (var t1 in probesBefore)
            {
                ct.ThrowIfCancellationRequested();
                double t2 = (t1 - coarseOffset) / speed;
                if (t2 < 0 || t2 > dur2) continue;
                var sim = await CompareAtAsync(v1Path, v2Path, t1, t2, ct).ConfigureAwait(false);
                totalBefore++;
                if (sim > SimAcceptLow) matchBefore++;
            }
            foreach (var t1 in probesAfter)
            {
                ct.ThrowIfCancellationRequested();
                double t2 = (t1 - coarseOffset) / speed;
                if (t2 < 0 || t2 > dur2) continue;
                var sim = await CompareAtAsync(v1Path, v2Path, t1, t2, ct).ConfigureAwait(false);
                totalAfter++;
                if (sim > SimAcceptLow) matchAfter++;
            }
            bool beforeOk = totalBefore == 0 || (double)matchBefore / totalBefore > 0.5;
            bool afterOk = totalAfter == 0 || (double)matchAfter / totalAfter > 0.5;
            if (!(beforeOk && afterOk)) return false;
        }
        return true;
    }

    

    public async Task<List<DetectedSegment>> RefineBoundaryVisualAsync(
        string v1Path, string v2Path,
        IList<DetectedSegment> segments, double speed,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default)
    {
        var refined = segments.Select(s => new DetectedSegment
        {
            V1Start = s.V1Start, V1End = s.V1End,
            Offset = s.Offset, NInliers = s.NInliers,
        }).ToList();
        if (refined.Count < 2) return refined;

        for (int si = 0; si < refined.Count - 1; si++)
        {
            var seg1 = refined[si];
            double boundary = seg1.V1End;
            if (double.IsPositiveInfinity(boundary) || boundary >= 1e9) continue;
            double off1 = seg1.Offset;

            double lo = Math.Max(0, boundary - 30.0);
            double hi = boundary + 30.0;

            progressCallback?.Invoke("status",
                $"Visual refine boundary {si + 1} ({SyncEngine.FormatTimestamp(lo)}-{SyncEngine.FormatTimestamp(hi)})...");

            for (int iter = 0; iter < 20; iter++)
            {
                ct.ThrowIfCancellationRequested();
                if (hi - lo < 0.1) break;
                double mid = (lo + hi) / 2;
                double v2t = (mid - off1) / speed;
                if (v2t < 0) { lo = mid; continue; }
                var sim = await CompareAtAsync(v1Path, v2Path, mid, v2t, ct).ConfigureAwait(false);
                if (sim > SimAcceptLow) lo = mid;
                else hi = mid;
            }

            double newBoundary = (lo + hi) / 2;
            refined[si].V1End = newBoundary;
            refined[si + 1].V1Start = newBoundary;
        }
        return refined;
    }

    

    public async Task<double?> RefineOffsetVisualAsync(
        string v1Path, string v2Path,
        double offset, double speed,
        double dur1, double dur2,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default)
    {
        if (speed <= 0) return null;
        double margin = Math.Max(60.0, dur1 * 0.05);
        double usable = dur1 - 2 * margin;
        if (usable < 30) return null;

        progressCallback?.Invoke("status", "Visual fine-tune: finding V1 hard cuts...");

        var (v1W, v1H) = await _ff.GetVideoResolutionAsync(v1Path, ct).ConfigureAwait(false);
        if (!v1W.HasValue || !v1H.HasValue || v1W <= 0 || v1H <= 0) return null;

        const int nLocations = 10;
        var locations = Enumerable.Range(1, nLocations)
            .Select(k => margin + usable * k / (nLocations + 1.0)).ToList();
        const double searchLen = 60.0;

        var (v2W, v2H) = await _ff.GetVideoResolutionAsync(v2Path, ct).ConfigureAwait(false);
        if (!v2W.HasValue || !v2H.HasValue || v2W <= 0 || v2H <= 0) return null;

        double v1Fps = await _ff.GetVideoFrameRateAsync(v1Path, ct).ConfigureAwait(false);
        if (v1Fps <= 0) v1Fps = 24.0;

        double v2Fps = await _ff.GetVideoFrameRateAsync(v2Path, ct).ConfigureAwait(false);
        if (v2Fps <= 0) v2Fps = 24.0;

        double v1Ar = (double)v1W.Value / v1H.Value;
        double v2Ar = (double)v2W.Value / v2H.Value;
        double widerAr = Math.Max(v1Ar, v2Ar);

        var allOffsets = new List<double>();
        List<double>? agreeing = null;

        async Task<(double?, double[]?, double[]?)> FindV1Cut(double loc)
        {
            var frames = await _ff.ExtractFrameSequenceAsync(
                v1Path, loc, searchLen, FfLib.FrameW, FfLib.FrameH, v1Fps, ct).ConfigureAwait(false);
            if (frames.Count < 2) return (null, null, null);
            int frameSize = FfLib.FrameW * FfLib.FrameH;
            for (int i = 1; i < frames.Count; i++)
            {
                if ((i & 0x1F) == 0) ct.ThrowIfCancellationRequested();
                var (curT, curBytes) = frames[i];
                var (_, prevBytes) = frames[i - 1];

                double sum = 0;
                for (int k = 0; k < frameSize; k++)
                {
                    double d = (double)curBytes[k] - prevBytes[k];
                    sum += d * d;
                }
                double mse = sum / frameSize;
                if (mse <= CutDetector.MseThreshold) continue;

                double mean = 0;
                for (int k = 0; k < frameSize; k++) mean += curBytes[k];
                mean /= frameSize;
                if (mean < DarkThreshold) continue;

                return (curT, CutDetector.ToFrame(curBytes), CutDetector.ToFrame(prevBytes));
            }
            return (null, null, null);
        }

        async Task<double?> MatchInV2(double v1Time, double[] v1Frame, double[] v1PrevFrame)
        {
            double expectedV2 = (v1Time - offset) / speed;
            double t2Start = Math.Max(0.0, expectedV2 - 10.0);
            double t2End = Math.Min(dur2, expectedV2 + 10.0);
            double duration = t2End - t2Start;
            if (duration <= 0) return null;

            
            var frames = await _ff.ExtractFrameSequenceAsync(
                v2Path, t2Start, duration, v2W.Value, v2H.Value, v2Fps, ct).ConfigureAwait(false);
            if (frames.Count < 2)
            {
                progressCallback?.Invoke("status", $"Visual fine-tune: V2 no frames at {expectedV2:F1}s");
                return null;
            }

            
            var (v1CutCrop, v1CutH, v1CutW) = CutDetector.CropLetterbox(v1Frame, FfLib.FrameH, FfLib.FrameW, v1Ar, widerAr);
            var (v1PrevCrop, v1PrevH, v1PrevW) = CutDetector.CropLetterbox(v1PrevFrame, FfLib.FrameH, FfLib.FrameW, v1Ar, widerAr);

            double bestSim = -1.0;
            double? bestTime = null;
            double bestPrevSim = -1.0;
            int nCuts = 0;
            int frameSize = v2W.Value * v2H.Value;

            for (int i = 1; i < frames.Count; i++)
            {
                if ((i & 0x1F) == 0) ct.ThrowIfCancellationRequested();
                var (curT, curBytes) = frames[i];
                var (_, prevBytes) = frames[i - 1];

                
                double sum = 0;
                for (int k = 0; k < frameSize; k++)
                {
                    double d = (double)curBytes[k] - prevBytes[k];
                    sum += d * d;
                }
                double mse = sum / frameSize;
                if (mse <= CutDetector.MseThreshold) continue;

                
                double mean = 0;
                for (int k = 0; k < frameSize; k++) mean += curBytes[k];
                mean /= frameSize;
                if (mean < DarkThreshold) continue;

                nCuts++;
                var curFrame = CutDetector.ToFrame(curBytes);
                var (v2CutCrop, v2CutH, v2CutW) = CutDetector.CropLetterbox(curFrame, v2H.Value, v2W.Value, v2Ar, widerAr);
                var cutSim = PerceptualHash.FrameSimilarity(v1CutCrop, v1CutH, v1CutW, v2CutCrop, v2CutH, v2CutW);
                if (cutSim <= bestSim) continue;

                
                var prevFrame = CutDetector.ToFrame(prevBytes);
                var (v2PrevCrop, v2PrevH2, v2PrevW2) = CutDetector.CropLetterbox(prevFrame, v2H.Value, v2W.Value, v2Ar, widerAr);
                var prevSim = PerceptualHash.FrameSimilarity(v1PrevCrop, v1PrevH, v1PrevW, v2PrevCrop, v2PrevH2, v2PrevW2);

                bestSim = cutSim;
                bestTime = curT;
                bestPrevSim = prevSim;
            }

            if (!bestTime.HasValue || bestSim < SimAcceptHigh || bestPrevSim < SimAcceptHigh)
            {
                if (nCuts == 0)
                    progressCallback?.Invoke("status",
                        $"Visual fine-tune: V1 {SyncEngine.FormatTimestamp(v1Time)} \u2194 V2 no cuts in {frames.Count} frames \u2717");
                else
                    progressCallback?.Invoke("status",
                        $"Visual fine-tune: V1 {SyncEngine.FormatTimestamp(v1Time)} \u2194 V2 best={SyncEngine.FormatTimestamp(bestTime)} cut={bestSim:F3} prev={bestPrevSim:F3} \u2717");
                return null;
            }

            progressCallback?.Invoke("status",
                $"Visual fine-tune: V1 {SyncEngine.FormatTimestamp(v1Time)} \u2194 V2 {SyncEngine.FormatTimestamp(bestTime)} cut={bestSim:F3} prev={bestPrevSim:F3} \u2713");
            return v1Time - bestTime.Value;
        }

        int locIdx = 0;
        foreach (var loc in locations)
        {
            ct.ThrowIfCancellationRequested();
            locIdx++;
            progressCallback?.Invoke("status",
                $"Visual fine-tune: location {locIdx}/{locations.Count} ({SyncEngine.FormatTimestamp(loc)}) — searching V1 hard cut...");
            var (v1Time, v1Frame, v1PrevFrame) = await FindV1Cut(loc).ConfigureAwait(false);
            if (!v1Time.HasValue || v1Frame is null || v1PrevFrame is null)
            {
                progressCallback?.Invoke("status",
                    $"Visual fine-tune: location {locIdx}/{locations.Count} — no V1 hard cut found ✗");
                continue;
            }
            progressCallback?.Invoke("status", $"Visual fine-tune: matching V1 cut at {SyncEngine.FormatTimestamp(v1Time)} in V2...");
            var matched = await MatchInV2(v1Time.Value, v1Frame, v1PrevFrame).ConfigureAwait(false);
            if (!matched.HasValue) continue;
            allOffsets.Add(matched.Value);
            var group = allOffsets.Where(o => Math.Abs(o - matched.Value) <= FrameTol).ToList();
            if (group.Count >= 3) { agreeing = group; break; }
        }

        if (agreeing is null)
        {
            progressCallback?.Invoke("status",
                $"Visual fine-tune: only {allOffsets.Count} cuts matched, no 3 agree, keeping coarse offset");
            return null;
        }

        agreeing.Sort();
        double refined = agreeing.Count % 2 == 1
            ? agreeing[agreeing.Count / 2]
            : (agreeing[agreeing.Count / 2 - 1] + agreeing[agreeing.Count / 2]) / 2;

        if (Math.Abs(refined) > 5.0)
        {
            progressCallback?.Invoke("status", $"Visual fine-tune: |{refined:F3}s| > 5.0s, discarding");
            return null;
        }
        progressCallback?.Invoke("status",
            $"Visual fine-tune: {agreeing.Count} cuts matched, offset {offset:F3}s -> {refined:F3}s");
        return refined;
    }
}
