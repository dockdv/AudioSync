namespace AudioSync.Core.Visual;

/// <summary>
/// Visual fine-tuning interface used by the sync engine. Phase 5 implements this;
/// the sync engine accepts null when no visual matching is desired.
/// </summary>
public interface IVisualMatcher
{
    /// <summary>
    /// Mirror of visual.refine_offset_visual — returns refined offset in seconds, or null if rejected.
    /// </summary>
    Task<double?> RefineOffsetVisualAsync(
        string v1Path, string v2Path,
        double offset, double speed,
        double dur1, double dur2,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default);

    /// <summary>
    /// Mirror of visual.validate_segments_visual — returns true if segments should be collapsed.
    /// </summary>
    Task<bool> ValidateSegmentsVisualAsync(
        string v1Path, string v2Path,
        IList<Sync.DetectedSegment> segments,
        double coarseOffset, double speed,
        double dur1, double dur2,
        CancellationToken ct = default);

    /// <summary>Mirror of visual.refine_boundary_visual.</summary>
    Task<List<Sync.DetectedSegment>> RefineBoundaryVisualAsync(
        string v1Path, string v2Path,
        IList<Sync.DetectedSegment> segments, double speed,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default);
}
