using AudioSync.Core.Sessions;

namespace AudioSync.Core.Merging;

public interface IMerger
{
    Task MergeAsync(
        SessionContext ctx,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default);
}
