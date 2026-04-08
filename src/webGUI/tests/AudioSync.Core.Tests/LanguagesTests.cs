using AudioSync.Core.Probing;

namespace AudioSync.Core.Tests;

public class LanguagesTests
{
    [Theory]
    [InlineData("en", "eng")]
    [InlineData("EN", "eng")]
    [InlineData("fra", "fre")]
    [InlineData("eng", "eng")]
    [InlineData("xyz", "xyz")]
    [InlineData("", "und")]
    [InlineData(null, "und")]
    public void Normalize3_HandlesIso2_Iso3_AltCodes(string? input, string expected)
        => Assert.Equal(expected, Languages.Normalize3(input));

    [Fact]
    public void All_StartsWithUndAndContainsKnownEntries()
    {
        Assert.Equal(("und", "Undetermined"), Languages.All[0]);
        Assert.Contains(("eng", "English"), Languages.All);
        Assert.Contains(("jpn", "Japanese"), Languages.All);
    }

    [Theory]
    [InlineData("foo.mkv", false, ".mkv")]
    [InlineData("foo.MKV", false, ".mkv")]
    [InlineData("foo.mp4", false, ".mp4")]
    [InlineData("foo.avi", true, ".avi")]
    [InlineData("foo.bin", true, ".bin")]
    public void NeedsContainerChange(string path, bool needsChange, string ext)
    {
        var (n, e) = Languages.NeedsContainerChange(path);
        Assert.Equal(needsChange, n);
        Assert.Equal(ext, e);
    }
}
