using AudioSync.Core.Tooling;

namespace AudioSync.Core.Visual;

public sealed class CutDetector
{
    public const double MseThreshold = 500.0;

    private readonly FfLib _ff;
    public CutDetector(FfLib ff) { _ff = ff; }

    /// <summary>Convert raw uint8 grayscale bytes to a double[H*W] frame.</summary>
    public static double[] ToFrame(byte[] bytes)
    {
        var d = new double[bytes.Length];
        for (int i = 0; i < bytes.Length; i++) d[i] = bytes[i];
        return d;
    }

    public sealed record HardCutResult(bool IsCut, double[]? Frame, double[]? PrevFrame, double Mse);

    /// <summary>
    /// Mirror of visual._is_hard_cut, plus the prev frame is returned alongside
    /// the cut frame so callers can do prev↔prev verification without re-extracting.
    /// </summary>
    public async Task<HardCutResult> IsHardCutAsync(
        string path, double kfTime, double prevTime, int w, int h, CancellationToken ct = default)
    {
        var t1 = _ff.ExtractFrameAsync(path, kfTime, w, h, ct);
        var t2 = _ff.ExtractFrameAsync(path, prevTime, w, h, ct);
        var bKf = await t1.ConfigureAwait(false);
        var bPrev = await t2.ConfigureAwait(false);
        if (bKf is null || bPrev is null) return new HardCutResult(false, null, null, -1.0);
        double sum = 0;
        for (int i = 0; i < bKf.Length; i++)
        {
            double d = (double)bKf[i] - bPrev[i];
            sum += d * d;
        }
        double mse = sum / bKf.Length;
        return new HardCutResult(mse > MseThreshold, ToFrame(bKf), ToFrame(bPrev), mse);
    }

    /// <summary>
    /// Walk forward up to 50 keyframes looking for a hard cut. Returns the cut
    /// keyframe time, its frame, and the frame immediately before the cut.
    /// </summary>
    public async Task<(double? KfTime, double[]? Frame, double[]? PrevFrame)> FindHardCutFromAsync(
        IList<double> keyframes, int idx, string path, int w, int h,
        double frameInterval, CancellationToken ct = default)
    {
        int end = Math.Min(idx + 50, keyframes.Count);
        for (int i = idx; i < end; i++)
        {
            ct.ThrowIfCancellationRequested();
            double kf = keyframes[i];
            double prev = Math.Max(0, kf - frameInterval);
            var r = await IsHardCutAsync(path, kf, prev, w, h, ct).ConfigureAwait(false);
            if (!r.IsCut || r.Frame is null) continue;
            // Skip dark scenes (pHash unreliable)
            double mean = 0;
            for (int j = 0; j < r.Frame.Length; j++) mean += r.Frame[j];
            mean /= r.Frame.Length;
            if (mean < 20) continue;
            return (kf, r.Frame, r.PrevFrame);
        }
        return (null, null, null);
    }

    /// <summary>Mirror of visual._crop_letterbox — crop to wider aspect ratio.</summary>
    public static (double[] Cropped, int H, int W) CropLetterbox(
        double[] frame, int h, int w, double frameAr, double targetAr)
    {
        if (frameAr >= targetAr) return (frame, h, w);
        int newH = (int)(w / targetAr);
        int margin = (h - newH) / 2;
        var cropped = new double[newH * w];
        Array.Copy(frame, margin * w, cropped, 0, newH * w);
        return (cropped, newH, w);
    }
}
