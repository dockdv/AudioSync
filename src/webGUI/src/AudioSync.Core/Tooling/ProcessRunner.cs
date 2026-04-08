using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;

namespace AudioSync.Core.Tooling;





public sealed class ProcessRunner : IProcessRunner
{
    private static readonly Regex TimeRegex =
        new(@"time=(\d+):(\d+):(\d+)\.(\d+)", RegexOptions.Compiled);

    public async Task<ProcessRunResult> RunAsync(ProcessRunOptions options, CancellationToken ct = default)
    {
        using var proc = StartProcess(options, redirectStdout: !options.DiscardStdout);
        var stderrLines = new List<string>();
        var stdoutBuf = new MemoryStream();

        var useProgress = options.ProgressCallback != null && options.Duration > 0;

        Task readStdoutTask = Task.CompletedTask;
        if (!options.DiscardStdout)
        {
            readStdoutTask = proc.StandardOutput.BaseStream.CopyToAsync(stdoutBuf, ct);
        }

        Task readStderrTask;
        if (useProgress)
        {
            readStderrTask = ReadStderrWithProgress(proc, stderrLines, options, ct);
        }
        else
        {
            readStderrTask = ReadStderrSimple(proc, stderrLines, options, ct);
        }

        try
        {
            using var timeoutCts = new CancellationTokenSource();
            if (options.Timeout.HasValue) timeoutCts.CancelAfter(options.Timeout.Value);
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, timeoutCts.Token);
            await proc.WaitForExitAsync(linked.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            try { if (!proc.HasExited) proc.Kill(entireProcessTree: true); } catch { }
            try { proc.WaitForExit(5000); } catch { }
            if (ct.IsCancellationRequested) throw new CancelledException();
            throw new TimeoutException($"{options.FileName} timed out");
        }

        try { await Task.WhenAll(readStdoutTask, readStderrTask).ConfigureAwait(false); } catch { }

        var stderrStr = string.Join("\n", stderrLines).Trim();

        if (proc.ExitCode != 0)
        {
            var tail = stderrLines.Count > 20
                ? string.Join("\n", stderrLines.GetRange(stderrLines.Count - 20, 20))
                : stderrStr;
            throw new InvalidOperationException(
                $"{options.FileName} failed (code {proc.ExitCode}):\n{tail}");
        }

        return new ProcessRunResult
        {
            ExitCode = proc.ExitCode,
            Stdout = options.DiscardStdout ? Array.Empty<byte>() : stdoutBuf.ToArray(),
            Stderr = stderrStr,
        };
    }

    public async Task<ProcessRunResult> RunStreamingAsync(
        ProcessRunOptions options,
        Func<ReadOnlyMemory<byte>, CancellationToken, Task> onStdoutChunk,
        CancellationToken ct = default)
    {
        using var proc = StartProcess(options, redirectStdout: true);
        var stderrLines = new List<string>();

        var stdoutTask = Task.Run(async () =>
        {
            var buf = new byte[65536];
            var stream = proc.StandardOutput.BaseStream;
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                int n = await stream.ReadAsync(buf.AsMemory(0, buf.Length), ct).ConfigureAwait(false);
                if (n <= 0) break;
                await onStdoutChunk(new ReadOnlyMemory<byte>(buf, 0, n), ct).ConfigureAwait(false);
            }
        }, ct);

        var stderrTask = ReadStderrSimple(proc, stderrLines, options, ct);

        try
        {
            using var timeoutCts = new CancellationTokenSource();
            if (options.Timeout.HasValue) timeoutCts.CancelAfter(options.Timeout.Value);
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, timeoutCts.Token);
            await proc.WaitForExitAsync(linked.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            try { if (!proc.HasExited) proc.Kill(entireProcessTree: true); } catch { }
            try { proc.WaitForExit(5000); } catch { }
            if (ct.IsCancellationRequested) throw new CancelledException();
            throw new TimeoutException($"{options.FileName} timed out");
        }

        try { await Task.WhenAll(stdoutTask, stderrTask).ConfigureAwait(false); } catch { }

        var stderrStr = string.Join("\n", stderrLines).Trim();
        if (proc.ExitCode != 0)
            throw new InvalidOperationException(
                $"{options.FileName} failed (code {proc.ExitCode}): {stderrStr}");

        return new ProcessRunResult
        {
            ExitCode = proc.ExitCode,
            Stdout = Array.Empty<byte>(),
            Stderr = stderrStr,
        };
    }

    private static Process StartProcess(ProcessRunOptions options, bool redirectStdout)
    {
        var psi = new ProcessStartInfo
        {
            FileName = options.FileName,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = redirectStdout,
            RedirectStandardError = true,
        };
        foreach (var a in options.Arguments) psi.ArgumentList.Add(a);
        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        if (!proc.Start())
            throw new InvalidOperationException($"Failed to start {options.FileName}");
        return proc;
    }

    private static async Task ReadStderrSimple(
        Process proc, List<string> lines, ProcessRunOptions options, CancellationToken ct)
    {
        try
        {
            using var sr = proc.StandardError;
            string? line;
            while ((line = await sr.ReadLineAsync(ct).ConfigureAwait(false)) != null)
            {
                lock (lines) lines.Add(line);
                options.StderrLineCallback?.Invoke(line);
            }
        }
        catch {  }
    }

    private static async Task ReadStderrWithProgress(
        Process proc, List<string> lines, ProcessRunOptions options, CancellationToken ct)
    {
        var sb = new StringBuilder();
        var sr = proc.StandardError;
        var buf = new char[1];
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            int n = await sr.ReadAsync(buf, 0, 1).ConfigureAwait(false);
            if (n <= 0) break;
            char ch = buf[0];
            if (ch == '\r' || ch == '\n')
            {
                if (sb.Length == 0) continue;
                var line = sb.ToString();
                sb.Clear();
                lock (lines) lines.Add(line);
                options.StderrLineCallback?.Invoke(line);

                var matches = TimeRegex.Matches(line);
                if (matches.Count > 0)
                {
                    var m = matches[^1];
                    int h = int.Parse(m.Groups[1].Value);
                    int mi = int.Parse(m.Groups[2].Value);
                    int s = int.Parse(m.Groups[3].Value);
                    string fracStr = m.Groups[4].Value;
                    double pos = h * 3600 + mi * 60 + s
                                 + int.Parse(fracStr) / Math.Pow(10, fracStr.Length);
                    int pct = Math.Min(99, (int)(pos / options.Duration * 100));
                    options.ProgressCallback?.Invoke("progress", $"{options.ProgressPrefix}:{pct}");
                }
            }
            else
            {
                sb.Append(ch);
            }
        }
    }
}
