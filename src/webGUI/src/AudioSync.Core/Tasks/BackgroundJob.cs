namespace AudioSync.Core.Tasks;

public enum JobStatus { Running, Done, Cancelled, Error }





public sealed class BackgroundJob
{
    public string Id { get; init; } = "";
    public string Type { get; init; } = ""; 
    public JobStatus Status { get; set; } = JobStatus.Running;
    public string Progress { get; set; } = "";
    
    public int Percent { get; set; } = -1;
    public object? Result { get; set; }
    public string? Error { get; set; }
    public IReadOnlyDictionary<string, object?> Params { get; init; } =
        new Dictionary<string, object?>();
    public CancellationTokenSource Cancel { get; } = new();
    public DateTimeOffset StartedAt { get; } = DateTimeOffset.UtcNow;
    public DateTimeOffset? FinishedAt { get; set; }
}
