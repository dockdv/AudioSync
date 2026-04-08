using AudioSync.Core.Merging;

namespace AudioSync.Core.Tests;

public class AtempoChainTests
{
    [Fact]
    public void NoOp_When_Atempo_One()
    {
        Assert.Empty(MergeHelpers.AtempoChain(1.0));
        Assert.Empty(MergeHelpers.AtempoChain(1.00001));
    }

    [Fact]
    public void Below_Half_SplitsInto_05x_Stages()
    {
        
        var chain = MergeHelpers.AtempoChain(0.2);
        Assert.Contains("atempo=0.5", chain);
        
        double product = 1.0;
        foreach (var s in chain)
        {
            var v = double.Parse(s.Substring("atempo=".Length), System.Globalization.CultureInfo.InvariantCulture);
            product *= v;
        }
        Assert.Equal(0.2, product, 5);
    }

    [Fact]
    public void Above_100_SplitsInto_100x_Stages()
    {
        var chain = MergeHelpers.AtempoChain(150.0);
        Assert.Contains("atempo=100.0", chain);
        double product = 1.0;
        foreach (var s in chain)
        {
            var v = double.Parse(s.Substring("atempo=".Length), System.Globalization.CultureInfo.InvariantCulture);
            product *= v;
        }
        Assert.Equal(150.0, product, 4);
    }

    [Fact]
    public void OutOfRange_Throws()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => MergeHelpers.AtempoChain(0.001));
        Assert.Throws<ArgumentOutOfRangeException>(() => MergeHelpers.AtempoChain(500));
    }
}
