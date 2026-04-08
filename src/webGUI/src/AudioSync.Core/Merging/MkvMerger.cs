using System.Globalization;
using System.Text.RegularExpressions;
using AudioSync.Core.Sessions;
using AudioSync.Core.Tooling;

namespace AudioSync.Core.Merging;






public sealed class MkvMerger
{
    private static readonly Regex ProgressRegex =
        new(@"#GUI#progress\s+([\d.]+)%", RegexOptions.Compiled);

    private readonly IProcessRunner _runner;
    private readonly IToolLocator _locator;

    public MkvMerger(IProcessRunner runner, IToolLocator locator)
    {
        _runner = runner;
        _locator = locator;
    }

    public async Task MuxToMkvAsync(SessionContext ctx,
        Action<string, string>? progressCallback = null, CancellationToken ct = default)
    {
        var mkvm = _locator.Mkvmerge ?? throw new InvalidOperationException("mkvmerge not found");

        var outDir = Path.GetDirectoryName(ctx.OutPath);
        if (!string.IsNullOrEmpty(outDir) && !Directory.Exists(outDir))
            throw new InvalidOperationException($"Output directory does not exist: {outDir}");

        progressCallback?.Invoke("status", "Muxing with mkvmerge...");

        var cmd = new List<string> { "--gui-mode", "-o", ctx.OutPath! };

        if (ctx.DurationLimit.HasValue && ctx.DurationLimit.Value > 0)
        {
            double dl = ctx.DurationLimit.Value;
            int h = (int)(dl / 3600);
            int m = (int)((dl % 3600) / 60);
            double s = dl % 60;
            cmd.Add("--split");
            cmd.Add($"parts:00:00:00.000-{h:D2}:{m:D2}:{s.ToString("00.000", CultureInfo.InvariantCulture)}");
        }

        
        if (ctx.V1VidTids.Count > 0)
        {
            cmd.Add("--video-tracks"); cmd.Add(string.Join(",", ctx.V1VidTids));
            if (ctx.V1VidMetadata is not null)
            {
                for (int i = 0; i < ctx.V1VidTids.Count; i++)
                    if (i < ctx.V1VidMetadata.Count)
                        ApplyMeta(cmd, ctx.V1VidTids[i], ctx.V1VidMetadata[i].Language, ctx.V1VidMetadata[i].Title);
            }
        }
        else cmd.Add("--no-video");

        if (ctx.V1AudTids.Count > 0)
        {
            cmd.Add("--audio-tracks"); cmd.Add(string.Join(",", ctx.V1AudTids));
        }
        else cmd.Add("--no-audio");

        if (ctx.V1SubTids.Count > 0)
        {
            cmd.Add("--subtitle-tracks"); cmd.Add(string.Join(",", ctx.V1SubTids));
        }
        else cmd.Add("--no-subtitles");

        if (!ctx.V1HasAttachments) cmd.Add("--no-attachments");

        
        for (int srcIdx = 0; srcIdx < ctx.V1AudTids.Count; srcIdx++)
        {
            int tid = ctx.V1AudTids[srcIdx];
            var meta = MergeHelpers.AudioMetaForSrcIndex(ctx, srcIdx);
            if (meta is not null) ApplyMeta(cmd, tid, meta.Language, meta.Title);
            if (ctx.DefaultAudioFt.HasValue)
            {
                string flag = ctx.DefaultAudioFt.Value == (0, tid) ? "1" : "0";
                cmd.Add("--default-track-flag"); cmd.Add($"{tid}:{flag}");
            }
        }

        
        if (ctx.V1SubMetadata is not null)
        {
            for (int i = 0; i < ctx.V1SubTids.Count; i++)
                if (i < ctx.V1SubMetadata.Count)
                    ApplyMeta(cmd, ctx.V1SubTids[i], ctx.V1SubMetadata[i].Language, ctx.V1SubMetadata[i].Title);
        }

        cmd.Add(ctx.V1Path!);

        const int fileIdV2 = 1;
        int fileIdV2Subs = fileIdV2;
        bool v2NoAtt = !ctx.V2HasAttachments;

        if (!string.IsNullOrEmpty(ctx.TmpAudioPath))
        {
            var v2Cmd = new List<string> { "--no-video", "--no-subtitles", "--no-attachments" };
            for (int i = 0; i < ctx.V2AudTids.Count; i++)
            {
                int tid = ctx.V2AudTids[i];
                var meta = MergeHelpers.AudioMetaForSrcIndex(ctx, ctx.V1AudTids.Count + i);
                if (meta is not null) ApplyMeta(v2Cmd, tid, meta.Language, meta.Title);
                if (ctx.DefaultAudioFt.HasValue)
                {
                    string flag = ctx.DefaultAudioFt.Value == (fileIdV2, tid) ? "1" : "0";
                    v2Cmd.Add("--default-track-flag"); v2Cmd.Add($"{tid}:{flag}");
                }
            }
            cmd.AddRange(v2Cmd);
            cmd.Add(ctx.TmpAudioPath!);

            if (ctx.V2SubTids.Count > 0 && !string.IsNullOrEmpty(ctx.V2Path))
            {
                fileIdV2Subs = fileIdV2 + 1;
                var v2sCmd = new List<string> { "--no-video", "--no-audio", "--no-attachments",
                    "--subtitle-tracks", string.Join(",", ctx.V2SubTids) };
                if (ctx.V2SubMetadata is not null)
                {
                    for (int i = 0; i < ctx.V2SubTids.Count; i++)
                        if (i < ctx.V2SubMetadata.Count)
                            ApplyMeta(v2sCmd, ctx.V2SubTids[i], ctx.V2SubMetadata[i].Language, ctx.V2SubMetadata[i].Title);
                }
                cmd.AddRange(v2sCmd);
                cmd.Add(ctx.V2Path!);
            }
        }
        else if (!string.IsNullOrEmpty(ctx.V2Path) && ctx.V2Streamcopy)
        {
            var v2Cmd = new List<string> { "--no-video" };
            if (v2NoAtt) v2Cmd.Add("--no-attachments");
            if (ctx.V2AudTids.Count > 0)
            {
                v2Cmd.Add("--audio-tracks"); v2Cmd.Add(string.Join(",", ctx.V2AudTids));
            }
            if (ctx.V2SubTids.Count > 0)
            {
                v2Cmd.Add("--subtitle-tracks"); v2Cmd.Add(string.Join(",", ctx.V2SubTids));
            }
            else v2Cmd.Add("--no-subtitles");

            if (Math.Abs(ctx.Offset) > 0.001)
            {
                int delayMs = (int)Math.Round(ctx.Offset * 1000);
                foreach (var tid in ctx.V2AudTids)
                {
                    v2Cmd.Add("--sync"); v2Cmd.Add($"{tid}:{delayMs}");
                }
            }
            for (int i = 0; i < ctx.V2AudTids.Count; i++)
            {
                int tid = ctx.V2AudTids[i];
                var meta = MergeHelpers.AudioMetaForSrcIndex(ctx, ctx.V1AudTids.Count + i);
                if (meta is not null) ApplyMeta(v2Cmd, tid, meta.Language, meta.Title);
                if (ctx.DefaultAudioFt.HasValue)
                {
                    string flag = ctx.DefaultAudioFt.Value == (fileIdV2, tid) ? "1" : "0";
                    v2Cmd.Add("--default-track-flag"); v2Cmd.Add($"{tid}:{flag}");
                }
            }
            if (ctx.V2SubMetadata is not null)
            {
                for (int i = 0; i < ctx.V2SubTids.Count; i++)
                    if (i < ctx.V2SubMetadata.Count)
                        ApplyMeta(v2Cmd, ctx.V2SubTids[i], ctx.V2SubMetadata[i].Language, ctx.V2SubMetadata[i].Title);
            }
            cmd.AddRange(v2Cmd);
            cmd.Add(ctx.V2Path!);
        }

        
        var orderParts = new List<string>();
        foreach (var tid in ctx.V1VidTids) orderParts.Add($"0:{tid}");
        foreach (var (fid, tid) in ctx.AudioFtOrdered) orderParts.Add($"{fid}:{tid}");
        foreach (var tid in ctx.V1SubTids) orderParts.Add($"0:{tid}");
        foreach (var tid in ctx.V2SubTids) orderParts.Add($"{fileIdV2Subs}:{tid}");
        foreach (var tid in ctx.V1OtherTids) orderParts.Add($"0:{tid}");
        if (orderParts.Count > 0)
        {
            cmd.Add("--track-order"); cmd.Add(string.Join(",", orderParts));
        }

        await RunMkvmergeAsync(mkvm, cmd, progressCallback, ct).ConfigureAwait(false);
    }

