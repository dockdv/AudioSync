namespace AudioSync.Core.Probing;

public interface IMediaProber
{
    Task<ProbeResult> ProbeAsync(string filepath, CancellationToken ct = default);
    Task<FullProbeResult> ProbeFullAsync(string filepath, CancellationToken ct = default);
    Task<double> GetDurationAsync(string filepath, CancellationToken ct = default);
    Task<int> GetAudioSampleRateAsync(string filepath, int trackIndex = 0, CancellationToken ct = default);
}
