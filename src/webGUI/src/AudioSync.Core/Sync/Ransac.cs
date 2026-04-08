using MathNet.Numerics.LinearAlgebra;

namespace AudioSync.Core.Sync;

public sealed class RansacResult
{
    public double A { get; init; } = 1.0;
    public double B { get; init; }
    public bool[] Mask { get; init; } = Array.Empty<bool>();
    public int Inliers { get; init; }
}

/// <summary>Mirror of audio.py RANSAC + residual + speed-snap helpers.</summary>
public static class Ransac
{
    /// <summary>
    /// Mirror of audio.ransac_linear_fit. Fits t1 = a·t2 + b. Edge-biased pair sampling.
    /// Optional seed for parity testing — Python default is non-deterministic.
    /// </summary>
    public static RansacResult LinearFit(double[] t1, double[] t2,
        int nIter = AudioConstants.AudioRansacIterations,
        double threshold = AudioConstants.AudioRansacThresholdSec,
        int? seed = null,
        CancellationToken ct = default)
    {
        int n = t1.Length;
        if (n < 2)
            return new RansacResult { A = 1.0, B = 0.0, Mask = Enumerable.Repeat(true, n).ToArray(), Inliers = n };

        var rng = seed.HasValue ? new Random(seed.Value) : Random.Shared;

        double ba = 1.0, bb = 0.0;
        int bn = 0;
        var bm = new bool[n];
        double t2Min = double.MaxValue, t2Max = double.MinValue;
        for (int i = 0; i < n; i++) { if (t2[i] < t2Min) t2Min = t2[i]; if (t2[i] > t2Max) t2Max = t2[i]; }
        double t2Range = t2Max - t2Min;

        // Quartile index buffers (allocated once)
        var q1 = new List<int>();
        var q4 = new List<int>();
        if (n > 20)
        {
            for (int i = 0; i < n; i++)
            {
                if (t2[i] <= t2Min + t2Range * 0.3) q1.Add(i);
                if (t2[i] >= t2Max - t2Range * 0.3) q4.Add(i);
            }
        }

        for (int it = 0; it < nIter; it++)
        {
            if ((it & 0xFF) == 0) ct.ThrowIfCancellationRequested();
            int i1, i2;
            if (it % 3 == 0 && n > 20 && q1.Count > 0 && q4.Count > 0)
            {
                i1 = q1[rng.Next(q1.Count)];
                i2 = q4[rng.Next(q4.Count)];
            }
            else
            {
                i1 = rng.Next(n);
                do { i2 = rng.Next(n); } while (i2 == i1);
            }
            double dt = t2[i2] - t2[i1];
            if (Math.Abs(dt) < 1e-9) continue;
            double a = (t1[i2] - t1[i1]) / dt;
            double b = t1[i1] - a * t2[i1];
            if (a < 0.5 || a > 2.0) continue;
            int c = 0;
            for (int k = 0; k < n; k++)
                if (Math.Abs(t1[k] - (a * t2[k] + b)) < threshold) c++;
            if (c > bn)
            {
                bn = c;
                ba = a;
                bb = b;
                for (int k = 0; k < n; k++)
                    bm[k] = Math.Abs(t1[k] - (a * t2[k] + b)) < threshold;
            }
        }

        if (bn >= 2)
        {
            (ba, bb) = LeastSquaresFit(t1, t2, bm, bn);
            for (int pass = 0; pass < 3; pass++)
            {
                int rc = 0;
                var refined = new bool[n];
                for (int k = 0; k < n; k++)
                {
                    if (Math.Abs(t1[k] - (ba * t2[k] + bb)) < threshold) { refined[k] = true; rc++; }
                }
                if (rc > bn)
                {
                    (ba, bb) = LeastSquaresFit(t1, t2, refined, rc);
                    bn = rc;
                    bm = refined;
                }
                else break;
            }
        }
        return new RansacResult { A = ba, B = bb, Mask = bm, Inliers = bn };
    }

    private static (double A, double B) LeastSquaresFit(double[] t1, double[] t2, bool[] mask, int count)
    {
        // Solve [t2 1] · [a;b] = t1 for masked rows.
        var A = Matrix<double>.Build.Dense(count, 2);
        var y = Vector<double>.Build.Dense(count);
        int row = 0;
        for (int i = 0; i < t1.Length; i++)
        {
            if (!mask[i]) continue;
            A[row, 0] = t2[i];
            A[row, 1] = 1.0;
            y[row] = t1[i];
            row++;
        }
        var sol = A.Svd(true).Solve(y);
        return (sol[0], sol[1]);
    }

    /// <summary>Mirror of audio.residual_stats — (mean, max, last).</summary>
    public static (double Mean, double Max, double End) ResidualStats(
        IList<(double T1, double T2, double Sim)> pairs, double a, double b)
    {
        if (pairs.Count == 0) return (0, 0, 0);
        double sum = 0, max = 0, last = 0;
        for (int i = 0; i < pairs.Count; i++)
        {
            double r = Math.Abs(pairs[i].T1 - (a * pairs[i].T2 + b));
            sum += r;
            if (r > max) max = r;
            last = r;
        }
        return (sum / pairs.Count, max, last);
    }

    /// <summary>Mirror of audio.snap_speed_to_candidate.</summary>
    public static (double A, double B) SnapSpeedToCandidate(
        double a, double[] t1Inliers, double[] t2Inliers)
    {
        double bestDist = double.PositiveInfinity;
        double bestCandidate = double.NaN;
        foreach (var sc in AudioConstants.SpeedCandidates)
        {
            double dist = Math.Abs(a - sc) / sc;
            if (dist < bestDist) { bestDist = dist; bestCandidate = sc; }
        }
        if (bestDist > AudioConstants.SpeedSnapTolerance || double.IsNaN(bestCandidate))
        {
            double meanB = 0;
            int len = Math.Min(t1Inliers.Length, t2Inliers.Length);
            for (int i = 0; i < len; i++) meanB += t1Inliers[i] - a * t2Inliers[i];
            return (a, len > 0 ? meanB / len : 0);
        }
        double meanBs = 0;
        int len2 = Math.Min(t1Inliers.Length, t2Inliers.Length);
        for (int i = 0; i < len2; i++) meanBs += t1Inliers[i] - bestCandidate * t2Inliers[i];
        return (bestCandidate, len2 > 0 ? meanBs / len2 : 0);
    }
}
