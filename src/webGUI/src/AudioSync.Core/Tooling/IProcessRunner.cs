namespace AudioSync.Core.Tooling;

public sealed class ProcessRunResult
{
    public int ExitCode { get; init; }
    public byte[] Stdout { get; init; } = Array.Empty<byte>();
    public string Stderr { get; init; } = string.Empty;
}

public sealed class ProcessRunOptions
{
    public string FileName { get; init; } = "";
    public IReadOnlyList<string> Arguments { get; init; } = Array.Empty<string>();
    public TimeSpan? Timeout { get; init; }
    public bool DiscardStdout { get; init; }
    public bool CaptureStderr { get; init; } = true;
    /// <summary>Optional progress callback. Receives raw stderr lines.</summary>
    public Action<string>? StderrLineCallback { get; init; }
    /// <summary>Optional duration in seconds for time= progress parsing.</summary>
    public double Duration { get; init; }
    /// <summary>Optional progress callback (kind, payload).</summary>
    public Action<string, string>? ProgressCallback { get; init; }
    public string ProgressPrefix { get; init; } = "mux";
}

public sealed class CancelledException : Exception
{
    public CancelledException(string message = "Cancelled") : base(message) { }
}

public interface IProcessRunner
{
    Task<ProcessRunResult> RunAsync(ProcessRunOptions options, CancellationToken ct = default);

    /// <summary>
    /// Run a process streaming stdout to a callback. Used for piped audio decoding.
    /// </summary>
    Task<ProcessRunResult> RunStreamingAsync(
        ProcessRunOptions options,
        Func<ReadOnlyMemory<byte>, CancellationToken, Task> onStdoutChunk,
        CancellationToken ct = default);
}
