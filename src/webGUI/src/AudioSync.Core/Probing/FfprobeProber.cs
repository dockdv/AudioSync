using System.Globalization;
using System.Text.Json;
using AudioSync.Core.Tooling;

namespace AudioSync.Core.Probing;

/// <summary>
/// Mirror of fflib.probe() + probe.probe_full() — parses ffprobe JSON into typed
/// MediaInfo records, filters phantom audio tracks (declared but no packets).
/// </summary>
public sealed class FfprobeProber : IMediaProber
{
    private readonly FfLib _ff;

    public FfprobeProber(FfLib ff) { _ff = ff; }

    public async Task<ProbeResult> ProbeAsync(string filepath, CancellationToken ct = default)
    {
        var json = await _ff.ProbeJsonAsync(filepath, ct).ConfigureAwait(false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var streamsEl = root.TryGetProperty("streams", out var s) ? s : default;

        // Declared audio indices
        var declaredAudio = new HashSet<int>();
        if (streamsEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var st in streamsEl.EnumerateArray())
            {
                if (st.TryGetProperty("codec_type", out var ct1) && ct1.GetString() == "audio")
                {
                    declaredAudio.Add(st.TryGetProperty("index", out var ix) ? ix.GetInt32() : 0);
                }
            }
        }

        HashSet<int> presentAudio;
        if (declaredAudio.Count == 0)
        {
            presentAudio = new HashSet<int>();
        }
        else
        {
            try
            {
                presentAudio = await _ff.AudioStreamsWithPacketsAsync(filepath, 60, ct).ConfigureAwait(false);
            }
            catch
            {
                presentAudio = new HashSet<int>(declaredAudio);
            }
        }
        // Defensive: if probe returned nothing for a file that does declare audio,
        // fall back to trusting the header rather than dropping every track.
        if (declaredAudio.Count > 0 && presentAudio.Count == 0)
            presentAudio = new HashSet<int>(declaredAudio);

        var result = new ProbeResult();
        int audioIdx = 0;

        if (streamsEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var stEl in streamsEl.EnumerateArray())
            {
                var codecType = stEl.TryGetProperty("codec_type", out var ct2) ? (ct2.GetString() ?? "unknown") : "unknown";
                var disp = stEl.TryGetProperty("disposition", out var d) ? d : default;
                if (codecType == "video" && disp.ValueKind == JsonValueKind.Object &&
                    disp.TryGetProperty("attached_pic", out var ap) && ap.GetInt32() != 0)
                {
                    codecType = "attachment";
                }

                int streamIndex = stEl.TryGetProperty("index", out var ix2) ? ix2.GetInt32() : 0;
                var tags = stEl.TryGetProperty("tags", out var t) && t.ValueKind == JsonValueKind.Object ? t : default;
                string langRaw = tags.ValueKind == JsonValueKind.Object && tags.TryGetProperty("language", out var lg)
                    ? (lg.GetString() ?? "und") : "und";
                string title = tags.ValueKind == JsonValueKind.Object && tags.TryGetProperty("title", out var ti)
                    ? (ti.GetString() ?? "") : "";
                string codec = stEl.TryGetProperty("codec_name", out var cn) ? (cn.GetString() ?? "?") : "?";
                double startTime = ReadDouble(stEl, "start_time");
                string language = Languages.Normalize3(langRaw);

                if (codecType == "audio")
                {
                    int channels = ReadInt(stEl, "channels");
                    int sampleRate = ReadInt(stEl, "sample_rate");
                    if (!presentAudio.Contains(streamIndex))
                    {
                        result.Streams.Add(new StreamEntry
                        {
                            StreamIndex = streamIndex,
                            CodecType = codecType,
                            Codec = codec,
                            Language = language,
                            Title = title,
                            StartTime = startTime,
                            Empty = true,
                            Channels = channels,
                            SampleRate = sampleRate,
                        });
                        continue;
                    }
                    long bitRate = ReadLong(stEl, "bit_rate");
                    result.Audio.Add(new AudioTrack
                    {
                        Index = audioIdx,
                        StreamIndex = streamIndex,
                        Codec = codec,
                        Channels = channels,
                        SampleRate = sampleRate,
                        BitRate = bitRate,
                        Language = language,
                        Title = title,
                        StartTime = startTime,
                    });
                    result.Streams.Add(new StreamEntry
                    {
                        StreamIndex = streamIndex,
                        CodecType = codecType,
                        Codec = codec,
                        Language = language,
                        Title = title,
                        StartTime = startTime,
                        AudioIndex = audioIdx,
                        Channels = channels,
                        SampleRate = sampleRate,
                    });
                    audioIdx++;
                }
                else if (codecType == "video")
                {
                    int w = ReadInt(stEl, "width");
                    int h = ReadInt(stEl, "height");
                    string r = stEl.TryGetProperty("r_frame_rate", out var rf) ? (rf.GetString() ?? "0/1") : "0/1";
                    result.Streams.Add(new StreamEntry
                    {
                        StreamIndex = streamIndex,
                        CodecType = codecType,
                        Codec = codec,
                        Language = language,
                        Title = title,
                        StartTime = startTime,
                        Width = w,
                        Height = h,
                        FrameRate = FfLib.ParseFrameRate(r),
                    });
                }
                else if (codecType == "subtitle")
                {
                    result.Streams.Add(new StreamEntry
                    {
                        StreamIndex = streamIndex,
                        CodecType = codecType,
                        Codec = codec,
                        Language = language,
                        Title = title,
                        StartTime = startTime,
                        SubtitleCodec = codec,
                    });
                }
                else
                {
                    result.Streams.Add(new StreamEntry
                    {
                        StreamIndex = streamIndex,
                        CodecType = codecType,
                        Codec = codec,
                        Language = language,
                        Title = title,
                        StartTime = startTime,
                    });
                }
            }
        }

