using AudioSync.Core.Sessions;
using AudioSync.Core.Probing;
using AudioSync.Core.Tooling;
using AudioSync.Core.Visual;

namespace AudioSync.Core.Sync;





public sealed class SyncEngine : ISyncEngine
{
    private readonly FfLib _ff;
    private readonly AudioLoader _loader;
    private readonly IVisualMatcher? _visual;

    public SyncEngine(FfLib ff, AudioLoader loader, IVisualMatcher? visual = null)
    {
        _ff = ff;
        _loader = loader;
        _visual = visual;
    }

    public async Task<AlignmentResult> AutoAlignAudioAsync(
        SessionContext ctx,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default)
    {
        PrepareAlign(ctx);
        await DecodeAndFingerprintAsync(ctx, progressCallback, ct).ConfigureAwait(false);
        ComputeCoarseAlignment(ctx, progressCallback, ct);

        bool ok = await AlignRansacAsync(ctx, progressCallback, ct).ConfigureAwait(false);
        if (ok)
        {
            FreeAudio(ctx);
        }
        else
        {
            FreeAudio(ctx);
            ctx.AlignMode = "audio-xcorr";
            ctx.AlignA = ctx.XcorrSpeed;
            ctx.AlignB = ctx.CoarseOffset;
            ctx.Segments = new List<DetectedSegment>
            {
                new() { V1Start = 0, V1End = double.PositiveInfinity, Offset = ctx.CoarseOffset, NInliers = 0 }
            };
            ctx.AlignPairs = new();
        }

        var detected = ctx.Segments;

        
        ctx.RansacOffset = ctx.AlignB;

        
        if (_visual != null && ctx.V1HasVideo && ctx.V2HasVideo &&
            (detected == null || detected.Count <= 1))
        {
            var refined = await _visual.RefineOffsetVisualAsync(
                ctx.V1Path!, ctx.V2Path!,
                ctx.AlignB, ctx.AlignA,
                ctx.AlignDur1, ctx.AlignDur2,
                progressCallback, ct).ConfigureAwait(false);
            if (refined.HasValue)
            {
                ctx.VisualRefinedOffset = refined.Value;
                double v2AudioSt = 0.0;
                foreach (var t in ctx.V2Info?.Audio ?? new())
                    if (t.Index == ctx.AlignTrack2) { v2AudioSt = t.StartTime; break; }
                double bAudio = ctx.AlignA * v2AudioSt + refined.Value;
                double delta = bAudio - ctx.AlignB;
                ctx.AlignB = bAudio;
                if (detected != null && detected.Count > 0)
                    foreach (var s in detected) s.Offset += delta;
                var (rmean, rmax, rend) = Ransac.ResidualStats(ctx.AlignPairs, ctx.AlignA, ctx.AlignB);
                ctx.AlignRmean = rmean;
                ctx.AlignRmax = rmax;
                ctx.AlignRend = rend;
            }
        }

        ctx.Atempo = SpeedToAtempo(ctx.AlignA);
        ctx.Offset = ctx.AlignB;

        progressCallback?.Invoke("status", "Align completed.");
        return BuildAlignResult(ctx, detected);
    }

    

    public void PrepareAlign(SessionContext ctx)
    {
        ctx.V1Info ??= new ProbeResult();
        ctx.V2Info ??= new ProbeResult();
        ctx.AlignDur1 = ctx.V1Info.Duration > 0 ? ctx.V1Info.Duration
            : _ff.GetDurationAsync(ctx.V1Path!).GetAwaiter().GetResult();
        ctx.AlignDur2 = ctx.V2Info.Duration > 0 ? ctx.V2Info.Duration
            : _ff.GetDurationAsync(ctx.V2Path!).GetAwaiter().GetResult();
        double hop = AudioConstants.AudioHopSec;
        int maxS = AudioConstants.AudioMaxSamples;
        ctx.AlignHop1 = (ctx.AlignDur1 > 0 && ctx.AlignDur1 / hop > maxS) ? ctx.AlignDur1 / maxS : hop;
        ctx.AlignHop2 = (ctx.AlignDur2 > 0 && ctx.AlignDur2 / hop > maxS) ? ctx.AlignDur2 / maxS : hop;
        ctx.AlignMaxSamples = maxS;
        ctx.V1HasVideo = ctx.V1Info.Streams.Any(s => s.CodecType == "video");
        ctx.V2HasVideo = ctx.V2Info.Streams.Any(s => s.CodecType == "video");
        ctx.DecodeWarnings = new List<string>();
    }

    

