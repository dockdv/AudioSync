namespace AudioSync.Core.Tooling;

public interface IToolLocator
{
    /// <summary>Resolved path to ffmpeg, or null if not found.</summary>
    string? Ffmpeg { get; }
    /// <summary>Resolved path to ffprobe, or null if not found.</summary>
    string? Ffprobe { get; }
    /// <summary>Resolved path to mkvmerge, or null if not found.</summary>
    string? Mkvmerge { get; }
    /// <summary>Selected hwaccel method (cuda/vaapi/videotoolbox/qsv) or "none".</summary>
    string Hwaccel { get; }

    /// <summary>Get version strings: { ffmpeg: "x.y", ffprobe: "x.y", mkvmerge: "x.y" }.</summary>
    IReadOnlyDictionary<string, string> VersionInfo();
}
