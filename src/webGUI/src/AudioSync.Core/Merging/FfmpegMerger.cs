using System.Globalization;
using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;
using AudioSync.Core.Tooling;

namespace AudioSync.Core.Merging;









public sealed class FfmpegMerger : IMerger
{
    private readonly IProcessRunner _runner;
    private readonly IToolLocator _locator;
    private readonly IMediaProber _prober;
    private readonly MkvMerger _mkv;

    public FfmpegMerger(IProcessRunner runner, IToolLocator locator, IMediaProber prober, MkvMerger mkv)
    {
        _runner = runner;
        _locator = locator;
        _prober = prober;
        _mkv = mkv;
    }

    private static void EmitPhases(Action<string, string>? cb, bool willEncode, bool willSub)
    {
        if (cb is null) return;
        var phases = new List<string>(3);
        if (willEncode) phases.Add("enc");
        phases.Add("mux");
        if (willSub) phases.Add("sub");
        cb("progress", "phases:" + string.Join(",", phases));
    }

    public async Task MergeAsync(
        SessionContext ctx,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default)
    {
        await PrepareMergeAsync(ctx, ct).ConfigureAwait(false);

        var outDir = Path.GetDirectoryName(ctx.OutPath);
        if (!string.IsNullOrEmpty(outDir) && !Directory.Exists(outDir))
            throw new InvalidOperationException($"Output directory does not exist: {outDir}");

        var tmpDir = Directory.CreateDirectory(
            Path.Combine(string.IsNullOrEmpty(outDir) ? "." : outDir,
                         $".audiosync_tmp_{Guid.NewGuid():N}")).FullName;
        var tmpAudio = Path.Combine(tmpDir, "audio.mka");
        var tmpNosubs = Path.Combine(tmpDir, "nosubs.mkv");

        try
        {
            bool useMkvmerge = ctx.OutPath!.EndsWith(".mkv", StringComparison.OrdinalIgnoreCase);
            bool streamcopyV2 = false;
            if (!ctx.IsRemux)
            {
                int nSegments = ctx.Segments?.Count ?? 1;
                bool usePiecewise = ctx.Segments is not null && nSegments > 1;

                Dictionary<int, double>? v2Gains = null;
                if (ctx.GainMatch && ctx.V1Lufs.HasValue && ctx.V2Lufs.HasValue)
                {
                    double gain = Math.Max(-20.0, Math.Min(20.0, ctx.V1Lufs.Value - ctx.V2Lufs.Value));
                    if (Math.Abs(gain) > 0.01)
                    {
                        v2Gains = new();
                        foreach (var tidx in ctx.V2AudIndices) v2Gains[tidx] = gain;
                        progressCallback?.Invoke("status",
                            $"Gain match: V1={ctx.V1Lufs.Value:F1} LUFS, V2={ctx.V2Lufs.Value:F1} LUFS \u2192 {gain:+0.0;-0.0} dB");
                    }
                }

                streamcopyV2 = CanStreamcopyV2(ctx.Atempo, usePiecewise, v2Gains);
                if (streamcopyV2)
                {
                    progressCallback?.Invoke("status", "Stream-copy mode: skipping audio re-encode");
                }

                
                EmitPhases(progressCallback, willEncode: !streamcopyV2,
                    willSub: !useMkvmerge && ctx.V1HasSubs);

                if (!streamcopyV2)
                    await MergePass1AudioAsync(ctx, tmpAudio, usePiecewise, v2Gains, progressCallback, ct).ConfigureAwait(false);
            }
            else
            {
                EmitPhases(progressCallback, willEncode: false,
                    willSub: !useMkvmerge && ctx.V1HasSubs);
            }

            if (!ctx.IsRemux && !streamcopyV2)
                SetV2Mode(ctx, tmpAudioPath: tmpAudio);
            else if (!ctx.IsRemux && streamcopyV2)
                SetV2Mode(ctx, streamcopy: true);
            else
                SetV2Mode(ctx);

            if (useMkvmerge)
            {
                await _mkv.MuxToMkvAsync(ctx, progressCallback, ct).ConfigureAwait(false);
            }
            else
            {
                await MuxPassAsync(ctx, ctx.V1HasSubs ? tmpNosubs : ctx.OutPath!, progressCallback, ct).ConfigureAwait(false);
                if (ctx.V1HasSubs)
                    await MergePass3SubsAsync(ctx, tmpNosubs, ctx.OutPath!, progressCallback, ct).ConfigureAwait(false);
            }
        }
        finally
        {
            try { if (Directory.Exists(tmpDir)) Directory.Delete(tmpDir, recursive: true); } catch { }
        }

        progressCallback?.Invoke("progress", "mux:100");
        progressCallback?.Invoke("status", ctx.IsRemux ? "Remux completed." : "Merge completed.");
    }

    