    private async Task DecodeAndFingerprintAsync(
        SessionContext ctx, Action<string, string>? cb, CancellationToken ct)
    {
        cb?.Invoke("status", "Decoding V1 + V2 audio...");

        Action<int> mkCb(string label) => pct => cb?.Invoke("status", $"Decoding {label}: {pct}%");

        var t1 = _loader.DecodeFullAudioAsync(ctx.V1Path!, ctx.AlignTrack1,
            AudioConstants.AudioSampleRate, false, ctx.AlignDur1, mkCb("V1"), ct);
        var t2 = _loader.DecodeFullAudioAsync(ctx.V2Path!, ctx.AlignTrack2,
            AudioConstants.AudioSampleRate, false, ctx.AlignDur2, mkCb("V2"), ct);
        var (a1, m1) = await t1.ConfigureAwait(false);
        var (a2, m2) = await t2.ConfigureAwait(false);
        ctx.Audio1 = a1;
        ctx.Audio2 = a2;
        ct.ThrowIfCancellationRequested();
        foreach (var m in m1) ctx.DecodeWarnings.Add($"V1: {m}");
        foreach (var m in m2) ctx.DecodeWarnings.Add($"V2: {m}");

        if (ctx.MeasureLufs)
        {
            cb?.Invoke("status", "Measuring loudness (LUFS)...");
            ctx.V1Lufs = await _ff.MeasureLufsAsync(ctx.V1Path!, ctx.AlignTrack1, ct).ConfigureAwait(false);
            ctx.V2Lufs = await _ff.MeasureLufsAsync(ctx.V2Path!, ctx.AlignTrack2, ct).ConfigureAwait(false);
        }
        else { ctx.V1Lufs = null; ctx.V2Lufs = null; }

        cb?.Invoke("status", "Mel FP: V1...");
        var f1 = Fingerprints.ExtractMel(ctx.Audio1!, AudioConstants.AudioSampleRate,
            ctx.AlignMaxSamples, ctx.AlignHop1, AudioConstants.AudioWindowSec,
            AudioConstants.AudioNMels,
            (c, t) => cb?.Invoke("fp", $"V1 Mel: {c}/{t}"), ct);
        ctx.Ts1 = f1.Timestamps;
        ctx.Fp1Main = f1.Fingerprints;
        ct.ThrowIfCancellationRequested();

        cb?.Invoke("status", "Mel FP: V2...");
        var f2 = Fingerprints.ExtractMel(ctx.Audio2!, AudioConstants.AudioSampleRate,
            ctx.AlignMaxSamples, ctx.AlignHop2, AudioConstants.AudioWindowSec,
            AudioConstants.AudioNMels,
            (c, t) => cb?.Invoke("fp", $"V2 Mel: {c}/{t}"), ct);
        ctx.Ts2 = f2.Timestamps;
        ctx.Fp2Main = f2.Fingerprints;
        ct.ThrowIfCancellationRequested();

        if (ctx.Fp1Main!.Length < 10 || ctx.Fp2Main!.Length < 10)
            throw new InvalidOperationException(
                $"Not enough audio data (V1: {ctx.Fp1Main.Length}, V2: {ctx.Fp2Main.Length})");

        ctx.AlignHop1 = ctx.Ts1.Length > 1 ? MedianDiff(ctx.Ts1) : ctx.AlignHop1;
        ctx.AlignHop2 = ctx.Ts2.Length > 1 ? MedianDiff(ctx.Ts2) : ctx.AlignHop2;
    }

    private static double MedianDiff(double[] ts)
    {
        var diffs = new double[ts.Length - 1];
        for (int i = 0; i < diffs.Length; i++) diffs[i] = ts[i + 1] - ts[i];
        Array.Sort(diffs);
        int n = diffs.Length;
        if ((n & 1) == 1) return diffs[n / 2];
        return (diffs[n / 2 - 1] + diffs[n / 2]) / 2;
    }

    

