namespace AudioSync.Core.Probing;


public static class Languages
{
    public static readonly IReadOnlyDictionary<string, string> Names = new Dictionary<string, string>
    {
        ["eng"] = "English", ["spa"] = "Spanish", ["fre"] = "French", ["ger"] = "German", ["ita"] = "Italian",
        ["por"] = "Portuguese", ["rus"] = "Russian", ["chi"] = "Chinese", ["jpn"] = "Japanese", ["kor"] = "Korean",
        ["ara"] = "Arabic", ["hin"] = "Hindi", ["tur"] = "Turkish", ["pol"] = "Polish", ["dut"] = "Dutch",
        ["swe"] = "Swedish", ["dan"] = "Danish", ["nor"] = "Norwegian", ["fin"] = "Finnish", ["cze"] = "Czech",
        ["gre"] = "Greek", ["heb"] = "Hebrew", ["tha"] = "Thai", ["vie"] = "Vietnamese", ["ind"] = "Indonesian",
        ["may"] = "Malay", ["rum"] = "Romanian", ["hun"] = "Hungarian", ["ukr"] = "Ukrainian", ["bul"] = "Bulgarian",
        ["hrv"] = "Croatian", ["slo"] = "Slovak", ["slv"] = "Slovenian", ["srp"] = "Serbian", ["lit"] = "Lithuanian",
        ["lav"] = "Latvian", ["est"] = "Estonian", ["cat"] = "Catalan", ["per"] = "Persian", ["urd"] = "Urdu",
        ["ben"] = "Bengali", ["tam"] = "Tamil", ["tel"] = "Telugu", ["mal"] = "Malayalam", ["kan"] = "Kannada",
    };

    private static readonly Dictionary<string, string> Normalize = new()
    {
        ["en"] = "eng", ["es"] = "spa", ["fr"] = "fre", ["de"] = "ger", ["it"] = "ita",
        ["pt"] = "por", ["ru"] = "rus", ["zh"] = "chi", ["ja"] = "jpn", ["ko"] = "kor",
        ["ar"] = "ara", ["hi"] = "hin", ["tr"] = "tur", ["pl"] = "pol", ["nl"] = "dut",
        ["sv"] = "swe", ["da"] = "dan", ["no"] = "nor", ["fi"] = "fin", ["cs"] = "cze",
        ["el"] = "gre", ["he"] = "heb", ["th"] = "tha", ["vi"] = "vie", ["id"] = "ind",
        ["ms"] = "may", ["ro"] = "rum", ["hu"] = "hun", ["uk"] = "ukr", ["bg"] = "bul",
        ["hr"] = "hrv", ["sk"] = "slo", ["sl"] = "slv", ["sr"] = "srp", ["lt"] = "lit",
        ["lv"] = "lav", ["et"] = "est", ["ca"] = "cat", ["fa"] = "per", ["ur"] = "urd",
        ["bn"] = "ben", ["ta"] = "tam", ["te"] = "tel", ["ml"] = "mal", ["kn"] = "kan",
        ["fra"] = "fre", ["deu"] = "ger", ["zho"] = "chi", ["nld"] = "dut", ["ces"] = "cze",
        ["ell"] = "gre", ["fas"] = "per", ["ron"] = "rum", ["slk"] = "slo", ["msa"] = "may",
    };

    public static string Normalize3(string? code)
    {
        if (string.IsNullOrEmpty(code)) return "und";
        var c = code.Trim().ToLowerInvariant();
        return Normalize.TryGetValue(c, out var n) ? n : c;
    }

    
    public static IReadOnlyList<(string Code, string Name)> All { get; } = BuildAll();

    private static IReadOnlyList<(string, string)> BuildAll()
    {
        var list = new List<(string, string)> { ("und", "Undetermined") };
        list.AddRange(Names.Select(kv => (kv.Key, kv.Value)).OrderBy(t => t.Item2, StringComparer.Ordinal));
        return list;
    }

    private static readonly HashSet<string> MultiAudioContainers = new(StringComparer.OrdinalIgnoreCase)
    {
        ".mkv", ".mka", ".mp4", ".m4v", ".mov",
        ".ts", ".mts", ".m2ts", ".webm",
    };

    
    public static (bool NeedsChange, string Ext) NeedsContainerChange(string filepath)
    {
        var ext = Path.GetExtension(filepath).ToLowerInvariant();
        return (!MultiAudioContainers.Contains(ext), ext);
    }
}
