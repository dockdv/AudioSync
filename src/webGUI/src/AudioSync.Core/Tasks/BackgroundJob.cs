namespace AudioSync.Core.Tasks;

public enum JobStatus { Running, Done, Cancelled, Error }

/// <summary>
/// Mirror of app.py task dict — one row in a session's tasks map.
/// CancellationTokenSource replaces Python CancellableTask.
/// </summary>
public sealed class BackgroundJob
{
    public string Id { get; init; } = "";
    public string Type { get; init; } = ""; // align|merge|remux|test-interleave
    public JobStatus Status { get; set; } = JobStatus.Running;
    public string Progress { get; set; } = "";
    /// <summary>Server-computed overall progress 0..100. -1 means indeterminate.</summary>
    public int Percent { get; set; } = -1;
    public object? Result { get; set; }
    public string? Error { get; set; }
    public IReadOnlyDictionary<string, object?> Params { get; init; } =
        new Dictionary<string, object?>();
    public CancellationTokenSource Cancel { get; } = new();
    public DateTimeOffset StartedAt { get; } = DateTimeOffset.UtcNow;
    public DateTimeOffset? FinishedAt { get; set; }
}
