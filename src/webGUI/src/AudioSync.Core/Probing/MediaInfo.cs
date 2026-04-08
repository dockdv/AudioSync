namespace AudioSync.Core.Probing;




public sealed class StreamEntry
{
    public int StreamIndex { get; init; }
    public string CodecType { get; init; } = "unknown"; 
    public string Codec { get; init; } = "?";
    public string Language { get; init; } = "und";
    public string Title { get; init; } = "";
    public double StartTime { get; init; }

    
    public int? AudioIndex { get; init; }
    public int? Channels { get; init; }
    public int? SampleRate { get; init; }
    public bool Empty { get; init; }

    
    public int? Width { get; init; }
    public int? Height { get; init; }
    public double? FrameRate { get; init; }

    
    public string? SubtitleCodec { get; init; }
}


public sealed class AudioTrack
{
    public int Index { get; init; }
    public int StreamIndex { get; init; }
    public string Codec { get; init; } = "?";
    public int Channels { get; init; }
    public int SampleRate { get; init; }
    public long BitRate { get; init; }
    public string Language { get; init; } = "und";
    public string Title { get; init; } = "";
    public double StartTime { get; init; }
}


public sealed class ProbeResult
{
    public List<AudioTrack> Audio { get; init; } = new();
    public List<StreamEntry> Streams { get; init; } = new();
    public double Duration { get; init; }
}




public sealed class FullProbeResult
{
    public List<AudioTrack> Tracks { get; init; } = new();
    public List<StreamEntry> Streams { get; init; } = new();
    public double Duration { get; init; }
    public string Method { get; init; } = "libav";
    public string Error { get; init; } = "";
    public string Warning { get; init; } = "";
}