    private async Task PrepareMergeAsync(SessionContext ctx, CancellationToken ct)
    {
        ctx.IsRemux = string.IsNullOrEmpty(ctx.V2Path);
        ctx.FfmpegPath = _locator.Ffmpeg ?? throw new InvalidOperationException("ffmpeg not found");

        if (ctx.V1Info is null)
            ctx.V1Info = await _prober.ProbeAsync(ctx.V1Path!, ct).ConfigureAwait(false);
        if (ctx.V2Info is null) ctx.V2Info = new ProbeResult();

        var v1Audio = ctx.V1Info.Audio;
        ctx.V1SampleRate = v1Audio.Count > 0 && v1Audio[0].SampleRate > 0
            ? v1Audio[0].SampleRate
            : await _prober.GetAudioSampleRateAsync(ctx.V1Path!, 0, ct).ConfigureAwait(false);

        ctx.V1Dur = ctx.V1Duration > 0 ? ctx.V1Duration
            : (ctx.V1Info.Duration > 0 ? ctx.V1Info.Duration
                : await _prober.GetDurationAsync(ctx.V1Path!, ct).ConfigureAwait(false));

        if (ctx.DurationLimit.HasValue && ctx.DurationLimit.Value > 0
            && (ctx.V1Dur <= 0 || ctx.DurationLimit.Value < ctx.V1Dur))
        {
            ctx.V1Dur = ctx.DurationLimit.Value;
        }

        MergeHelpers.ClassifyV1Streams(ctx);
        MergeHelpers.ComputeV1Tids(ctx);
        if (!ctx.IsRemux) MergeHelpers.ClassifyV2Streams(ctx);
    }

    
    public static void SetV2Mode(SessionContext ctx, string? tmpAudioPath = null, bool streamcopy = false)
    {
        ctx.TmpAudioPath = tmpAudioPath;
        ctx.V2Streamcopy = streamcopy;
        MergeHelpers.ComputeV2Tids(ctx);
        MergeHelpers.ComputeAudioOrdering(ctx);
    }

    private static bool CanStreamcopyV2(double atempo, bool usePiecewise, Dictionary<int, double>? v2Gains)
    {
        if (Math.Abs(atempo - 1.0) > 0.0001) return false;
        if (usePiecewise) return false;
        if (v2Gains is { Count: > 0 }) return false;
        return true;
    }

    

    private async Task<Dictionary<int, long>> GetV2BitratesAsync(SessionContext ctx, CancellationToken ct)
    {
        var result = new Dictionary<int, long>();
        try
        {
            var info = ctx.V2Info ?? await _prober.ProbeAsync(ctx.V2Path!, ct).ConfigureAwait(false);
            var tracks = info.Audio;
            foreach (var tidx in ctx.V2AudIndices)
            {
                long br = tidx < tracks.Count ? tracks[tidx].BitRate : 0;
                result[tidx] = br;
            }
            if (result.Values.All(b => b == 0))
            {
                double duration = info.Duration;
                if (duration > 0)
                {
                    try
                    {
                        long fileSize = new FileInfo(ctx.V2Path!).Length;
                        int nAudio = Math.Max(1, tracks.Count);
                        long avg = (long)(fileSize * 8 / duration / nAudio);
                        foreach (var k in result.Keys.ToList()) result[k] = avg;
                    }
                    catch { }
                }
            }
        }
        catch
        {
            foreach (var tidx in ctx.V2AudIndices) result[tidx] = 0;
        }
        return result;
    }

    private static string PickAacBitrate(long sourceBr)
    {
        if (sourceBr <= 0) return "192k";
        long capped = Math.Min(sourceBr, 192_000);
        capped = Math.Max(capped, 64_000);
        return $"{capped / 1000}k";
    }

    