    private static void ApplyMeta(List<string> cmd, int tid, string? language, string? title)
    {
        cmd.Add("--language"); cmd.Add($"{tid}:{language ?? "und"}");
        cmd.Add("--track-name"); cmd.Add($"{tid}:{title ?? ""}");
    }

    private async Task RunMkvmergeAsync(string mkvm, List<string> args,
        Action<string, string>? progressCallback, CancellationToken ct)
    {
        
        
        var opts = new ProcessRunOptions
        {
            FileName = mkvm,
            Arguments = args,
            DiscardStdout = false,
            ProgressCallback = progressCallback,
            ProgressPrefix = "mux",
            Timeout = null,
            
        };
        
        var sb = new System.Text.StringBuilder();
        await _runner.RunStreamingAsync(opts, async (chunk, c) =>
        {
            sb.Append(System.Text.Encoding.UTF8.GetString(chunk.Span));
            while (true)
            {
                int nl = -1;
                for (int i = 0; i < sb.Length; i++)
                    if (sb[i] == '\r' || sb[i] == '\n') { nl = i; break; }
                if (nl < 0) break;
                var line = sb.ToString(0, nl);
                sb.Remove(0, nl + 1);
                if (line.Length == 0) continue;
                var m = ProgressRegex.Match(line);
                if (m.Success && progressCallback is not null
                    && double.TryParse(m.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out var pct))
                {
                    int p = Math.Min(99, (int)pct);
                    progressCallback("progress", $"mux:{p}");
                }
            }
            await Task.CompletedTask;
        }, ct).ConfigureAwait(false);
    }
}
