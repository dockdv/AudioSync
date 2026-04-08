using Microsoft.Extensions.Hosting;

namespace AudioSync.Core.Sessions;





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
            try { _store.Snapshot();  } catch { }
            try { await Task.Delay(_interval, stoppingToken).ConfigureAwait(false); }
            catch (OperationCanceledException) { return; }
        }
    }
}
