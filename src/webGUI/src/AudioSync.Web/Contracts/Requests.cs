using AudioSync.Core.Probing;

namespace AudioSync.Web.Contracts;


public sealed record PathRequest(string? Path);
public sealed record FileExistsRequest(string? Path);
public sealed record ProbeRequest(string? Filepath, string? Sid = null, int? Slot = null);
public sealed record TestInterleaveRequest(string? Filepath);


public sealed record AlignRequest(
    string? V1Path, string? V2Path,
    int V1Track, int V2Track,
    bool VocalFilter, bool MeasureLufs, bool VisualRefine,
    List<StreamEntry>? V1Streams, List<AudioTrack>? V1Tracks, double V1Duration,
    List<StreamEntry>? V2Streams, List<AudioTrack>? V2Tracks, double V2Duration);


public sealed record MergeRequest(double? DurationLimit = null, string? OutPath = null);