    private void ComputeCoarseAlignment(SessionContext ctx, Action<string, string>? cb, CancellationToken ct)
    {
        cb?.Invoke("status", "Computing coarse offset + speed (cross-correlation)...");

        float[] xa1, xa2;
        if (ctx.VocalFilter)
        {
            cb?.Invoke("status", "Applying vocal bandreject filter...");
            xa1 = AudioLoader.BandReject(ctx.Audio1!, AudioConstants.AudioSampleRate);
            ct.ThrowIfCancellationRequested();
            xa2 = AudioLoader.BandReject(ctx.Audio2!, AudioConstants.AudioSampleRate);
            ct.ThrowIfCancellationRequested();
        }
        else
        {
            xa1 = ctx.Audio1!;
            xa2 = ctx.Audio2!;
        }

        var (ds1, dsRate) = CrossCorrelation.DownsampleAudio(xa1, AudioConstants.AudioSampleRate);
        ct.ThrowIfCancellationRequested();
        var (ds2, _) = CrossCorrelation.DownsampleAudio(xa2, AudioConstants.AudioSampleRate);
        ct.ThrowIfCancellationRequested();
        ctx.DsRate = dsRate;

        if (ctx.VocalFilter)
        {
            (ctx.Ds1Seg, _) = CrossCorrelation.DownsampleAudio(ctx.Audio1!, AudioConstants.AudioSampleRate);
            ct.ThrowIfCancellationRequested();
            (ctx.Ds2Seg, _) = CrossCorrelation.DownsampleAudio(ctx.Audio2!, AudioConstants.AudioSampleRate);
            ct.ThrowIfCancellationRequested();
        }
        else
        {
            ctx.Ds1Seg = ds1;
            ctx.Ds2Seg = ds2;
        }

        var x = CrossCorrelation.XcorrOnDownsampled(ds1, ds2, dsRate, AudioConstants.SpeedCandidates, returnAltOffsets: true);
        ct.ThrowIfCancellationRequested();
        ctx.CoarseOffset = x.Offset;
        ctx.XcorrSpeed = x.Speed;
        ctx.AltOffsets = (x.AltOffsets ?? new()).Select(t => t.Offset).ToList();

        double v2St = 0.0;
        if (ctx.V2Info != null)
        {
            foreach (var t in ctx.V2Info.Audio)
                if (t.Index == ctx.AlignTrack2) { v2St = t.StartTime; break; }
        }
        ctx.V2StartDelay = v2St;
        ctx.AudioOffset = ctx.CoarseOffset;
        ctx.AudioSpeed = ctx.XcorrSpeed;
        ct.ThrowIfCancellationRequested();
    }

    

