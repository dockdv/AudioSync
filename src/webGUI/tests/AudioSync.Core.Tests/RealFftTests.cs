using AudioSync.Core.Sync;

namespace AudioSync.Core.Tests;

public class RealFftTests
{
    [Fact]
    public void Rfft_DcSignal_HasOnlyDcBin()
    {
        var sig = new double[64];
        Array.Fill(sig, 1.0);
        var bins = RealFft.Rfft(sig, 64);
        Assert.Equal(33, bins.Length); 
        Assert.Equal(64.0, bins[0].Real, 9);
        Assert.Equal(0.0, bins[0].Imaginary, 9);
        for (int i = 1; i < bins.Length; i++)
            Assert.True(bins[i].Magnitude < 1e-9, $"bin {i} = {bins[i]}");
    }

    [Fact]
    public void Rfft_Sine_PeaksAtExpectedBin()
    {
        const int n = 256;
        const int k = 8;
        var sig = new double[n];
        for (int i = 0; i < n; i++) sig[i] = Math.Sin(2 * Math.PI * k * i / n);
        var bins = RealFft.Rfft(sig, n);
        
        int peak = 0;
        double mx = 0;
        for (int i = 0; i < bins.Length; i++)
            if (bins[i].Magnitude > mx) { mx = bins[i].Magnitude; peak = i; }
        Assert.Equal(k, peak);
    }

    [Fact]
    public void IrfftRoundTrip_RecoversSignal()
    {
        const int n = 128;
        var rng = new Random(42);
        var sig = new double[n];
        for (int i = 0; i < n; i++) sig[i] = rng.NextDouble() * 2 - 1;
        var bins = RealFft.Rfft(sig, n);
        var back = RealFft.Irfft(bins, n);
        for (int i = 0; i < n; i++)
            Assert.Equal(sig[i], back[i], 9);
    }

    [Fact]
    public void RfftFreq_MatchesNumpyFormula()
    {
        var f = RealFft.RfftFreq(8, 1.0);
        Assert.Equal(new double[] { 0, 0.125, 0.25, 0.375, 0.5 }, f);
    }
}
