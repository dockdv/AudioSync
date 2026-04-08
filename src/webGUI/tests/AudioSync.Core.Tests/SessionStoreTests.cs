using AudioSync.Core.Sessions;
using AudioSync.Core.Tasks;

namespace AudioSync.Core.Tests;

public class SessionStoreTests
{
    [Fact]
    public void NewSession_AssignsIdAndStoresEntry()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        Assert.Equal(16, sid.Length);
        Assert.NotNull(s.Get(sid));
    }

    [Fact]
    public void StartTask_RejectsSecondConcurrentTask()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        var (j1, e1) = s.StartTask(sid, "align", null);
        Assert.NotNull(j1);
        Assert.Null(e1);

        var (j2, e2) = s.StartTask(sid, "merge", null);
        Assert.Null(j2);
        Assert.NotNull(e2);
    }

    [Fact]
    public void UpdateTask_TerminalStateClearsActive_AndBumpsVersion()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        var sess = s.Get(sid)!;
        long versionBefore = sess.Version;

        var (job, _) = s.StartTask(sid, "align", null);
        Assert.NotNull(sess.ActiveTask);

        s.UpdateTask(sid, job!.Id, status: JobStatus.Done, result: "done");
        Assert.Null(sess.ActiveTask);
        Assert.Equal(JobStatus.Done, sess.Tasks[job.Id].Status);
        Assert.True(sess.Version > versionBefore);
    }

    [Fact]
    public void CancelTask_SignalsCancellationToken()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        var (job, _) = s.StartTask(sid, "merge", null);
        Assert.False(job!.Cancel.IsCancellationRequested);
        Assert.True(s.CancelTask(sid, job.Id));
        Assert.True(job.Cancel.IsCancellationRequested);
    }

    [Fact]
    public void DeleteSession_CancelsRunningTasksAndRemoves()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        var (job, _) = s.StartTask(sid, "align", null);
        Assert.True(s.DeleteSession(sid));
        Assert.Null(s.Get(sid));
        Assert.True(job!.Cancel.IsCancellationRequested);
    }

    [Fact]
    public void EnsureTaskFinished_MarksOrphanedRunningAsError()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        var (job, _) = s.StartTask(sid, "align", null);
        // task thread "exits" without ever calling UpdateTask
        s.EnsureTaskFinished(sid, job!.Id);
        Assert.Equal(JobStatus.Error, job.Status);
        Assert.Equal("Task died unexpectedly", job.Error);
    }

    [Fact]
    public void AppendLog_RingBuffersAt1000()
    {
        var s = new SessionStore();
        var sid = s.NewSession();
        for (int i = 0; i < 1500; i++) s.AppendLog(sid, $"msg-{i}");
        var sess = s.Get(sid)!;
        Assert.Equal(1000, sess.Log.Count);
        Assert.Equal(1500, sess.LogIdx);
        Assert.Equal("msg-1499", sess.Log[^1].Msg);
    }
}
