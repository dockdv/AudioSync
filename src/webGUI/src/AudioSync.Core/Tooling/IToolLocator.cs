namespace AudioSync.Core.Tooling;

public interface IToolLocator
{
    
    string? Ffmpeg { get; }
    
    string? Ffprobe { get; }
    
    string? Mkvmerge { get; }
    
    string Hwaccel { get; }

    
    IReadOnlyDictionary<string, string> VersionInfo();
}