    private static (string Filter, string OutputLabel, int InputsConsumed) BuildPiecewiseFilter(
        double atempo, IList<DetectedSegment> segments, int v1Sr, double v1Dur,
        int v2Track = 0, int inputBase = 1, double? gainDb = null)
    {
        int n = segments.Count;
        var tempoParts = MergeHelpers.AtempoChain(atempo);
        string tempoChain = tempoParts.Count > 0 ? string.Join(",", tempoParts) : "";
        string gainFilter = gainDb.HasValue && Math.Abs(gainDb.Value) > 0.01
            ? $"volume={gainDb.Value.ToString("F2", CultureInfo.InvariantCulture)}dB" : "";
        string baseFilters = string.Join(",",
            new[] { tempoChain, $"aresample={v1Sr}", gainFilter }.Where(f => f.Length > 0));

        var lines = new List<string>();
        var segLabels = new List<string>();
        double? prevV2End = null;
        int nextInput = inputBase;

        for (int i = 0; i < n; i++)
        {
            var seg = segments[i];
            double off = seg.Offset;
            double v1S = seg.V1Start;
            double v1E = v1Dur > 0 ? Math.Min(seg.V1End, v1Dur) : seg.V1End;
            if (double.IsPositiveInfinity(v1E) || v1E > 1e8)
                v1E = v1Dur > 0 ? v1Dur : 36000;

            double trimStartPre = Math.Max(0.0, (v1S - off) * atempo);
            double trimEndPre = Math.Max(trimStartPre + 0.001, (v1E - off) * atempo);
            double segDur = v1E - v1S;

            double gapV1 = 0.0;
            if (prevV2End.HasValue && trimStartPre < prevV2End.Value)
            {
                gapV1 = Math.Max(0.0, prevV2End.Value / atempo + off - v1S);
                gapV1 = Math.Min(gapV1, segDur);
                trimStartPre = prevV2End.Value;
                trimEndPre = Math.Max(trimStartPre + 0.001, trimEndPre);
            }

            prevV2End = trimEndPre;
            string outLabel = $"[_seg{i}]";
            segLabels.Add(outLabel);

            if (gapV1 > 0.01)
            {
                double gapV2 = gapV1 * atempo;
                int gapIdx = nextInput++;
                string gapIn = $"{gapIdx}:a:{v2Track}";
                string gapLbl = $"[_gap{i}]";
                var gf = new List<string>
                {
                    $"[{gapIn}]asetpts=PTS-STARTPTS",
                    $"atrim=end={MergeHelpers.F6(gapV2)}",
                    "asetpts=PTS-STARTPTS",
                    "volume=0",
                };
                if (baseFilters.Length > 0) gf.Add(baseFilters);
                lines.Add(string.Join(",", gf) + gapLbl);

                int audIdx = nextInput++;
                string audIn = $"{audIdx}:a:{v2Track}";
                string audLbl = $"[_aud{i}]";
                var af = new List<string>
                {
                    $"[{audIn}]asetpts=PTS-STARTPTS",
                    $"atrim=start={MergeHelpers.F6(trimStartPre)}:end={MergeHelpers.F6(trimEndPre)}",
                    "asetpts=PTS-STARTPTS",
                };
                if (baseFilters.Length > 0) af.Add(baseFilters);
                lines.Add(string.Join(",", af) + audLbl);

                lines.Add($"{gapLbl}{audLbl}concat=n=2:v=0:a=1,apad=whole_dur={MergeHelpers.F6(segDur)}{outLabel}");
            }
            else
            {
                int inputIdx = nextInput++;
                string inLabel = $"{inputIdx}:a:{v2Track}";
                var parts = new List<string>
                {
                    $"[{inLabel}]asetpts=PTS-STARTPTS",
                    $"atrim=start={MergeHelpers.F6(trimStartPre)}:end={MergeHelpers.F6(trimEndPre)}",
                    "asetpts=PTS-STARTPTS",
                };
                if (baseFilters.Length > 0) parts.Add(baseFilters);

                double neededDelay = Math.Max(0.0, off - v1S);
                neededDelay = Math.Min(neededDelay, segDur);
                if (neededDelay > 0.01)
                {
                    int delayMs = (int)Math.Round(neededDelay * 1000);
                    parts.Add($"adelay={delayMs}:all=1");
                }
                parts.Add($"apad=whole_dur={MergeHelpers.F6(segDur)}");
                lines.Add(string.Join(",", parts) + outLabel);
            }
        }

        const string outputLabel = "[_v2out]";
        string segIn = string.Concat(segLabels);
        lines.Add($"{segIn}concat=n={n}:v=0:a=1{outputLabel}");
        return (string.Join("; ", lines), outputLabel, nextInput - inputBase);
    }

    

