using System.Globalization;
using System.Text.RegularExpressions;

namespace AudioSync.Core.Tooling;





public sealed class FfLib
{
    public const int FrameW = 160;
    public const int FrameH = 120;

    private static readonly Regex LufsSummaryRegex = new(
        @"Integrated loudness:\s*\n\s*I:\s+(-?\d+(?:\.\d+)?)\s+LUFS",
        RegexOptions.Compiled | RegexOptions.Multiline);

    private readonly IProcessRunner _runner;
    private readonly IToolLocator _locator;

    public FfLib(IProcessRunner runner, IToolLocator locator)
    {
        _runner = runner;
        _locator = locator;
    }

    private string Ffmpeg => _locator.Ffmpeg
        ?? throw new InvalidOperationException("ffmpeg not found");
    private string Ffprobe => _locator.Ffprobe
        ?? throw new InvalidOperationException("ffprobe not found");

    
    public static double ParseFrameRate(string? s)
    {
        if (string.IsNullOrEmpty(s) || s == "0/0") return 0.0;
        var parts = s.Split('/');
        if (parts.Length == 2 &&
            double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out var num) &&
            double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var den))
        {
            return den > 0 ? num / den : 0.0;
        }
        return double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var f) ? f : 0.0;
    }

    
    public async Task<string> ProbeJsonAsync(string handle, CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffprobe,
            Arguments = new[]
            {
                "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", handle,
            },
            Timeout = TimeSpan.FromSeconds(60),
        }, ct).ConfigureAwait(false);
        return System.Text.Encoding.UTF8.GetString(res.Stdout);
    }

    
    
    
    
    
    public async Task<HashSet<int>> AudioStreamsWithPacketsAsync(
        string handle, int maxSeconds = 60, CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffprobe,
            Arguments = new[]
            {
                "-v", "error",
                "-read_intervals", $"%+{maxSeconds}",
                "-select_streams", "a",
                "-show_entries", "packet=stream_index",
                "-of", "csv=p=0", handle,
            },
            Timeout = TimeSpan.FromSeconds(60),
        }, ct).ConfigureAwait(false);

        var present = new HashSet<int>();
        foreach (var raw in System.Text.Encoding.UTF8.GetString(res.Stdout).Split('\n'))
        {
            var line = raw.Trim().TrimEnd(',');
            if (line.Length == 0) continue;
            if (int.TryParse(line, out var idx)) present.Add(idx);
        }
        return present;
    }

    
    public async Task<double> GetDurationAsync(string handle, CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffprobe,
            Arguments = new[]
            {
                "-v", "quiet", "-print_format", "json",
                "-show_format", handle,
            },
            Timeout = TimeSpan.FromSeconds(30),
        }, ct).ConfigureAwait(false);
        var json = System.Text.Encoding.UTF8.GetString(res.Stdout);
        using var doc = System.Text.Json.JsonDocument.Parse(json);
        if (doc.RootElement.TryGetProperty("format", out var fmt) &&
            fmt.TryGetProperty("duration", out var d) &&
            double.TryParse(d.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var dur))
        {
            return dur;
        }
        return 0.0;
    }

    
    
    
    
    public async Task<double?> MeasureLufsAsync(
        string handle, int audioTrackIndex, CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffmpeg,
            Arguments = new[]
            {
                "-v", "info",
                "-i", handle,
                "-map", $"0:a:{audioTrackIndex}",
                "-af", "ebur128",
                "-f", "null", "-",
            },
            Timeout = TimeSpan.FromHours(1),
            DiscardStdout = true,
        }, ct).ConfigureAwait(false);
        var m = LufsSummaryRegex.Match(res.Stderr ?? string.Empty);
        if (m.Success && double.TryParse(m.Groups[1].Value, NumberStyles.Float,
                CultureInfo.InvariantCulture, out var lufs))
        {
            return lufs;
        }
        return null;
    }

    
    
    
    
    public async Task<(float[] Samples, string? Warnings)> DecodeAudioAsync(
        string handle, int audioTrackIndex, int targetSr,
        bool vocalFilter = false,
        Action<int>? progressCallback = null,
        double duration = 0,
        CancellationToken ct = default)
    {
        var args = new List<string>
        {
            "-v", "error",
            "-i", handle,
            "-map", $"0:a:{audioTrackIndex}",
        };
        if (vocalFilter)
        {
            args.Add("-af");
            args.Add($"aformat=channel_layouts=mono,bandreject=f=1000:width_type=h:w=2700,aresample={targetSr}");
        }
        else
        {
            args.Add("-ar");
            args.Add(targetSr.ToString(CultureInfo.InvariantCulture));
        }
        args.AddRange(new[]
        {
            "-ac", "1",
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "pipe:1",
        });

        if (progressCallback is null || duration <= 0)
        {
            var res = await _runner.RunAsync(new ProcessRunOptions
            {
                FileName = Ffmpeg,
                Arguments = args,
                Timeout = TimeSpan.FromHours(1),
            }, ct).ConfigureAwait(false);
            var samples = BytesToFloats(res.Stdout);
            return (samples, string.IsNullOrEmpty(res.Stderr) ? null : res.Stderr);
        }

        
        long expectedBytes = (long)(duration * targetSr * 4);
        long totalRead = 0;
        int lastPct = -1;
        using var ms = new MemoryStream();

        var streamRes = await _runner.RunStreamingAsync(new ProcessRunOptions
        {
            FileName = Ffmpeg,
            Arguments = args,
            Timeout = TimeSpan.FromHours(1),
        },
        async (chunk, c) =>
        {
            await ms.WriteAsync(chunk, c).ConfigureAwait(false);
            totalRead += chunk.Length;
            if (expectedBytes > 0)
            {
                int pct = Math.Min(99, (int)(totalRead * 100 / expectedBytes));
                if (pct != lastPct)
                {
                    progressCallback(pct);
                    lastPct = pct;
                }
            }
        }, ct).ConfigureAwait(false);

        var floats = BytesToFloats(ms.ToArray());
        return (floats, string.IsNullOrEmpty(streamRes.Stderr) ? null : streamRes.Stderr);
    }

    private static float[] BytesToFloats(byte[] bytes)
    {
        if (bytes.Length == 0) return Array.Empty<float>();
        int n = bytes.Length / 4;
        var samples = new float[n];
        Buffer.BlockCopy(bytes, 0, samples, 0, n * 4);
        return samples;
    }

    
    private static string TonemapVf(int width, int height) => $"format=gray,scale={width}:{height}";

    
    
    
    
    public async Task<byte[]?> ExtractFrameAsync(
        string handle, double timestamp,
        int width = FrameW, int height = FrameH,
        CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffmpeg,
            Arguments = new[]
            {
                "-v", "error",
                "-ss", timestamp.ToString("F3", CultureInfo.InvariantCulture),
                "-i", handle,
                "-vframes", "1",
                "-vf", TonemapVf(width, height),
                "-f", "rawvideo",
                "pipe:1",
            },
            Timeout = TimeSpan.FromSeconds(30),
        }, ct).ConfigureAwait(false);
        if (res.Stdout.Length != width * height) return null;
        return res.Stdout;
    }


    public async Task<double> GetVideoFrameRateAsync(string handle, CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffprobe,
            Arguments = new[]
            {
                "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "csv=p=0", handle,
            },
            Timeout = TimeSpan.FromSeconds(10),
        }, ct).ConfigureAwait(false);
        var s = System.Text.Encoding.UTF8.GetString(res.Stdout).Trim();
        return ParseFrameRate(s);
    }

    
    
    
    
    
    
    public async Task<List<(double Time, byte[] Frame)>> ExtractFrameSequenceAsync(
        string handle, double start, double duration,
        int width, int height, double fps,
        CancellationToken ct = default)
    {
        if (duration <= 0 || fps <= 0) return new();
        double rough = Math.Max(0, start - 3.0);
        double precise = start - rough;
        var args = new List<string>
        {
            "-v", "error",
            "-ss", rough.ToString("F3", CultureInfo.InvariantCulture),
            "-i", handle,
            "-ss", precise.ToString("F3", CultureInfo.InvariantCulture),
            "-t", duration.ToString("F3", CultureInfo.InvariantCulture),
            "-vf", $"format=gray,scale={width}:{height}",
            "-f", "rawvideo",
            "pipe:1",
        };
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffmpeg,
            Arguments = args,
            Timeout = TimeSpan.FromMinutes(2),
        }, ct).ConfigureAwait(false);

        int frameSize = width * height;
        int n = res.Stdout.Length / frameSize;
        var frames = new List<(double, byte[])>(n);
        for (int i = 0; i < n; i++)
        {
            double t = start + i / fps;
            var f = new byte[frameSize];
            Buffer.BlockCopy(res.Stdout, i * frameSize, f, 0, frameSize);
            frames.Add((t, f));
        }
        return frames;
    }

    
    public async Task<(int? Width, int? Height)> GetVideoResolutionAsync(
        string handle, CancellationToken ct = default)
    {
        var res = await _runner.RunAsync(new ProcessRunOptions
        {
            FileName = Ffprobe,
            Arguments = new[]
            {
                "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0", handle,
            },
            Timeout = TimeSpan.FromSeconds(10),
        }, ct).ConfigureAwait(false);

        var text = System.Text.Encoding.UTF8.GetString(res.Stdout).Trim();
        var parts = text.Split(',');
        if (parts.Length < 2) return (null, null);
        if (int.TryParse(parts[0], out var w) && int.TryParse(parts[1], out var h))
            return (w, h);
        return (null, null);
    }

    
    
    
    public async Task<Dictionary<int, List<double>>> ProbePacketsAsync(
        string handle,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default)
    {
        var packets = new Dictionary<int, List<double>>();
        long lineCount = 0;
        long lastReport = 0;
        var pending = new System.Text.StringBuilder();

        await _runner.RunStreamingAsync(new ProcessRunOptions
        {
            FileName = Ffprobe,
            Arguments = new[]
            {
                "-v", "quiet",
                "-print_format", "csv=p=0",
                "-show_entries", "packet=stream_index,dts_time",
                handle,
            },
            Timeout = TimeSpan.FromHours(2),
        },
        async (chunk, c) =>
        {
            pending.Append(System.Text.Encoding.UTF8.GetString(chunk.Span));
            while (true)
            {
                var nl = -1;
                for (int i = 0; i < pending.Length; i++)
                    if (pending[i] == '\n') { nl = i; break; }
                if (nl < 0) break;
                var line = pending.ToString(0, nl);
                pending.Remove(0, nl + 1);
                var parts = line.Trim().Split(',');
                if (parts.Length < 2) continue;
                if (!int.TryParse(parts[0], out var idx)) continue;
                if (!double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var dts)) continue;
                if (!packets.TryGetValue(idx, out var list))
                    packets[idx] = list = new List<double>();
                list.Add(dts);
                lineCount++;
            }
            if (progressCallback != null && lineCount - lastReport >= 50000)
            {
                progressCallback("progress", $"Reading packets... ({lineCount:N0} so far)");
                lastReport = lineCount;
            }
            await Task.CompletedTask;
        }, ct).ConfigureAwait(false);

        foreach (var list in packets.Values) list.Sort();
        return packets;
    }
}
