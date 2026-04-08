using AudioSync.Core.Sync;

namespace AudioSync.Core.Tests;

public class RansacTests
{
    [Fact]
    public void LinearFit_RecoversSlopeAndInterceptFromCleanData()
    {
        
        const double a = 1.001, b = 2.5;
        var t2 = Enumerable.Range(0, 200).Select(i => i * 0.5).ToArray();
        var t1 = t2.Select(t => a * t + b).ToArray();

        var r = Ransac.LinearFit(t1, t2, nIter: 1000, threshold: 0.1, seed: 7, ct: TestContext.Current.CancellationToken);
        Assert.Equal(a, r.A, 5);
        Assert.Equal(b, r.B, 5);
        Assert.Equal(t1.Length, r.Inliers);
    }

    [Fact]
    public void LinearFit_RejectsOutliers()
    {
        const double a = 1.0, b = 0.0;
        var rng = new Random(11);
        var t2 = Enumerable.Range(0, 200).Select(i => (double)i).ToArray();
        var t1 = t2.Select(t => a * t + b).ToArray();
        
        for (int k = 0; k < 30; k++)
        {
            int i = rng.Next(t1.Length);
            t1[i] += (rng.NextDouble() - 0.5) * 50.0;
        }
        var r = Ransac.LinearFit(t1, t2, nIter: 3000, threshold: 0.5, seed: 13, ct: TestContext.Current.CancellationToken);
        Assert.InRange(r.A, 0.99, 1.01);
        Assert.InRange(r.B, -0.5, 0.5);
        Assert.True(r.Inliers >= 160, $"expected ≥160 inliers, got {r.Inliers}");
    }

    [Fact]
    public void SnapSpeedToCandidate_SnapsWithinTolerance()
    {
        
        double a = 25.0 / 24.0 + 0.0005;
        var t1 = new[] { 0.0, 1.0 };
        var t2 = new[] { 0.0, 0.96 };
        var (snappedA, _) = Ransac.SnapSpeedToCandidate(a, t1, t2);
        Assert.Equal(25.0 / 24.0, snappedA, 9);
    }

    [Fact]
    public void SnapSpeedToCandidate_NoSnapWhenOutsideTolerance()
    {
        
        double a = 1.5;
        var t1 = new[] { 0.0, 1.0 };
        var t2 = new[] { 0.0, 0.5 };
        var (snappedA, _) = Ransac.SnapSpeedToCandidate(a, t1, t2);
        Assert.Equal(1.5, snappedA, 9);
    }

    [Fact]
    public void ResidualStats_MeanMaxEnd()
    {
        var pairs = new List<(double T1, double T2, double Sim)>
        {
            (1.0, 1.0, 0), 
            (3.0, 2.0, 0), 
            (5.0, 3.0, 0), 
        };
        var (mean, max, end) = Ransac.ResidualStats(pairs, 1.0, 0.0);
        Assert.Equal(1.0, mean, 9);
        Assert.Equal(2.0, max, 9);
        Assert.Equal(2.0, end, 9);
    }
}