    private async Task MergePass1AudioAsync(
        SessionContext ctx, string tmpAudio, bool usePiecewise,
        Dictionary<int, double>? v2Gains,
        Action<string, string>? progressCallback, CancellationToken ct)
    {
        progressCallback?.Invoke("status", "Pass 1: encoding audio...");
        var v2Bitrates = await GetV2BitratesAsync(ctx, ct).ConfigureAwait(false);
        var v2Tracks = ctx.V2Info?.Audio ?? new();

        var cmd = new List<string> { "-y", "-hide_banner" };

        if (usePiecewise)
        {
            var fcParts = new List<string>();
            var outputLabels = new List<string>();
            int runningBase = 0;
            for (int i = 0; i < ctx.V2AudIndices.Count; i++)
            {
                int tidx = ctx.V2AudIndices[i];
                double trackSt = 0.0;
                foreach (var t in v2Tracks)
                    if (t.Index == tidx) { trackSt = t.StartTime; break; }
                IList<DetectedSegment> trackSegments = ctx.Segments!;
                if (trackSt > 0.001)
                    trackSegments = ctx.Segments!.Select(s => new DetectedSegment
                    {
                        V1Start = s.V1Start, V1End = s.V1End,
                        Offset = s.Offset + trackSt, NInliers = s.NInliers,
                    }).ToList();

                double? trackGain = (v2Gains is not null && v2Gains.TryGetValue(tidx, out var g)) ? g : null;
                var (fg, outLabel, nInputs) = BuildPiecewiseFilter(
                    ctx.Atempo, trackSegments, ctx.V1SampleRate, ctx.V1Dur,
                    v2Track: tidx, inputBase: runningBase, gainDb: trackGain);
                if (ctx.V2AudIndices.Count > 1)
                {
                    fg = fg.Replace("[_", $"[_t{i}_");
                    outLabel = outLabel.Replace("[_", $"[_t{i}_");
                }
                fcParts.Add(fg);
                outputLabels.Add(outLabel);
                runningBase += nInputs;
            }

            for (int k = 0; k < runningBase; k++)
            {
                cmd.Add("-i"); cmd.Add(ctx.V2Path!);
            }
            cmd.Add("-filter_complex"); cmd.Add(string.Join("; ", fcParts));
            foreach (var label in outputLabels) { cmd.Add("-map"); cmd.Add(label); }

            for (int i = 0; i < ctx.V2AudIndices.Count; i++)
            {
                int tidx = ctx.V2AudIndices[i];
                string br = PickAacBitrate(v2Bitrates.GetValueOrDefault(tidx, 0));
                cmd.Add($"-c:a:{i}"); cmd.Add("aac");
                cmd.Add($"-b:a:{i}"); cmd.Add(br);
            }
        }
        else
        {
            cmd.Add("-i"); cmd.Add(ctx.V2Path!);
            foreach (var tidx in ctx.V2AudIndices) { cmd.Add("-map"); cmd.Add($"0:a:{tidx}"); }

            for (int i = 0; i < ctx.V2AudIndices.Count; i++)
            {
                int tidx = ctx.V2AudIndices[i];
                double trackSt = 0.0;
                foreach (var t in v2Tracks)
                    if (t.Index == tidx) { trackSt = t.StartTime; break; }
                double trackDelay = ctx.Offset + trackSt;

                var filters = new List<string> { "asetpts=PTS-STARTPTS" };
                if (trackDelay < -0.001)
                {
                    double trim = Math.Abs(trackDelay);
                    if (Math.Abs(ctx.Atempo - 1.0) > 0.0001) trim = Math.Abs(trackDelay) * ctx.Atempo;
                    filters.Add($"atrim=start={MergeHelpers.F6(trim)}");
                    filters.Add("asetpts=PTS-STARTPTS");
                }
                filters.AddRange(MergeHelpers.AtempoChain(ctx.Atempo));
                if (trackDelay > 0.001)
                {
                    int delayMs = (int)Math.Round(trackDelay * 1000);
                    filters.Add($"adelay={delayMs}:all=1");
                }
                filters.Add($"aresample={ctx.V1SampleRate}");
                if (v2Gains is not null && v2Gains.TryGetValue(tidx, out var trackGain) && Math.Abs(trackGain) > 0.01)
                    filters.Add($"volume={trackGain.ToString("F2", CultureInfo.InvariantCulture)}dB");
                if (ctx.V1Dur > 0)
                    filters.Add($"apad=whole_dur={MergeHelpers.F6(ctx.V1Dur)}");

                cmd.Add($"-filter:a:{i}"); cmd.Add(string.Join(",", filters));
                string br = PickAacBitrate(v2Bitrates.GetValueOrDefault(tidx, 0));
                cmd.Add($"-c:a:{i}"); cmd.Add("aac");
                cmd.Add($"-b:a:{i}"); cmd.Add(br);
            }
        }

        if (ctx.V1Dur > 0) { cmd.Add("-t"); cmd.Add(MergeHelpers.F6(ctx.V1Dur)); }
        cmd.Add(tmpAudio);

        await RunFfmpegAsync(ctx, cmd, "enc", progressCallback, ct).ConfigureAwait(false);
    }

    