        if (root.TryGetProperty("format", out var fmt) && fmt.TryGetProperty("duration", out var du)
            && double.TryParse(du.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var dur))
        {
            return new ProbeResult
            {
                Audio = result.Audio,
                Streams = result.Streams,
                Duration = dur,
            };
        }
        return result;
    }

    public async Task<FullProbeResult> ProbeFullAsync(string filepath, CancellationToken ct = default)
    {
        try
        {
            var info = await ProbeAsync(filepath, ct).ConfigureAwait(false);
            var emptyIdxs = info.Streams.Where(s => s.Empty).Select(s => s.StreamIndex).ToList();
            string warning = "";
            if (emptyIdxs.Count > 0)
                warning = "Skipped empty audio track(s) with no packets: " +
                          string.Join(", ", emptyIdxs.Select(i => $"#{i}"));
            if (info.Audio.Count == 0 && warning.Length == 0)
                warning = "No audio streams found";

            return new FullProbeResult
            {
                Tracks = info.Audio,
                Streams = info.Streams,
                Duration = info.Duration,
                Method = "libav",
                Error = "",
                Warning = warning,
            };
        }
        catch (Exception ex)
        {
            return new FullProbeResult
            {
                Tracks = new(),
                Streams = new(),
                Duration = 0,
                Method = "none",
                Error = $"libAV probe error: {ex.Message}",
                Warning = "",
            };
        }
    }

    public async Task<double> GetDurationAsync(string filepath, CancellationToken ct = default)
    {
        try { return await _ff.GetDurationAsync(filepath, ct).ConfigureAwait(false); }
        catch { return 0.0; }
    }

    public async Task<int> GetAudioSampleRateAsync(string filepath, int trackIndex = 0, CancellationToken ct = default)
    {
        try
        {
            var info = await ProbeAsync(filepath, ct).ConfigureAwait(false);
            if (trackIndex < info.Audio.Count)
            {
                var sr = info.Audio[trackIndex].SampleRate;
                return sr > 0 ? sr : 48000;
            }
            return 48000;
        }
        catch { return 48000; }
    }

    private static int ReadInt(JsonElement el, string name)
    {
        if (!el.TryGetProperty(name, out var v)) return 0;
        if (v.ValueKind == JsonValueKind.Number) return v.TryGetInt32(out var i) ? i : 0;
        if (v.ValueKind == JsonValueKind.String && int.TryParse(v.GetString(), out var s)) return s;
        return 0;
    }
    private static long ReadLong(JsonElement el, string name)
    {
        if (!el.TryGetProperty(name, out var v)) return 0;
        if (v.ValueKind == JsonValueKind.Number) return v.TryGetInt64(out var i) ? i : 0;
        if (v.ValueKind == JsonValueKind.String && long.TryParse(v.GetString(), out var s)) return s;
        return 0;
    }
    private static double ReadDouble(JsonElement el, string name)
    {
        if (!el.TryGetProperty(name, out var v)) return 0;
        if (v.ValueKind == JsonValueKind.Number) return v.GetDouble();
        if (v.ValueKind == JsonValueKind.String &&
            double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d)) return d;
        return 0;
    }
}
