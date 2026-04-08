using System.Diagnostics;
using System.Runtime.InteropServices;

namespace AudioSync.Core.Tooling;

public sealed class ToolLocatorOptions
{
    public string? FfmpegPath { get; set; }
    public string? FfprobePath { get; set; }
    public string? MkvmergePath { get; set; }
}





public sealed class ToolLocator : IToolLocator
{
    private static readonly string[] HwaccelPriority = { "cuda", "vaapi", "videotoolbox", "qsv" };

    public string? Ffmpeg { get; }
    public string? Ffprobe { get; }
    public string? Mkvmerge { get; }
    public string Hwaccel { get; }

    public ToolLocator(ToolLocatorOptions? options = null)
    {
        options ??= new ToolLocatorOptions();
        Ffmpeg = ResolveBinary("ffmpeg", "FFMPEG_PATH", options.FfmpegPath, "ffmpeg-lib");
        Ffprobe = ResolveBinary("ffprobe", "FFPROBE_PATH", options.FfprobePath, "ffmpeg-lib");
        Mkvmerge = ResolveBinary("mkvmerge", "MKVMERGE_PATH", options.MkvmergePath, "mkvtoolnix-lib");
        Hwaccel = DetectHwaccel(Ffmpeg) ?? "none";
    }

    private static string? ResolveBinary(string name, string envKey, string? configured, string sidecarDir)
    {
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured)) return configured;

        var envVal = Environment.GetEnvironmentVariable(envKey);
        if (!string.IsNullOrWhiteSpace(envVal) && File.Exists(envVal)) return envVal;

        var scriptDir = AppContext.BaseDirectory;
        var baseDir = Path.GetFullPath(Path.Combine(scriptDir, "..", ".."));
        var arch = RuntimeInformation.OSArchitecture switch
        {
            Architecture.X64 => "x64",
            Architecture.Arm64 => "arm64",
            _ => "x64",
        };
        var plat = RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? "win" : "linux";
        var suffixes = RuntimeInformation.IsOSPlatform(OSPlatform.Windows)
            ? new[] { name + ".exe", name }
            : new[] { name };

        var dirs = new[]
        {
            scriptDir,
            Path.Combine(baseDir, sidecarDir, plat, arch),
            Path.Combine(baseDir, sidecarDir, arch),
        };

        foreach (var d in dirs)
        foreach (var s in suffixes)
        {
            var p = Path.Combine(d, s);
            if (File.Exists(p)) return Path.GetFullPath(p);
        }

        return WhichOnPath(name);
    }

    private static string? WhichOnPath(string name)
    {
        var path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path)) return null;
        var sep = RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? ';' : ':';
        var suffixes = RuntimeInformation.IsOSPlatform(OSPlatform.Windows)
            ? new[] { name + ".exe", name }
            : new[] { name };
        foreach (var dir in path.Split(sep, StringSplitOptions.RemoveEmptyEntries))
        foreach (var s in suffixes)
        {
            var p = Path.Combine(dir, s);
            if (File.Exists(p)) return p;
        }
        return null;
    }

    private static string? DetectHwaccel(string? ffmpeg)
    {
        if (ffmpeg is null) return null;
        var hwtest = Path.Combine(AppContext.BaseDirectory, "assets", "hwtest.mp4");
        if (!File.Exists(hwtest)) return null;

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = ffmpeg,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add("-hwaccels");
            using var p = Process.Start(psi)!;
            if (!p.WaitForExit(10000)) { try { p.Kill(); } catch { } return null; }
            var raw = p.StandardOutput.ReadToEnd();
            var available = new HashSet<string>();
            bool capture = false;
            foreach (var lineRaw in raw.Split('\n'))
            {
                var line = lineRaw.Trim();
                if (line.StartsWith("Hardware acceleration methods:")) { capture = true; continue; }
                if (capture && line.Length > 0) available.Add(line);
            }

            foreach (var method in HwaccelPriority)
            {
                if (!available.Contains(method)) continue;
                var probe = BuildProbeArgs(method, hwtest);
                if (probe is null) continue;
                var psi2 = new ProcessStartInfo
                {
                    FileName = ffmpeg,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                psi2.ArgumentList.Add("-v"); psi2.ArgumentList.Add("error");
                foreach (var a in probe) psi2.ArgumentList.Add(a);
                using var p2 = Process.Start(psi2)!;
                if (!p2.WaitForExit(5000)) { try { p2.Kill(); } catch { } continue; }
                if (p2.ExitCode == 0) return method;
            }
        }
        catch { }
        return null;
    }

    private static string[]? BuildProbeArgs(string method, string hwtest) => method switch
    {
        "cuda" => new[] { "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                          "-i", hwtest, "-vframes", "1", "-f", "null", "-" },
        "vaapi" => new[] { "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                           "-vaapi_device", "/dev/dri/renderD128",
                           "-i", hwtest, "-vframes", "1", "-f", "null", "-" },
        "videotoolbox" => new[] { "-hwaccel", "videotoolbox",
                                  "-hwaccel_output_format", "videotoolbox_vld",
                                  "-i", hwtest, "-vframes", "1", "-f", "null", "-" },
        "qsv" => new[] { "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
                         "-i", hwtest, "-vframes", "1", "-f", "null", "-" },
        _ => null,
    };

    public IReadOnlyList<string> HwaccelFlags()
    {
        if (Hwaccel == "none") return Array.Empty<string>();
        var flags = new List<string> { "-hwaccel", Hwaccel };
        if (Hwaccel == "vaapi") { flags.Add("-vaapi_device"); flags.Add("/dev/dri/renderD128"); }
        return flags;
    }

    private IReadOnlyDictionary<string, string>? _versionCache;
    public IReadOnlyDictionary<string, string> VersionInfo()
    {
        if (_versionCache is not null) return _versionCache;
        var result = new Dictionary<string, string>();
        foreach (var (name, path) in new[] { ("ffmpeg", Ffmpeg), ("ffprobe", Ffprobe), ("mkvmerge", Mkvmerge) })
        {
            if (path is null) continue;
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = path,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                psi.ArgumentList.Add(name == "mkvmerge" ? "--version" : "-version");
                using var p = Process.Start(psi)!;
                if (!p.WaitForExit(10000)) { try { p.Kill(); } catch { } continue; }
                var firstLine = p.StandardOutput.ReadToEnd().Split('\n')[0];
                var parts = firstLine.Split(' ');
                
                
                int verIdx = name == "mkvmerge" ? 1 : 2;
                result[name] = parts.Length > verIdx ? parts[verIdx] : firstLine.Trim();
            }
            catch { }
        }
        _versionCache = result;
        return result;
    }
}
