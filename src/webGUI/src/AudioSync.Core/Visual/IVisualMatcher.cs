namespace AudioSync.Core.Visual;





public interface IVisualMatcher
{
    
    
    
    Task<double?> RefineOffsetVisualAsync(
        string v1Path, string v2Path,
        double offset, double speed,
        double dur1, double dur2,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default);

    
    
    
    Task<bool> ValidateSegmentsVisualAsync(
        string v1Path, string v2Path,
        IList<Sync.DetectedSegment> segments,
        double coarseOffset, double speed,
        double dur1, double dur2,
        CancellationToken ct = default);

    
    Task<List<Sync.DetectedSegment>> RefineBoundaryVisualAsync(
        string v1Path, string v2Path,
        IList<Sync.DetectedSegment> segments, double speed,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default);
}
