using AudioSync.Core.Sync;

namespace AudioSync.Core.Tests;

public class CrossCorrelationTests
{
    [Fact]
    public void DownsampleAudio_BlockMeanOfAbs()
    {
        
        var audio = new float[80 * 4];
        for (int i = 0; i < audio.Length; i++) audio[i] = (i % 2 == 0) ? 1.0f : -1.0f;
        var (ds, rate) = CrossCorrelation.DownsampleAudio(audio, 8000);
        Assert.Equal(4, ds.Length);
        Assert.Equal(100.0, rate);
        foreach (var v in ds) Assert.Equal(1.0, v, 6); 
    }

    [Fact]
    public void XcorrOnDownsampled_RecoversKnownOffset()
    {
        
        
        const double rate = 100;
        const int n = 800;
        const int shiftSamples = 50; 
        var rng = new Random(123);
        var d1 = new double[n];
        for (int i = 0; i < n; i++) d1[i] = rng.NextDouble();
        var d2 = new double[n + shiftSamples];
        Array.Copy(d1, 0, d2, shiftSamples, n);

        var x = CrossCorrelation.XcorrOnDownsampled(d1, d2, rate, new[] { 1.0 });
        
        
        Assert.InRange(x.Offset, -0.55, -0.45);
        Assert.Equal(1.0, x.Speed, 6);
    }

    [Fact]
    public void LinearResample_LinearRamp_Identity()
    {
        var src = new double[] { 0, 1, 2, 3, 4 };
        var r = CrossCorrelation.LinearResample(src, 9);
        
        for (int i = 0; i < 9; i++)
            Assert.Equal(i * 0.5, r[i], 9);
    }

    [Fact]
    public void FilterMatchesByOffset_KeepsOnlyInWindow()
    {
        var ts1 = new[] { 0.0, 1.0, 2.0, 3.0 };
        var ts2 = new[] { 0.0, 1.0, 2.0, 3.0 };
        var matches = new List<(int I, int J, double Sim)>
        {
            (0, 0, 1.0),  
            (1, 1, 1.0),  
            (2, 0, 1.0),  
            (0, 3, 1.0),  
        };
        var f = CrossCorrelation.FilterMatchesByOffset(matches, ts1, ts2, 0.0, 10.0, 1.0);
        Assert.Equal(4, f.Count);
        var f2 = CrossCorrelation.FilterMatchesByOffset(matches, ts1, ts2, 0.0, 1.0, 1.0);
        Assert.Equal(2, f2.Count); 
    }
}