    private async Task<bool> AlignRansacAsync(SessionContext ctx, Action<string, string>? cb, CancellationToken ct)
    {
        cb?.Invoke("status", $"Matching {ctx.Fp1Main!.Length}x{ctx.Fp2Main!.Length} fingerprints...");
        var matches = Fingerprints.Match(ctx.Fp1Main!, ctx.Fp2Main!, AudioConstants.AudioMatchTopK);
        ct.ThrowIfCancellationRequested();

        matches = Fingerprints.MutualNearestNeighbors(matches, ctx.Fp1Main!.Length, ctx.Fp2Main!.Length, AudioConstants.AudioMatchTopK);

        var filtered = CrossCorrelation.FilterMatchesByOffset(matches, ctx.Ts1!, ctx.Ts2!, ctx.CoarseOffset, speed: ctx.XcorrSpeed);
        if (filtered.Count >= 20) matches = filtered;
        else
        {
            filtered = CrossCorrelation.FilterMatchesByOffset(matches, ctx.Ts1!, ctx.Ts2!, ctx.CoarseOffset, 30.0, ctx.XcorrSpeed);
            if (filtered.Count >= 20) matches = filtered;
        }

        if (matches.Count < 20)
        {
            cb?.Invoke("status", "Falling back to energy-band matching...");
            var f1e = Fingerprints.ExtractEnergy(ctx.Audio1!, AudioConstants.AudioSampleRate,
                ctx.AlignMaxSamples, ctx.AlignHop1, AudioConstants.AudioWindowSec,
                (c, t) => cb?.Invoke("fp", $"V1 energy: {c}/{t}"), ct).Fingerprints;
            ct.ThrowIfCancellationRequested();
            var f2e = Fingerprints.ExtractEnergy(ctx.Audio2!, AudioConstants.AudioSampleRate,
                ctx.AlignMaxSamples, ctx.AlignHop2, AudioConstants.AudioWindowSec,
                (c, t) => cb?.Invoke("fp", $"V2 energy: {c}/{t}"), ct).Fingerprints;
            ct.ThrowIfCancellationRequested();

            matches = Fingerprints.Match(f1e, f2e, AudioConstants.AudioMatchTopK);
            matches = Fingerprints.MutualNearestNeighbors(matches, f1e.Length, f2e.Length, AudioConstants.AudioMatchTopK);
            filtered = CrossCorrelation.FilterMatchesByOffset(matches, ctx.Ts1!, ctx.Ts2!, ctx.CoarseOffset, speed: ctx.XcorrSpeed);
            if (filtered.Count >= 20) matches = filtered;
        }

        List<(int I, int J, double Sim)> good;
        if (matches.Count == 0) good = new();
        else
        {
            var sims = matches.Select(m => m.Sim).ToArray();
            double thr = Math.Max(0.90, Percentile(sims, 80));
            good = matches.Where(m => m.Sim >= thr).ToList();
            if (good.Count < 20)
            {
                thr = Math.Max(0.80, Percentile(sims, 60));
                good = matches.Where(m => m.Sim >= thr).ToList();
            }
            if (good.Count < 10)
            {
                thr = Math.Max(0.70, Percentile(sims, 40));
                good = matches.Where(m => m.Sim >= thr).ToList();
            }
        }

        if (good.Count < 4) return false;

        var t1m = good.Select(g => ctx.Ts1![g.I]).ToArray();
        var t2m = good.Select(g => ctx.Ts2![g.J]).ToArray();
        double ransacThr = Math.Max(AudioConstants.AudioRansacThresholdSec, (ctx.AlignHop1 + ctx.AlignHop2) * 0.6);
        cb?.Invoke("status", $"RANSAC ({good.Count} candidates, thr={ransacThr:F2}s)...");

        var rfit = Ransac.LinearFit(t1m, t2m, AudioConstants.AudioRansacIterations, ransacThr, null, ct);
        var t1Inliers = rfit.Inliers >= 2 ? Enumerable.Range(0, t1m.Length).Where(i => rfit.Mask[i]).Select(i => t1m[i]).ToArray() : t1m;
        var t2Inliers = rfit.Inliers >= 2 ? Enumerable.Range(0, t2m.Length).Where(i => rfit.Mask[i]).Select(i => t2m[i]).ToArray() : t2m;
        var (a, b) = Ransac.SnapSpeedToCandidate(rfit.A, t1Inliers, t2Inliers);

        var pairs = new List<(double T1, double T2, double Sim)>();
        for (int i = 0; i < good.Count; i++)
            if (rfit.Mask[i]) pairs.Add((ctx.Ts1![good[i].I], ctx.Ts2![good[i].J], good[i].Sim));

        var (rmean, rmax, rend) = Ransac.ResidualStats(pairs, a, b);

        double v1Span = ctx.Ts1![^1] - ctx.Ts1[0];
        double inlierSpan = 0.0;
        if (pairs.Count > 0)
        {
            double mn = double.MaxValue, mx = double.MinValue;
            foreach (var p in pairs) { if (p.T1 < mn) mn = p.T1; if (p.T1 > mx) mx = p.T1; }
            inlierSpan = mx - mn;
        }
        double coverage = v1Span > 0 ? inlierSpan / v1Span : 0.0;

        if (rfit.Inliers < 15 || rmean > 0.5 || coverage < 0.5)
        {
            double aFb = ctx.XcorrSpeed;
            double bFb;
            if (rfit.Inliers >= 2)
            {
                double sum = 0;
                for (int i = 0; i < t1Inliers.Length; i++) sum += t1Inliers[i] - aFb * t2Inliers[i];
                bFb = sum / t1Inliers.Length;
            }
            else bFb = ctx.CoarseOffset;
            var (rmFb, rxFb, reFb) = Ransac.ResidualStats(pairs, aFb, bFb);
            if (rfit.Inliers < 4 || rmFb <= rmean)
            {
                a = aFb; b = bFb;
                rmean = rmFb; rmax = rxFb; rend = reFb;
            }
        }

        cb?.Invoke("status", "Checking for content breaks...");
        var detected = SegmentDetector.Detect(pairs, ctx.XcorrSpeed,
            ctx.CoarseOffset, ctx.Ds1Seg, ctx.Ds2Seg, ctx.DsRate);

        if (detected.Count > 1 && ctx.V1HasVideo && ctx.V2HasVideo && _visual != null)
        {
            cb?.Invoke("status", "Validating segments visually...");
            bool collapse = await _visual.ValidateSegmentsVisualAsync(
                ctx.V1Path!, ctx.V2Path!, detected, ctx.CoarseOffset,
                ctx.XcorrSpeed, ctx.AlignDur1, ctx.AlignDur2, ct).ConfigureAwait(false);
            if (collapse)
            {
                detected = new List<DetectedSegment> { new() {
                    V1Start = 0, V1End = double.PositiveInfinity,
                    Offset = ctx.CoarseOffset, NInliers = pairs.Count } };
            }
            else
            {
                var refined = await _visual.RefineBoundaryVisualAsync(
                    ctx.V1Path!, ctx.V2Path!, detected, ctx.XcorrSpeed, cb, ct).ConfigureAwait(false);
                for (int i = 0; i < refined.Count && i < detected.Count; i++)
                {
                    detected[i].V1Start = refined[i].V1Start;
                    detected[i].V1End = refined[i].V1End;
                }
                
                for (int si = 0; si < detected.Count; si++)
                {
                    var seg = detected[si];
                    int v1S = (int)(seg.V1Start * ctx.DsRate);
                    double v1ERaw = double.IsPositiveInfinity(seg.V1End)
                        ? (double)ctx.Ds1Seg!.Length / ctx.DsRate
                        : seg.V1End;
                    int v1E = (int)(v1ERaw * ctx.DsRate);
                    double prevOff = si > 0 ? detected[si - 1].Offset : ctx.CoarseOffset;
                    double v2Est = (seg.V1Start - prevOff) / ctx.XcorrSpeed;
                    int v2S = Math.Max(0, (int)((v2Est - 300) * ctx.DsRate));
                    int v2E = Math.Min(ctx.Ds2Seg!.Length, (int)((v2Est + (v1ERaw - seg.V1Start) + 300) * ctx.DsRate));
                    int d1Len = Math.Min(ctx.Ds1Seg!.Length, v1E) - v1S;
                    int d2Len = v2E - v2S;
                    if (d1Len > ctx.DsRate * 60 && d2Len > ctx.DsRate * 60)
                    {
                        var d1Sx = new double[d1Len];
                        Array.Copy(ctx.Ds1Seg, v1S, d1Sx, 0, d1Len);
                        var d2Sa = new double[d2Len];
                        Array.Copy(ctx.Ds2Seg, v2S, d2Sa, 0, d2Len);
                        var xs = CrossCorrelation.XcorrOnDownsampled(d1Sx, d2Sa, ctx.DsRate, AudioConstants.SpeedCandidates);
                        if (Math.Abs(xs.Speed - ctx.XcorrSpeed) / ctx.XcorrSpeed <= 0.005)
                        {
                            double v2Abs = v2S / ctx.DsRate;
                            seg.Offset = seg.V1Start + xs.Offset - v2Abs * xs.Speed;
                        }
                    }
                }
            }
        }

        if (detected.Count > 1) b = detected[0].Offset;
        else if (detected.Count == 1)
        {
            if (pairs.Count >= 2)
            {
                double sum = 0;
                foreach (var p in pairs) sum += p.T1 - a * p.T2;
                b = sum / pairs.Count;
            }
            else b = detected[0].Offset;
        }

        ctx.AlignMode = "audio";
        ctx.AlignA = a;
        ctx.AlignB = b;
        ctx.AlignNi = rfit.Inliers;
        ctx.AlignTotalGood = good.Count;
        ctx.AlignPairs = pairs;
        ctx.AlignRmean = rmean;
        ctx.AlignRmax = rmax;
        ctx.AlignRend = rend;
        ctx.Segments = detected;
        return true;
    }

    

