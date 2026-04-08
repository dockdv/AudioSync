using AudioSync.Core.Probing;

namespace AudioSync.Web.Contracts;

// ===== generic =====
public sealed record PathRequest(string? Path);
public sealed record FileExistsRequest(string? Path);
public sealed record ProbeRequest(string? Filepath);
public sealed record TestInterleaveRequest(string? Filepath);

// ===== sessions =====
public sealed record LogPostRequest(List<string>? Messages);

// ===== align =====
public sealed record AlignRequest(
    string? V1Path, string? V2Path,
    int V1Track, int V2Track,
    bool VocalFilter, bool MeasureLufs,
    List<StreamEntry>? V1Streams, List<AudioTrack>? V1Tracks, double V1Duration,
    List<StreamEntry>? V2Streams, List<AudioTrack>? V2Tracks, double V2Duration);

// ===== merge =====
public sealed record SegmentDto(
    double V1Start, double V1End, double Offset, int NInliers);

public sealed record AudioMetaDto(string? Language, string? Title);
public sealed record TrackMetaDto(string? Language, string? Title);

public sealed record MergeRequest(
    string? V1Path, string? V2Path, string? OutPath,
    double? Atempo, double? Offset,
    List<SegmentDto>? Segments,
    double? V1Lufs, double? V2Lufs,
    List<int>? V1StreamIndices, List<int>? V2StreamIndices,
    double V1Duration,
    List<AudioMetaDto>? Metadata,
    List<TrackMetaDto>? SubMetadata,
    List<TrackMetaDto>? V2SubMetadata,
    List<TrackMetaDto>? V1VidMetadata,
    double? DurationLimit,
    int? DefaultAudio,
    List<int>? AudioOrder,
    bool GainMatch,
    bool V1HasAttachments,
    bool V2HasAttachments,
    List<StreamEntry>? V1Streams, List<AudioTrack>? V1Tracks,
    List<StreamEntry>? V2Streams, List<AudioTrack>? V2Tracks);

public sealed record RemuxRequest(
    string? V1Path, string? OutPath,
    List<int>? V1StreamIndices,
    double V1Duration,
    List<AudioMetaDto>? Metadata,
    List<TrackMetaDto>? SubMetadata,
    List<TrackMetaDto>? V1VidMetadata,
    double? DurationLimit,
    int? DefaultAudio,
    List<int>? AudioOrder,
    bool V1HasAttachments,
    List<StreamEntry>? V1Streams, List<AudioTrack>? V1Tracks);
