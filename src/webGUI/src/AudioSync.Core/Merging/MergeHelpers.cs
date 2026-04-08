using System.Globalization;
using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;

namespace AudioSync.Core.Merging;





public static class MergeHelpers
{
    private static readonly Dictionary<string, string> MkvTypeMap = new()
    {
        ["video"] = "video", ["audio"] = "audio",
        ["subtitle"] = "subtitles", ["attachment"] = "attachment",
    };

    
    
    
    
    public static (Dictionary<int, int> SiToTid, Dictionary<int, string> TidType) TidsFromProbe(ProbeResult? info)
    {
        var siToTid = new Dictionary<int, int>();
        var tidType = new Dictionary<int, string>();
        int tid = 0;
        foreach (var s in info?.Streams ?? new())
        {
            if (s.CodecType == "attachment") continue;
            siToTid[s.StreamIndex] = tid;
            tidType[tid] = MkvTypeMap.TryGetValue(s.CodecType, out var v) ? v : s.CodecType;
            tid++;
        }
        return (siToTid, tidType);
    }

    
    public static void ClassifyV1Streams(SessionContext ctx)
    {
        var st = new Dictionary<int, string>();
        foreach (var s in ctx.V1Info?.Streams ?? new())
            st[s.StreamIndex] = s.CodecType;
        ctx.V1StreamTypes = st;

        IEnumerable<int> source = ctx.V1StreamIndices is not null ? ctx.V1StreamIndices : st.Keys.OrderBy(k => k);
        var sel = source.ToList();
        ctx.V1VidSi = sel.Where(si => st.GetValueOrDefault(si) == "video").ToList();
        ctx.V1AudSi = sel.Where(si => st.GetValueOrDefault(si) == "audio").ToList();
        ctx.V1SubSi = sel.Where(si => st.GetValueOrDefault(si) == "subtitle").ToList();
        ctx.V1OtherSi = sel.Where(si =>
            !ctx.V1VidSi.Contains(si) && !ctx.V1AudSi.Contains(si) && !ctx.V1SubSi.Contains(si)).ToList();
        ctx.V1HasSubs = ctx.V1SubSi.Count > 0;
    }

    
    public static void ComputeV1Tids(SessionContext ctx)
    {
        var (siToTid, tidType) = TidsFromProbe(ctx.V1Info);
        var allTids = tidType.Keys.OrderBy(t => t).ToList();
        HashSet<int> selected;
        if (ctx.V1StreamIndices is not null)
        {
            selected = new HashSet<int>();
            foreach (var si in ctx.V1StreamIndices)
                if (siToTid.TryGetValue(si, out var t)) selected.Add(t);
        }
        else selected = new HashSet<int>(allTids);

        ctx.V1VidTids = allTids.Where(t => selected.Contains(t) && tidType.GetValueOrDefault(t) == "video").ToList();
        ctx.V1AudTids = allTids.Where(t => selected.Contains(t) && tidType.GetValueOrDefault(t) == "audio").ToList();
        ctx.V1SubTids = allTids.Where(t => selected.Contains(t) && tidType.GetValueOrDefault(t) == "subtitles").ToList();
        ctx.V1OtherTids = allTids.Where(t => selected.Contains(t) &&
            tidType.GetValueOrDefault(t) is not ("video" or "audio" or "subtitles")).ToList();
    }

    
    public static void ClassifyV2Streams(SessionContext ctx)
    {
        var st = new Dictionary<int, string>();
        foreach (var s in ctx.V2Info?.Streams ?? new())
            st[s.StreamIndex] = s.CodecType;
        ctx.V2StreamTypes = st;

        IEnumerable<int> source = ctx.V2StreamIndices is not null ? ctx.V2StreamIndices : st.Keys.OrderBy(k => k);
        var sel = source.ToList();
        ctx.V2AudSi = sel.Where(si => st.GetValueOrDefault(si) == "audio").ToList();
        ctx.V2SubSi = sel.Where(si => st.GetValueOrDefault(si) == "subtitle").ToList();

        var v2AllAudioSi = (ctx.V2Info?.Streams ?? new())
            .Where(s => s.CodecType == "audio").Select(s => s.StreamIndex).ToList();
        ctx.V2AudIndices = ctx.V2AudSi
            .Where(si => v2AllAudioSi.Contains(si))
            .Select(si => v2AllAudioSi.IndexOf(si)).ToList();
    }

    
    public static void ComputeV2Tids(SessionContext ctx)
    {
        if (!string.IsNullOrEmpty(ctx.TmpAudioPath))
            ctx.V2AudTids = Enumerable.Range(0, ctx.V2AudIndices.Count).ToList();
        else if (!string.IsNullOrEmpty(ctx.V2Path) && ctx.V2Streamcopy)
            ctx.V2AudTids = new List<int>(ctx.V2AudSi);
        else
            ctx.V2AudTids = new();

        ctx.V2SubTids = (ctx.V2SubSi.Count > 0 && !string.IsNullOrEmpty(ctx.V2Path))
            ? new List<int>(ctx.V2SubSi)
            : new();
    }

    
    public static void ComputeAudioOrdering(SessionContext ctx)
    {
        const int fileIdV2 = 1;
        ctx.AudioFt = new();
        foreach (var t in ctx.V1AudTids) ctx.AudioFt.Add((0, t));
        foreach (var t in ctx.V2AudTids) ctx.AudioFt.Add((fileIdV2, t));

        if (ctx.AudioOrder is not null && ctx.AudioOrder.Count == ctx.AudioFt.Count)
        {
            
            var ordered = new List<(int, int)>(ctx.AudioOrder.Count);
            foreach (var srcIdx in ctx.AudioOrder)
            {
                if (srcIdx < 0 || srcIdx >= ctx.AudioFt.Count)
                {
                    ordered = new List<(int, int)>(ctx.AudioFt);
                    break;
                }
                ordered.Add(ctx.AudioFt[srcIdx]);
            }
            ctx.AudioFtOrdered = ordered;
        }
        else
        {
            ctx.AudioFtOrdered = new List<(int, int)>(ctx.AudioFt);
        }

        ctx.DefaultAudioFt = null;
        if (ctx.DefaultAudioIndex.HasValue
            && ctx.DefaultAudioIndex.Value >= 0
            && ctx.DefaultAudioIndex.Value < ctx.AudioFtOrdered.Count)
        {
            ctx.DefaultAudioFt = ctx.AudioFtOrdered[ctx.DefaultAudioIndex.Value];
        }

        ctx.AudioSrcToMeta = new();
        if (ctx.AudioMetadata is not null
            && ctx.AudioOrder is not null
            && ctx.AudioOrder.Count == ctx.AudioFt.Count)
        {
            for (int outPos = 0; outPos < ctx.AudioOrder.Count; outPos++)
            {
                if (outPos < ctx.AudioMetadata.Count)
                {
                    int srcIdx = ctx.AudioOrder[outPos];
                    if (srcIdx >= 0 && srcIdx < ctx.AudioFt.Count)
                        ctx.AudioSrcToMeta[(srcIdx, 0)] = ctx.AudioMetadata[outPos];
                }
            }
        }
        else if (ctx.AudioMetadata is not null)
        {
            for (int srcIdx = 0; srcIdx < ctx.AudioFt.Count; srcIdx++)
            {
                if (srcIdx < ctx.AudioMetadata.Count)
                    ctx.AudioSrcToMeta[(srcIdx, 0)] = ctx.AudioMetadata[srcIdx];
            }
        }
    }

    
    public static AudioMetadata? AudioMetaForSrcIndex(SessionContext ctx, int srcIdx)
        => ctx.AudioSrcToMeta.TryGetValue((srcIdx, 0), out var m) ? m : null;

    
    
    
    
    public static List<string> AtempoChain(double atempo)
    {
        if (Math.Abs(atempo - 1.0) <= 0.0001) return new();
        if (atempo <= 0.01 || atempo > 200)
            throw new ArgumentOutOfRangeException(nameof(atempo), $"atempo out of range (0.01–200), got {atempo}");
        var parts = new List<string>();
        double remaining = atempo;
        for (int i = 0; i < 20 && remaining > 100.0; i++)
        {
            parts.Add("atempo=100.0");
            remaining /= 100.0;
        }
        for (int i = 0; i < 20 && remaining < 0.5; i++)
        {
            parts.Add("atempo=0.5");
            remaining /= 0.5;
        }
        parts.Add($"atempo={remaining.ToString("F6", CultureInfo.InvariantCulture)}");
        return parts;
    }

    public static string F6(double v) => v.ToString("F6", CultureInfo.InvariantCulture);
    public static string F3(double v) => v.ToString("F3", CultureInfo.InvariantCulture);
}