    private static double SpeedToAtempo(double a) => Math.Abs(a) > 1e-9 ? 1.0 / a : 1.0;

    private static double GetVideoFps(ProbeResult? info)
    {
        if (info == null) return 0.0;
        foreach (var s in info.Streams)
            if (s.CodecType == "video" && s.FrameRate.HasValue && s.FrameRate.Value > 0)
                return s.FrameRate.Value;
        return 0.0;
    }

    private AlignmentResult BuildAlignResult(SessionContext ctx, List<DetectedSegment>? detected)
    {
        double atempo = SpeedToAtempo(ctx.AlignA);
        double v1Fps = GetVideoFps(ctx.V1Info);
        double v2Fps = GetVideoFps(ctx.V2Info);
        bool fpsAdjusted = false;

        if (v1Fps > 0 && v2Fps > 0)
        {
            double fpsRatio = v1Fps / v2Fps;
            if (Math.Abs(atempo - fpsRatio) / fpsRatio < 0.002)
            {
                double newA = 1.0 / fpsRatio;
                if (Math.Abs(newA - ctx.AlignA) > 1e-9)
                {
                    var pairs = ctx.AlignPairs ?? new();
                    double newB;
                    if (ctx.VisualRefinedOffset.HasValue)
                    {
                        double v2AudioSt = 0.0;
                        foreach (var t in ctx.V2Info?.Audio ?? new())
                            if (t.Index == ctx.AlignTrack2) { v2AudioSt = t.StartTime; break; }
                        newB = newA * v2AudioSt + ctx.VisualRefinedOffset.Value;
                    }
                    else if (pairs.Count >= 2)
                    {
                        double sum = 0;
                        foreach (var p in pairs) sum += p.T1 - newA * p.T2;
                        newB = sum / pairs.Count;
                    }
                    else newB = ctx.AlignB;
                    var (rm, rx, re) = Ransac.ResidualStats(pairs, newA, newB);
                    ctx.AlignA = newA;
                    ctx.AlignB = newB;
                    ctx.AlignRmean = rm;
                    ctx.AlignRmax = rx;
                    ctx.AlignRend = re;
                    atempo = SpeedToAtempo(newA);
                    if (detected != null)
                    {
                        foreach (var seg in detected)
                        {
                            var sp = pairs.Where(p => p.T1 >= seg.V1Start && p.T1 < seg.V1End).ToList();
                            if (sp.Count >= 2)
                            {
                                double sum2 = 0;
                                foreach (var p in sp) sum2 += p.T1 - newA * p.T2;
                                seg.Offset = sum2 / sp.Count;
                            }
                        }
                    }
                    fpsAdjusted = true;
                }
            }
        }

        return new AlignmentResult
        {
            SpeedRatio = atempo,
            Offset = ctx.AlignB,
            LinearA = ctx.AlignA,
            LinearB = ctx.AlignB,
            InlierCount = ctx.AlignNi,
            TotalCandidates = ctx.AlignTotalGood,
            InlierPairs = ctx.AlignPairs ?? new(),
            V1Coverage = ctx.Ts1 != null && ctx.Ts1.Length > 0 ? (ctx.Ts1[0], ctx.Ts1[^1]) : (0, 0),
            V2Coverage = ctx.Ts2 != null && ctx.Ts2.Length > 0 ? (ctx.Ts2[0], ctx.Ts2[^1]) : (0, 0),
            V1Interval = ctx.AlignHop1,
            V2Interval = ctx.AlignHop2,
            Mode = ctx.AlignMode,
            SyncTracks = (ctx.AlignTrack1, ctx.AlignTrack2),
            ResidualMean = ctx.AlignRmean,
            ResidualMax = ctx.AlignRmax,
            ResidualEnd = ctx.AlignRend,
            CoarseOffset = ctx.CoarseOffset,
            Segments = detected,
            Warnings = ctx.DecodeWarnings,
            AudioOffset = ctx.AudioOffset,
            AudioSpeed = ctx.AudioSpeed,
            V1Lufs = ctx.V1Lufs,
            V2Lufs = ctx.V2Lufs,
            V2StartDelay = ctx.V2StartDelay,
            V1Fps = v1Fps,
            V2Fps = v2Fps,
            FpsAdjusted = fpsAdjusted,
            VisualRefinedOffset = ctx.VisualRefinedOffset,
            RansacOffset = ctx.RansacOffset,
        };
    }

