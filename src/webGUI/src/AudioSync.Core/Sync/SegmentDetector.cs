namespace AudioSync.Core.Sync;

public sealed class DetectedSegment
{
    public double V1Start { get; set; }
    public double V1End { get; set; }
    public double Offset { get; set; }
    public int NInliers { get; set; }
}

/// <summary>Mirror of audio.detect_segments — sliding-window xcorr scan + clustering.</summary>
public static class SegmentDetector
{
    public static List<DetectedSegment> Detect(
        IList<(double T1, double T2, double Sim)> inlierPairs, double a, double coarseOffset = 0.0,
        double[]? d1 = null, double[]? d2 = null, double effectiveRate = 100.0,
        double minSegmentSec = 60)
    {
        var primary = new DetectedSegment
        {
            V1Start = 0.0,
            V1End = double.PositiveInfinity,
            Offset = coarseOffset,
            NInliers = inlierPairs.Count,
        };
        if (d1 == null || d2 == null) return new List<DetectedSegment> { primary };

        double er = effectiveRate;
        double v1Dur = (double)d1.Length / er;
        if (v1Dur < minSegmentSec * 2) return new List<DetectedSegment> { primary };

        double windowSec = 300.0;
        double stepSec = 60.0;
        double paddingSec = 300.0;
        int minWindowSamples = (int)(er * 60);

        var scanResults = new List<(double Center, double Off, double Corr)>();
        double v1Pos = 0.0;
        while (v1Pos + windowSec <= v1Dur)
        {
            int d1S = (int)(v1Pos * er);
            int d1E = (int)((v1Pos + windowSec) * er);
            int d1Len = Math.Min(d1.Length, d1E) - d1S;
            if (d1Len < minWindowSamples) { v1Pos += stepSec; continue; }
            var d1W = new double[d1Len];
            Array.Copy(d1, d1S, d1W, 0, d1Len);

            double v1Center = v1Pos + windowSec / 2;
            double v2Est = (v1Center - coarseOffset) / a;
            int v2S = Math.Max(0, (int)((v2Est - windowSec / 2 - paddingSec) * er));
            int v2E = Math.Min(d2.Length, (int)((v2Est + windowSec / 2 + paddingSec) * er));
            int d2Len = v2E - v2S;
            if (d2Len < minWindowSamples) { v1Pos += stepSec; continue; }
            var d2W = new double[d2Len];
            Array.Copy(d2, v2S, d2W, 0, d2Len);

            var x = CrossCorrelation.XcorrOnDownsampled(d1W, d2W, er, AudioConstants.SpeedCandidates);
            if (x.Correlation < 0.3 || Math.Abs(x.Speed - a) / Math.Max(a, 1e-9) > 0.005)
            {
                v1Pos += stepSec; continue;
            }
            double v2Abs = v2S / er;
            double absOff = v1Pos + x.Offset - v2Abs * x.Speed;
            scanResults.Add((v1Center, absOff, x.Correlation));
            v1Pos += stepSec;
        }

        if (scanResults.Count < 2) return new List<DetectedSegment> { primary };

        double offsetThreshold = 10.0;
        int minClusterWindows = 3;
        var clusters = new List<List<(double Center, double Off, double Corr)>>();
        var current = new List<(double, double, double)> { scanResults[0] };
        for (int i = 1; i < scanResults.Count; i++)
        {
            double curMedian = Median(current.Select(r => r.Item2));
            if (Math.Abs(scanResults[i].Off - curMedian) > offsetThreshold)
            {
                clusters.Add(current);
                current = new List<(double, double, double)> { scanResults[i] };
            }
            else current.Add(scanResults[i]);
        }
        clusters.Add(current);

        if (clusters.Count > 1)
        {
            var merged = new List<List<(double, double, double)>>();
            foreach (var cl in clusters)
            {
                if (cl.Count < minClusterWindows && merged.Count > 0)
                    merged[^1].AddRange(cl);
                else
                    merged.Add(cl);
            }
            while (merged.Count > 1 && merged[^1].Count < minClusterWindows)
            {
                merged[^2].AddRange(merged[^1]);
                merged.RemoveAt(merged.Count - 1);
            }
            var finalClusters = new List<List<(double, double, double)>> { merged[0] };
            for (int i = 1; i < merged.Count; i++)
            {
                double prevMed = Median(finalClusters[^1].Select(r => r.Item2));
                double curMed = Median(merged[i].Select(r => r.Item2));
                if (Math.Abs(curMed - prevMed) <= offsetThreshold)
                    finalClusters[^1].AddRange(merged[i]);
                else
                    finalClusters.Add(merged[i]);
            }
            clusters = finalClusters;
        }
        if (clusters.Count <= 1) return new List<DetectedSegment> { primary };

        var rawSegments = new List<(DetectedSegment Seg, double LastCenter)>();
        for (int ci = 0; ci < clusters.Count; ci++)
        {
            var cluster = clusters[ci];
            double medOff = Median(cluster.Select(r => r.Item2));
            double v1Start = cluster[0].Item1 - windowSec / 2;
            double v1End = cluster[^1].Item1 + windowSec / 2;

            if (ci > 0)
            {
                var prev = rawSegments[^1];
                double prevOff = prev.Seg.Offset;
                double coarseBoundary = (prev.LastCenter + cluster[0].Item1) / 2;
                double refLo = Math.Max(0, coarseBoundary - 120);
                double refHi = Math.Min(v1Dur, coarseBoundary + 120);
                double refStep = 5.0;
                double refWin = 30.0;
                double lastPrev = refLo;
                double firstCur = refHi;

                double t = refLo;
                while (t + refWin <= refHi)
                {
                    int d1s = (int)(t * er);
                    int d1e = (int)((t + refWin) * er);
                    int n_out = Math.Min(d1.Length, d1e) - d1s;
                    if (n_out < (int)(er * 10)) { t += refStep; continue; }
                    var d1r = new double[n_out];
                    Array.Copy(d1, d1s, d1r, 0, n_out);

                    double bestCorr = -1.0;
                    double bestTestOff = prevOff;
                    foreach (var testOff in new[] { prevOff, medOff })
                    {
                        double v2c = (t + refWin / 2 - testOff) / a;
                        int v2s = Math.Max(0, (int)((v2c - refWin / 2) * er));
                        int v2e = Math.Min(d2.Length, (int)((v2c + refWin / 2) * er));
                        int d2len = v2e - v2s;
                        if (d2len < (int)(er * 10)) continue;
                        var d2r = new double[d2len];
                        Array.Copy(d2, v2s, d2r, 0, d2len);
                        var d2i = CrossCorrelation.LinearResample(d2r, n_out);
                        double c = Pearson(d1r, d2i);
                        if (double.IsNaN(c)) c = -1.0;
                        if (c > bestCorr) { bestCorr = c; bestTestOff = testOff; }
                    }
                    if (bestCorr > 0.1)
                    {
                        if (Math.Abs(bestTestOff - prevOff) < Math.Abs(bestTestOff - medOff))
                            lastPrev = Math.Max(lastPrev, t);
                        else
                            firstCur = Math.Min(firstCur, t);
                    }
                    t += refStep;
                }
                double boundary = (lastPrev + refWin + firstCur) / 2;
                if (boundary < refLo) boundary = refLo;
                if (boundary > refHi) boundary = refHi;
                prev.Seg.V1End = boundary;
                v1Start = boundary;
            }

            int segInliers = 0;
            foreach (var p in inlierPairs)
                if (p.T1 >= v1Start && p.T1 < v1End) segInliers++;

            rawSegments.Add((new DetectedSegment
            {
                V1Start = v1Start, V1End = v1End,
                Offset = medOff, NInliers = segInliers,
            }, cluster[^1].Item1));
        }

        var merged2 = new List<DetectedSegment> { rawSegments[0].Seg };
        for (int i = 1; i < rawSegments.Count; i++)
        {
            var seg = rawSegments[i].Seg;
            double dur = seg.V1End - seg.V1Start;
            if (dur < minSegmentSec)
            {
                merged2[^1].V1End = seg.V1End;
                merged2[^1].NInliers += seg.NInliers;
            }
            else merged2.Add(seg);
        }
        while (merged2.Count > 1)
        {
            double firstDur = merged2[0].V1End - merged2[0].V1Start;
            if (firstDur < minSegmentSec)
            {
                merged2[1].V1Start = merged2[0].V1Start;
                merged2[1].NInliers += merged2[0].NInliers;
                merged2.RemoveAt(0);
            }
            else break;
        }
        merged2[0].V1Start = 0.0;
        merged2[^1].V1End = double.PositiveInfinity;
        return merged2;
    }

    private static double Median(IEnumerable<double> values)
    {
        var arr = values.ToArray();
        Array.Sort(arr);
        int n = arr.Length;
        if (n == 0) return 0;
        if ((n & 1) == 1) return arr[n / 2];
        return (arr[n / 2 - 1] + arr[n / 2]) / 2;
    }

    private static double Pearson(double[] a, double[] b)
    {
        int n = Math.Min(a.Length, b.Length);
        if (n < 2) return double.NaN;
        double sa = 0, sb = 0;
        for (int i = 0; i < n; i++) { sa += a[i]; sb += b[i]; }
        double ma = sa / n, mb = sb / n;
        double cov = 0, va = 0, vb = 0;
        for (int i = 0; i < n; i++)
        {
            double da = a[i] - ma, db = b[i] - mb;
            cov += da * db;
            va += da * da;
            vb += db * db;
        }
        if (va <= 0 || vb <= 0) return double.NaN;
        return cov / Math.Sqrt(va * vb);
    }
}
