namespace AudioSync.Core.Sync;

/// <summary>Mirror of sync_engine.build_align_result return shape.</summary>
public sealed class AlignmentResult
{
    public double SpeedRatio { get; init; }
    public double Offset { get; init; }
    public double LinearA { get; init; }
    public double LinearB { get; init; }
    public int InlierCount { get; init; }
    public int TotalCandidates { get; init; }
    public List<(double T1, double T2, double Sim)> InlierPairs { get; init; } = new();
    public (double Lo, double Hi) V1Coverage { get; init; }
    public (double Lo, double Hi) V2Coverage { get; init; }
    public double V1Interval { get; init; }
    public double V2Interval { get; init; }
    public string Mode { get; init; } = "";
    public (int T1, int T2) SyncTracks { get; init; }
    public double ResidualMean { get; init; }
    public double ResidualMax { get; init; }
    public double ResidualEnd { get; init; }
    public double CoarseOffset { get; init; }
    /// <summary>RANSAC-refined offset before any visual fine-tune was applied.</summary>
    public double? RansacOffset { get; init; }
    public List<DetectedSegment>? Segments { get; init; }
    public List<string> Warnings { get; init; } = new();
    public double AudioOffset { get; init; }
    public double AudioSpeed { get; init; }
    public double? V1Lufs { get; init; }
    public double? V2Lufs { get; init; }
    public double V2StartDelay { get; init; }
    public double V1Fps { get; init; }
    public double V2Fps { get; init; }
    public bool FpsAdjusted { get; init; }
    public double? VisualRefinedOffset { get; init; }
}