    private async Task MuxPassAsync(SessionContext ctx, string outPath,
        Action<string, string>? progressCallback, CancellationToken ct)
    {
        bool isStreamcopy = ctx.V2Streamcopy;
        string label = (ctx.TmpAudioPath is not null || isStreamcopy)
            ? "muxing..." : "muxing video + audio...";
        string passNum = ctx.TmpAudioPath is not null ? "2" : "1";
        progressCallback?.Invoke("status", $"Pass {passNum}: {label}");

        var v1Vid = ctx.V1VidSi;
        var v1Aud = ctx.V1AudSi;
        var v1Rest = new List<int>(ctx.V1OtherSi);
        v1Rest.AddRange(ctx.V1SubSi);
        if (ctx.V1HasSubs)
            v1Rest = v1Rest.Where(si => ctx.V1StreamTypes.GetValueOrDefault(si) != "subtitle").ToList();

        var cmd = new List<string> { "-y", "-hide_banner", "-i", ctx.V1Path! };
        var v2InputMap = new Dictionary<int, int>();

        if (ctx.TmpAudioPath is not null)
        {
            cmd.Add("-i"); cmd.Add(ctx.TmpAudioPath);
        }
        else if (isStreamcopy)
        {
            var v2Tracks = ctx.V2Info?.Audio ?? new();
            int nextInput = 1;
            foreach (var tidx in ctx.V2AudIndices)
            {
                double trackSt = 0.0;
                foreach (var t in v2Tracks)
                    if (t.Index == tidx) { trackSt = t.StartTime; break; }
                double trackDelay = ctx.Offset + trackSt;
                if (trackDelay < -0.001)
                {
                    cmd.Add("-ss"); cmd.Add(MergeHelpers.F6(Math.Abs(trackDelay)));
                }
                else if (trackDelay > 0.001)
                {
                    cmd.Add("-itsoffset"); cmd.Add(MergeHelpers.F6(trackDelay));
                }
                cmd.Add("-i"); cmd.Add(ctx.V2Path!);
                v2InputMap[tidx] = nextInput++;
            }
        }

        foreach (var si in v1Vid) { cmd.Add("-map"); cmd.Add($"0:{si}"); }

        var audioMaps = v1Aud.Select(si => $"0:{si}").ToList();
        if (ctx.TmpAudioPath is not null)
            audioMaps.AddRange(Enumerable.Range(0, ctx.V2AudIndices.Count).Select(i => $"1:a:{i}"));
        else if (isStreamcopy)
            audioMaps.AddRange(ctx.V2AudIndices.Select(tidx => $"{v2InputMap[tidx]}:a:{tidx}"));

        if (ctx.AudioOrder is not null && ctx.AudioOrder.Count == audioMaps.Count
            && ctx.AudioOrder.All(i => i >= 0 && i < audioMaps.Count))
        {
            
            audioMaps = ctx.AudioOrder.Select(i => audioMaps[i]).ToList();
        }
        foreach (var m in audioMaps) { cmd.Add("-map"); cmd.Add(m); }
        foreach (var si in v1Rest) { cmd.Add("-map"); cmd.Add($"0:{si}"); }

        cmd.Add("-c"); cmd.Add("copy");

        if (ctx.AudioMetadata is not null)
        {
            for (int i = 0; i < ctx.AudioMetadata.Count; i++)
            {
                var meta = ctx.AudioMetadata[i];
                cmd.Add($"-metadata:s:a:{i}"); cmd.Add($"language={meta.Language ?? ""}");
                cmd.Add($"-metadata:s:a:{i}"); cmd.Add($"title={meta.Title ?? ""}");
            }
        }
        if (ctx.V1VidMetadata is not null)
        {
            for (int i = 0; i < ctx.V1VidMetadata.Count; i++)
            {
                var meta = ctx.V1VidMetadata[i];
                cmd.Add($"-metadata:s:v:{i}"); cmd.Add($"language={meta.Language ?? ""}");
                cmd.Add($"-metadata:s:v:{i}"); cmd.Add($"title={meta.Title ?? ""}");
            }
        }
        if (ctx.DefaultAudioIndex.HasValue)
        {
            int n = audioMaps.Count;
            for (int i = 0; i < n; i++)
            {
                string disp = i == ctx.DefaultAudioIndex.Value ? "default" : "0";
                cmd.Add($"-disposition:a:{i}"); cmd.Add(disp);
            }
        }
        if (ctx.V1Dur > 0) { cmd.Add("-t"); cmd.Add(MergeHelpers.F6(ctx.V1Dur)); }
        cmd.Add(outPath);

        await RunFfmpegAsync(ctx, cmd, "mux", progressCallback, ct).ConfigureAwait(false);
    }

    

