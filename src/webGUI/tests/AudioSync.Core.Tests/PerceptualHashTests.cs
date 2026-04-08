using AudioSync.Core.Visual;

namespace AudioSync.Core.Tests;

public class PerceptualHashTests
{
    private static double[] SolidGray(int h, int w, double v)
    {
        var f = new double[h * w];
        Array.Fill(f, v);
        return f;
    }

    private static double[] HalfBlackHalfWhite(int h, int w)
    {
        var f = new double[h * w];
        for (int y = 0; y < h; y++)
            for (int x = 0; x < w; x++)
                f[y * w + x] = x < w / 2 ? 0.0 : 255.0;
        return f;
    }

    [Fact]
    public void PHash_IdenticalFrames_HammingZero()
    {
        var f = HalfBlackHalfWhite(120, 160);
        var h1 = PerceptualHash.PHash(f, 120, 160);
        var h2 = PerceptualHash.PHash(f, 120, 160);
        Assert.Equal(h1, h2);
        Assert.Equal(1.0, PerceptualHash.FrameSimilarity(f, 120, 160, f, 120, 160), 9);
    }

    [Fact]
    public void PHash_DifferentStructure_LowerSimilarity()
    {
        var solid = SolidGray(120, 160, 128);
        var split = HalfBlackHalfWhite(120, 160);
        var sim = PerceptualHash.FrameSimilarity(solid, 120, 160, split, 120, 160);
        Assert.True(sim < 0.9, $"expected structural mismatch, sim={sim}");
    }

    [Fact]
    public void Dct2_DcOnly_FromConstantBlock()
    {
        var block = new double[32, 32];
        for (int y = 0; y < 32; y++)
            for (int x = 0; x < 32; x++)
                block[y, x] = 5.0;
        var dct = PerceptualHash.Dct2(block);
        
        Assert.Equal(5.0 * 32 * 32, dct[0, 0], 6);
        for (int y = 0; y < 32; y++)
            for (int x = 0; x < 32; x++)
                if (y != 0 || x != 0)
                    Assert.True(Math.Abs(dct[y, x]) < 1e-6, $"dct[{y},{x}]={dct[y, x]}");
    }
}
