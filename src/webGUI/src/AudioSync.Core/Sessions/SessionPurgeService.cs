using Microsoft.Extensions.Hosting;

namespace AudioSync.Core.Sessions;

/// <summary>
/// Background loop that periodically asks SessionStore to purge stale sessions.
/// Mirror of the implicit Python purge done inside _new_session()/_serialize.
/// </summary>
public sealed class SessionPurgeService : BackgroundService
{
    private readonly SessionStore _store;
    private readonly TimeSpan _interval;

    public SessionPurgeService(SessionStore store, SessionStoreOptions? options = null)
    {
        _store = store;
        _interval = (options ?? new SessionStoreOptions()).PurgeInterval;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try { _store.Snapshot(); /* triggers purge */ } catch { }
            try { await Task.Delay(_interval, stoppingToken).ConfigureAwait(false); }
            catch (OperationCanceledException) { return; }
        }
    }
}