    private async Task MergePass3SubsAsync(SessionContext ctx, string nosubsPath, string outPath,
        Action<string, string>? progressCallback, CancellationToken ct)
    {
        progressCallback?.Invoke("status", "Pass 3: adding subtitles...");
        var cmd = new List<string> { "-y", "-hide_banner",
            "-i", nosubsPath, "-i", ctx.V1Path!, "-map", "0" };
        foreach (var si in ctx.V1SubSi) { cmd.Add("-map"); cmd.Add($"1:{si}"); }

        int nextInput = 2;
        if (ctx.V2SubSi.Count > 0 && !string.IsNullOrEmpty(ctx.V2Path))
        {
            cmd.Add("-i"); cmd.Add(ctx.V2Path!);
            foreach (var si in ctx.V2SubSi) { cmd.Add("-map"); cmd.Add($"{nextInput}:{si}"); }
        }

        cmd.Add("-c"); cmd.Add("copy");

        int subIdx = 0;
        if (ctx.V1SubMetadata is not null)
        {
            foreach (var meta in ctx.V1SubMetadata)
            {
                cmd.Add($"-metadata:s:s:{subIdx}"); cmd.Add($"language={meta.Language ?? ""}");
                cmd.Add($"-metadata:s:s:{subIdx}"); cmd.Add($"title={meta.Title ?? ""}");
                subIdx++;
            }
        }
        if (ctx.V2SubMetadata is not null)
        {
            foreach (var meta in ctx.V2SubMetadata)
            {
                cmd.Add($"-metadata:s:s:{subIdx}"); cmd.Add($"language={meta.Language ?? ""}");
                cmd.Add($"-metadata:s:s:{subIdx}"); cmd.Add($"title={meta.Title ?? ""}");
                subIdx++;
            }
        }
        if (ctx.V1Dur > 0) { cmd.Add("-t"); cmd.Add(MergeHelpers.F6(ctx.V1Dur)); }
        cmd.Add(outPath);

        await RunFfmpegAsync(ctx, cmd, "sub", progressCallback, ct).ConfigureAwait(false);
    }

    

    private async Task RunFfmpegAsync(SessionContext ctx, List<string> args, string progressPrefix,
        Action<string, string>? progressCallback, CancellationToken ct)
    {
        await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = ctx.FfmpegPath!,
            Arguments = args,
            DiscardStdout = true,
            ProgressCallback = progressCallback,
            ProgressPrefix = progressPrefix,
            Duration = ctx.V1Dur,
            Timeout = null,
        }, ct).ConfigureAwait(false);
    }
}
