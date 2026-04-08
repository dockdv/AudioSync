using AudioSync.Core.Sync;

namespace AudioSync.Core.Tests;

public class FingerprintsTests
{
    [Fact]
    public void BuildMelFilterbank_ShapeAndNonNegative()
    {
        var fb = Fingerprints.BuildMelFilterbank(nFft: 2049, sr: 8000, nMels: 128);
        Assert.Equal(128, fb.Length);
        foreach (var row in fb)
        {
            Assert.Equal(2049, row.Length);
            foreach (var v in row) Assert.True(v >= 0);
        }
    }

    [Fact]
    public void Match_FindsExactSelfMatchAtTopK()
    {
        var rng = new Random(99);
        var fp = new double[10][];
        for (int i = 0; i < 10; i++)
        {
            fp[i] = new double[16];
            double n = 0;
            for (int j = 0; j < 16; j++) { fp[i][j] = rng.NextDouble(); n += fp[i][j] * fp[i][j]; }
            n = Math.Sqrt(n);
            for (int j = 0; j < 16; j++) fp[i][j] /= n;
        }
        var matches = Fingerprints.Match(fp, fp, topK: 3);
        
        for (int i = 0; i < 10; i++)
        {
            var top = matches.Where(m => m.I == i).OrderByDescending(m => m.Sim).First();
            Assert.Equal(i, top.J);
            Assert.Equal(1.0, top.Sim, 6);
        }
    }

    [Fact]
    public void MutualNearestNeighbors_DropsAsymmetric()
    {
        
        var matches = new List<(int I, int J, double Sim)>
        {
            (0, 0, 0.9),
            (1, 0, 0.95), 
            (1, 1, 0.5),
        };
        var f = Fingerprints.MutualNearestNeighbors(matches, n1: 2, n2: 2, topK: 1);
        
        Assert.Contains((1, 0, 0.95), f);
        Assert.DoesNotContain((0, 0, 0.9), f);
    }

    [Fact]
    public void ExtractMel_ProducesNormalizedFingerprintsFromSineWave()
    {
        const int sr = 8000;
        const double durSec = 4.0;
        var audio = new float[(int)(sr * durSec)];
        for (int i = 0; i < audio.Length; i++)
            audio[i] = (float)Math.Sin(2 * Math.PI * 440.0 * i / sr);

        var r = Fingerprints.ExtractMel(audio, sr, maxSamples: 100,
            hopSec: 0.2, windowSec: 0.5, nMels: 32,
            ct: TestContext.Current.CancellationToken);
        Assert.True(r.Fingerprints.Length >= 10);
        
        foreach (var fp in r.Fingerprints)
        {
            double n = 0;
            foreach (var v in fp) n += v * v;
            Assert.Equal(1.0, Math.Sqrt(n), 5);
        }
    }
}
