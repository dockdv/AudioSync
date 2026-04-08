using System.Text.Json;
using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;

namespace AudioSync.Web.Endpoints;

public static class MergeContextBuilder
{
    private static readonly JsonSerializerOptions SnakeOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals,
    };

    public sealed class BuildResult
    {
        public SessionContext? Ctx { get; init; }
        public string? Error { get; init; }
        public string? V1Path { get; init; }
        public string? V2Path { get; init; }
        public string? OutPath { get; init; }
    }

    public static BuildResult Build(SessionEntry sess, bool isRemux, double? durationLimit, string? outPathOverride)
    {
        var ui = sess.UiState;
        string? v1Path = GetString(ui, "v1_path");
        string? v2Path = isRemux ? null : GetString(ui, "v2_path");
        string? outPath = outPathOverride ?? GetString(ui, "out_path");

        if (string.IsNullOrEmpty(v1Path) || !File.Exists(v1Path))
            return new BuildResult { Error = $"V1 not found: {v1Path}" };
        if (!isRemux && (string.IsNullOrEmpty(v2Path) || !File.Exists(v2Path)))
            return new BuildResult { Error = $"V2 not found: {v2Path}" };
        if (string.IsNullOrEmpty(outPath))
            return new BuildResult { Error = "Output path is required" };

        var v1State = ReadVideoState(ui, "v1_state");
        var v2State = isRemux ? null : ReadVideoState(ui, "v2_state");
        if (v1State is null)
            return new BuildResult { Error = "V1 state missing" };
        if (!isRemux && v2State is null)
            return new BuildResult { Error = "V2 state missing" };

        var selV1 = ReadSelected(ui, "v1");
        var selV2 = isRemux ? new Dictionary<int, bool>() : ReadSelected(ui, "v2");
        var overrides = ReadTrackOverrides(ui);

        var v1StreamIndices = ComputeStreamIndices(v1State.Streams, selV1);
        var v2StreamIndices = isRemux ? null : ComputeAudioSubIndices(v2State!.Streams, selV2);

        var v1Audio = CollectMetadata(v1State.Streams, selV1, "audio", 1, overrides);
        var v1Sub = CollectMetadata(v1State.Streams, selV1, "subtitle", 1, overrides);
        var v1Vid = CollectMetadata(v1State.Streams, selV1, "video", 1, overrides);
        var v2Audio = isRemux ? new List<(string lang, string title)>() : CollectMetadata(v2State!.Streams, selV2, "audio", 2, overrides);
        var v2Sub = isRemux ? new List<(string lang, string title)>() : CollectMetadata(v2State!.Streams, selV2, "subtitle", 2, overrides);

        var allAudio = new List<(string lang, string title)>(v1Audio);
        allAudio.AddRange(v2Audio);

        int? defaultAudioIdx = null;
        if (ui.TryGetValue("default_audio_idx", out var diel) && diel.ValueKind == JsonValueKind.Number)
            defaultAudioIdx = diel.GetInt32();

        var (sortedAudio, audioOrder) = ReorderAudioTracks(allAudio, defaultAudioIdx);

        bool v1HasAttachments = HasAttachments(v1State.Streams, selV1);
        bool v2HasAttachments = !isRemux && HasAttachments(v2State!.Streams, selV2);

        var ctx = sess.Ctx;
        ctx.V1Path = v1Path;
        ctx.V2Path = isRemux ? null : v2Path;
        ctx.OutPath = outPath;
        ctx.V1Info = new ProbeResult { Streams = v1State.Streams, Audio = v1State.Tracks, Duration = v1State.Duration };
        ctx.V1Duration = v1State.Duration;
        ctx.V1StreamIndices = v1StreamIndices;
        ctx.V1VidMetadata = MapTrackMeta(v1Vid);
        ctx.V1SubMetadata = MapTrackMeta(v1Sub);
        ctx.V1HasAttachments = v1HasAttachments;
        ctx.AudioMetadata = MapAudioMeta(sortedAudio);
        ctx.AudioOrder = audioOrder;
        ctx.DefaultAudioIndex = 0;
        ctx.DurationLimit = durationLimit;

        if (isRemux)
        {
            ctx.V2Info = null;
            ctx.V2StreamIndices = null;
            ctx.V2SubMetadata = null;
            ctx.V2HasAttachments = false;
            ctx.Atempo = 1.0;
            ctx.Offset = 0;
            ctx.Segments = null;
            ctx.GainMatch = false;
            ctx.V1Lufs = null;
            ctx.V2Lufs = null;
        }
        else
        {
            ctx.V2Info = new ProbeResult { Streams = v2State!.Streams, Audio = v2State.Tracks, Duration = v2State.Duration };
            ctx.V2StreamIndices = v2StreamIndices;
            ctx.V2SubMetadata = MapTrackMeta(v2Sub);
            ctx.V2HasAttachments = v2HasAttachments;

            double atempo = ParseDouble(ui, "atempo", 1.0);
            double offset = ParseDouble(ui, "offset", 0.0);
            ctx.Atempo = atempo;
            ctx.Offset = offset;

            ctx.Segments = ReadSegments(ui);

            bool gainMatch = GetBool(ui, "gain_match");
            double? v1Lufs = GetDouble(ui, "v1_lufs");
            double? v2Lufs = GetDouble(ui, "v2_lufs");
            ctx.V1Lufs = v1Lufs;
            ctx.V2Lufs = v2Lufs;
            ctx.GainMatch = gainMatch && v1Lufs.HasValue && v2Lufs.HasValue;
        }

        return new BuildResult { Ctx = ctx, V1Path = v1Path, V2Path = v2Path, OutPath = outPath };
    }

    private sealed class VideoState
    {
        public List<StreamEntry> Streams { get; set; } = new();
        public List<AudioTrack> Tracks { get; set; } = new();
        public double Duration { get; set; }
    }

    private static VideoState? ReadVideoState(Dictionary<string, JsonElement> ui, string key)
    {
        if (!ui.TryGetValue(key, out var el) || el.ValueKind != JsonValueKind.Object) return null;
        var vs = new VideoState();
        if (el.TryGetProperty("streams", out var st) && st.ValueKind == JsonValueKind.Array)
            vs.Streams = JsonSerializer.Deserialize<List<StreamEntry>>(st.GetRawText(), SnakeOpts) ?? new();
        if (el.TryGetProperty("tracks", out var tr) && tr.ValueKind == JsonValueKind.Array)
            vs.Tracks = JsonSerializer.Deserialize<List<AudioTrack>>(tr.GetRawText(), SnakeOpts) ?? new();
        if (el.TryGetProperty("duration", out var du) && du.ValueKind == JsonValueKind.Number)
            vs.Duration = du.GetDouble();
        return vs;
    }

    private static Dictionary<int, bool> ReadSelected(Dictionary<string, JsonElement> ui, string slot)
    {
        var result = new Dictionary<int, bool>();
        if (!ui.TryGetValue("selected", out var sel) || sel.ValueKind != JsonValueKind.Object) return result;
        if (!sel.TryGetProperty(slot, out var inner) || inner.ValueKind != JsonValueKind.Object) return result;
        foreach (var prop in inner.EnumerateObject())
        {
            if (int.TryParse(prop.Name, out int k))
                result[k] = prop.Value.ValueKind == JsonValueKind.True
                    || (prop.Value.ValueKind == JsonValueKind.String && prop.Value.GetString() == "true");
        }
        return result;
    }

    private sealed class OverrideEntry { public string? Language { get; set; } public string? Title { get; set; } }

    private static Dictionary<string, OverrideEntry> ReadTrackOverrides(Dictionary<string, JsonElement> ui)
    {
        var d = new Dictionary<string, OverrideEntry>();
        if (!ui.TryGetValue("track_overrides", out var el) || el.ValueKind != JsonValueKind.Object) return d;
        foreach (var prop in el.EnumerateObject())
        {
            var oe = new OverrideEntry();
            if (prop.Value.ValueKind == JsonValueKind.Object)
            {
                if (prop.Value.TryGetProperty("language", out var lv) && lv.ValueKind == JsonValueKind.String) oe.Language = lv.GetString();
                if (prop.Value.TryGetProperty("title", out var tv) && tv.ValueKind == JsonValueKind.String) oe.Title = tv.GetString();
            }
            d[prop.Name] = oe;
        }
        return d;
    }

    private static List<int> ComputeStreamIndices(List<StreamEntry> streams, Dictionary<int, bool> sel)
    {
        var result = new List<int>();
        foreach (var kv in sel)
            if (kv.Value) result.Add(kv.Key);
        foreach (var s in streams)
            if (s.CodecType == "video" && !result.Contains(s.StreamIndex)) result.Add(s.StreamIndex);
        result.Sort();
        return result;
    }

    private static List<int> ComputeAudioSubIndices(List<StreamEntry> streams, Dictionary<int, bool> sel)
    {
        var result = new List<int>();
        foreach (var s in streams)
        {
            if ((s.CodecType == "audio" || s.CodecType == "subtitle") && sel.TryGetValue(s.StreamIndex, out bool v) && v)
                result.Add(s.StreamIndex);
        }
        result.Sort();
        return result;
    }

    private static List<(string lang, string title)> CollectMetadata(
        List<StreamEntry> streams, Dictionary<int, bool> sel, string codecType, int n, Dictionary<string, OverrideEntry> overrides)
    {
        var result = new List<(string, string)>();
        foreach (var s in streams)
        {
            if (s.CodecType != codecType) continue;
            if (!sel.TryGetValue(s.StreamIndex, out bool v) || !v) continue;
            var key = $"v{n}_s{s.StreamIndex}";
            overrides.TryGetValue(key, out var ovr);
            string lang = ovr?.Language ?? (string.IsNullOrEmpty(s.Language) ? "und" : s.Language);
            string title = ovr?.Title ?? s.Title ?? "";
            result.Add((lang, title));
        }
        return result;
    }

    private static bool HasAttachments(List<StreamEntry> streams, Dictionary<int, bool> sel)
    {
        foreach (var s in streams)
            if (s.CodecType == "attachment" && sel.TryGetValue(s.StreamIndex, out bool v) && v) return true;
        return false;
    }

    private static (List<(string lang, string title)> sorted, List<int> order) ReorderAudioTracks(
        List<(string lang, string title)> metadata, int? defaultIdx)
    {
        if (metadata.Count <= 1)
        {
            var ord = new List<int>();
            for (int i = 0; i < metadata.Count; i++) ord.Add(i);
            return (metadata, ord);
        }
        int defIdx = (defaultIdx.HasValue && defaultIdx.Value < metadata.Count) ? defaultIdx.Value : 0;
        string defLang = metadata[defIdx].lang;
        var defGroup = new List<((string lang, string title) meta, int orig)>();
        var rest = new List<((string lang, string title) meta, int orig)>();
        defGroup.Add((metadata[defIdx], defIdx));
        for (int i = 0; i < metadata.Count; i++)
        {
            if (i == defIdx) continue;
            if (metadata[i].lang == defLang) defGroup.Add((metadata[i], i));
            else rest.Add((metadata[i], i));
        }
        rest.Sort((a, b) => string.Compare(a.meta.lang, b.meta.lang, StringComparison.Ordinal));
        var combined = new List<((string lang, string title) meta, int orig)>(defGroup);
        combined.AddRange(rest);
        return (combined.Select(e => e.meta).ToList(), combined.Select(e => e.orig).ToList());
    }

    private static List<AudioMetadata>? MapAudioMeta(List<(string lang, string title)> src)
        => src.Select((m, i) => new AudioMetadata { Tid = i, Language = m.lang, Title = m.title }).ToList();

    private static List<TrackMetadata>? MapTrackMeta(List<(string lang, string title)> src)
        => src.Select((m, i) => new TrackMetadata { Tid = i, Language = m.lang, Title = m.title }).ToList();

    private static List<DetectedSegment>? ReadSegments(Dictionary<string, JsonElement> ui)
    {
        if (!ui.TryGetValue("segments", out var el) || el.ValueKind != JsonValueKind.Array) return null;
        var list = new List<DetectedSegment>();
        foreach (var s in el.EnumerateArray())
        {
            if (s.ValueKind != JsonValueKind.Object) continue;
            double v1Start = s.TryGetProperty("v1_start", out var a) ? a.GetDouble() : 0;
            double v1End = s.TryGetProperty("v1_end", out var b) ? b.GetDouble() : 0;
            double offset = s.TryGetProperty("offset", out var c) ? c.GetDouble() : 0;
            int nInliers = s.TryGetProperty("n_inliers", out var d) && d.ValueKind == JsonValueKind.Number ? d.GetInt32() : 0;
            list.Add(new DetectedSegment
            {
                V1Start = v1Start,
                V1End = v1End >= 1e9 ? double.PositiveInfinity : v1End,
                Offset = offset,
                NInliers = nInliers,
            });
        }
        return list.Count > 1 ? list : null;
    }

    private static string? GetString(Dictionary<string, JsonElement> ui, string k)
        => ui.TryGetValue(k, out var el) && el.ValueKind == JsonValueKind.String ? el.GetString() : null;

    private static bool GetBool(Dictionary<string, JsonElement> ui, string k)
        => ui.TryGetValue(k, out var el) && el.ValueKind == JsonValueKind.True;

    private static double? GetDouble(Dictionary<string, JsonElement> ui, string k)
    {
        if (!ui.TryGetValue(k, out var el)) return null;
        if (el.ValueKind == JsonValueKind.Number) return el.GetDouble();
        if (el.ValueKind == JsonValueKind.String && double.TryParse(el.GetString(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out double d)) return d;
        return null;
    }

    private static double ParseDouble(Dictionary<string, JsonElement> ui, string k, double def)
        => GetDouble(ui, k) ?? def;
}
