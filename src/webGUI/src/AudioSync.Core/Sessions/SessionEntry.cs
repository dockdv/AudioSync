using System.Text.Json;
using AudioSync.Core.Tasks;

namespace AudioSync.Core.Sessions;

public sealed class LogEntry
{
    public long Idx { get; init; }
    public string Msg { get; init; } = "";
    public string Source { get; init; } = "server";
    public double Ts { get; init; }
}

/// <summary>
/// Mirror of one row in app.py _sessions dict — wraps SessionContext with
/// task tracking, log ring, label, version counter, and TTL bookkeeping.
/// All mutating operations must be performed under SessionStore's lock.
/// </summary>
public sealed class SessionEntry
{
    public string Id { get; init; } = "";
    public DateTimeOffset CreatedWall { get; init; } = DateTimeOffset.UtcNow;
    public long CreatedAtTicks { get; init; }     // Stopwatch ticks for monotonic age
    public long UpdatedAtTicks { get; set; }
    public string Label { get; set; } = "New session";
    public SessionContext Ctx { get; init; } = new();
    public Dictionary<string, BackgroundJob> Tasks { get; } = new();
    public string? ActiveTask { get; set; }
    public Dictionary<string, JsonElement> UiState { get; set; } = new();
    public long Version { get; set; }
    public List<LogEntry> Log { get; } = new();
    public long LogIdx { get; set; }
}
