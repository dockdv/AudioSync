using AudioSync.Core.Sessions;

namespace AudioSync.Core.Sync;

public interface ISyncEngine
{
    Task<AlignmentResult> AutoAlignAudioAsync(
        SessionContext ctx,
        Action<string, string>? progressCallback = null,
        CancellationToken ct = default);
}