    public static void FreeAudio(SessionContext ctx)
    {
        ctx.Audio1 = null;
        ctx.Audio2 = null;
    }

    
    public static string FormatTimestamp(double? seconds)
    {
        if (!seconds.HasValue || double.IsNaN(seconds.Value)) return "0:00.000";
        double s = seconds.Value;
        string sign = s < 0 ? "-" : "";
        s = Math.Abs(s);
        int h = (int)(s / 3600);
        int m = (int)((s % 3600) / 60);
        double sec = s % 60;
        if (h > 0) return $"{sign}{h}:{m:D2}:{sec:00.000}";
        return $"{sign}{m}:{sec:00.000}";
    }

    public static string BuildAlignDetail(AlignmentResult r)
    {
        static string FmtSigned(double v) => $"{(v >= 0 ? "+" : "")}{v:0.000}s";
        var sb = new System.Text.StringBuilder();
        double aoSpd = r.AudioSpeed != 0 ? r.AudioSpeed : r.SpeedRatio;
        double aoOff = r.AudioOffset;
        sb.Append("Audio coarse offset: ").Append(FmtSigned(aoOff)).Append('\n');
        if (r.RansacOffset.HasValue)
            sb.Append("Audio fine offset:   ").Append(FmtSigned(r.RansacOffset.Value)).Append('\n');
        if (r.VisualRefinedOffset.HasValue)
            sb.Append("Visual fine offset:  ").Append(FmtSigned(r.VisualRefinedOffset.Value)).Append('\n');
        sb.Append("Speed:               ").Append((1.0 / aoSpd).ToString("0.000000")).Append('\n');
        if (r.V1Fps > 0 && r.V2Fps > 0)
        {
            sb.Append("Framerate:        V1=").Append(r.V1Fps.ToString("0.000"))
              .Append("  V2=").Append(r.V2Fps.ToString("0.000"));
            if (r.FpsAdjusted) sb.Append("  (atempo snapped to fps ratio)");
            sb.Append('\n');
        }
        if (r.V2StartDelay > 0.01)
            sb.Append("V2 start delay:   ").Append(r.V2StartDelay.ToString("0.000")).Append("s\n");
        sb.Append(new string('\u2500', 38)).Append('\n');
        sb.Append("V1 hop: ").Append(r.V1Interval.ToString("0.00"))
          .Append("s  V2 hop: ").Append(r.V2Interval.ToString("0.00")).Append("s\n");
        sb.Append("V1: ").Append(FormatTimestamp(r.V1Coverage.Lo))
          .Append(" - ").Append(FormatTimestamp(r.V1Coverage.Hi)).Append('\n');
        sb.Append("V2: ").Append(FormatTimestamp(r.V2Coverage.Lo))
          .Append(" - ").Append(FormatTimestamp(r.V2Coverage.Hi)).Append('\n');
        sb.Append("Residual: avg=").Append(r.ResidualMean.ToString("0.000"))
          .Append("s max=").Append(r.ResidualMax.ToString("0.000")).Append("s\n");

        var segs = r.Segments ?? new();
        if (segs.Count > 1)
        {
            sb.Append(new string('\u2500', 38)).Append('\n');
            sb.Append("SEGMENTS: ").Append(segs.Count).Append(" (content breaks detected)\n");
            for (int i = 0; i < segs.Count; i++)
            {
                var s = segs[i];
                string sEnd = double.IsPositiveInfinity(s.V1End) ? "end" : FormatTimestamp(s.V1End);
                sb.Append("  #").Append(i + 1).Append(": ").Append(FormatTimestamp(s.V1Start))
                  .Append(" - ").Append(sEnd)
                  .Append("  offset=").Append(FmtSigned(s.Offset))
                  .Append("  (").Append(s.NInliers).Append(" matches)\n");
            }
        }

        sb.Append(new string('\u2500', 38)).Append('\n');
        var pairs = r.InlierPairs ?? new();
        int step = Math.Max(1, pairs.Count / 10);
        sb.Append("        V1         V2    Sim\n");
        for (int i = 0; i < pairs.Count; i += step)
        {
            var p = pairs[i];
            sb.Append(FormatTimestamp(p.T1).PadLeft(10))
              .Append(' ').Append(FormatTimestamp(p.T2).PadLeft(10))
              .Append(' ').Append(p.Sim.ToString("0.000")).Append('\n');
        }
        return sb.ToString();
    }

    private static double Percentile(double[] sims, double p)
    {
        if (sims.Length == 0) return 0;
        var sorted = (double[])sims.Clone();
        Array.Sort(sorted);
        double idx = (sorted.Length - 1) * p / 100.0;
        int lo = (int)Math.Floor(idx);
        int hi = (int)Math.Ceiling(idx);
        if (lo == hi) return sorted[lo];
        double frac = idx - lo;
        return sorted[lo] * (1 - frac) + sorted[hi] * frac;
    }

}
