using AudioSync.Core.Probing;
using AudioSync.Core.Sync;

namespace AudioSync.Core.Sessions;






public sealed class SessionContext
{
    
    public string? V1Path { get; set; }
    public string? V2Path { get; set; }
    public string? OutPath { get; set; }

    
    public ProbeResult? V1Info { get; set; }
    public ProbeResult? V2Info { get; set; }
    public double V1Duration { get; set; }

    
    public int AlignTrack1 { get; set; }
    public int AlignTrack2 { get; set; }
    public bool VocalFilter { get; set; }
    public bool MeasureLufs { get; set; }

    
    public double AlignDur1 { get; set; }
    public double AlignDur2 { get; set; }
    public double AlignHop1 { get; set; }
    public double AlignHop2 { get; set; }
    public int AlignMaxSamples { get; set; }
    public bool V1HasVideo { get; set; }
    public bool V2HasVideo { get; set; }
    public float[]? Audio1 { get; set; }
    public float[]? Audio2 { get; set; }
    public double[]? Ts1 { get; set; }
    public double[]? Ts2 { get; set; }
    public double[][]? Fp1Main { get; set; }
    public double[][]? Fp2Main { get; set; }
    public List<string> DecodeWarnings { get; set; } = new();
    public double CoarseOffset { get; set; }
    public double XcorrSpeed { get; set; } = 1.0;
    public double AudioOffset { get; set; }
    public double AudioSpeed { get; set; } = 1.0;
    public List<double> AltOffsets { get; set; } = new();
    public double[]? Ds1Seg { get; set; }
    public double[]? Ds2Seg { get; set; }
    public double DsRate { get; set; }
    public double? VisualRefinedOffset { get; set; }
    
    public double? RansacOffset { get; set; }
    public double V2StartDelay { get; set; }
    public string AlignMode { get; set; } = "";
    public double AlignA { get; set; } = 1.0;
    public double AlignB { get; set; }
    public int AlignNi { get; set; }
    public int AlignTotalGood { get; set; }
    public List<(double T1, double T2, double Sim)> AlignPairs { get; set; } = new();
    public double AlignRmean { get; set; }
    public double AlignRmax { get; set; }
    public double AlignRend { get; set; }

    
    public double Atempo { get; set; } = 1.0;
    public double Offset { get; set; }
    public List<DetectedSegment>? Segments { get; set; }
    public double? V1Lufs { get; set; }
    public double? V2Lufs { get; set; }

    
    public List<int>? V1StreamIndices { get; set; }
    public List<int>? V2StreamIndices { get; set; }
    public List<AudioMetadata>? AudioMetadata { get; set; }
    
    
    
    
    public List<int>? AudioOrder { get; set; }
    public int? DefaultAudioIndex { get; set; }
    public List<TrackMetadata>? V1SubMetadata { get; set; }
    public List<TrackMetadata>? V2SubMetadata { get; set; }
    public List<TrackMetadata>? V1VidMetadata { get; set; }
    public double? DurationLimit { get; set; }
    public bool GainMatch { get; set; }
    public bool V1HasAttachments { get; set; } = true;
    public bool V2HasAttachments { get; set; }

    
    public bool IsRemux { get; set; }
    public string? FfmpegPath { get; set; }
    public int V1SampleRate { get; set; } = 48000;
    public double V1Dur { get; set; }

    
    public Dictionary<int, string> V1StreamTypes { get; set; } = new();
    public List<int> V1VidSi { get; set; } = new();
    public List<int> V1AudSi { get; set; } = new();
    public List<int> V1SubSi { get; set; } = new();
    public List<int> V1OtherSi { get; set; } = new();
    public bool V1HasSubs { get; set; }

    
    public Dictionary<int, string> V2StreamTypes { get; set; } = new();
    public List<int> V2AudSi { get; set; } = new();
    public List<int> V2SubSi { get; set; } = new();
    public List<int> V2AudIndices { get; set; } = new();

    
    public List<int> V1VidTids { get; set; } = new();
    public List<int> V1AudTids { get; set; } = new();
    public List<int> V1SubTids { get; set; } = new();
    public List<int> V1OtherTids { get; set; } = new();

    
    public string? TmpAudioPath { get; set; }
    public bool V2Streamcopy { get; set; }
    public List<int> V2AudTids { get; set; } = new();
    public List<int> V2SubTids { get; set; } = new();

    
    public List<(int FileId, int Tid)> AudioFt { get; set; } = new();
    public List<(int FileId, int Tid)> AudioFtOrdered { get; set; } = new();
    public (int FileId, int Tid)? DefaultAudioFt { get; set; }
    public Dictionary<(int FileId, int Tid), AudioMetadata> AudioSrcToMeta { get; set; } = new();
}

public sealed class AudioMetadata
{
    public int FileId { get; init; }   
    public int Tid { get; init; }      
    public string Language { get; init; } = "und";
    public string Title { get; init; } = "";
    public bool Default { get; init; }
}

public sealed class TrackMetadata
{
    public int Tid { get; init; }
    public string Language { get; init; } = "und";
    public string Title { get; init; } = "";
    public bool Default { get; init; }
}
