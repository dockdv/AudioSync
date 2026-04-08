using System.Numerics;
using AudioSync.Core.Tooling;

namespace AudioSync.Core.Sync;





public sealed class AudioLoader
{
    private readonly FfLib _ff;
    public AudioLoader(FfLib ff) { _ff = ff; }

    
    
    
    
    public async Task<(float[] Samples, List<string> Warnings)> DecodeFullAudioAsync(
        string filepath, int trackIndex, int sr,
        bool vocalFilter = false,
        double duration = 0,
        Action<int>? progressCallback = null,
        CancellationToken ct = default)
    {
        var (audio, warningStr) = await _ff.DecodeAudioAsync(
            filepath, trackIndex, sr, vocalFilter, progressCallback, duration, ct).ConfigureAwait(false);
        if (audio.Length == 0) throw new InvalidOperationException("No audio data decoded");

        double decodedDur = (double)audio.Length / sr;
        double expectedDur = duration > 0 ? duration : await _ff.GetDurationAsync(filepath, ct).ConfigureAwait(false);
        var msgs = new List<string>();
        if (!string.IsNullOrEmpty(warningStr)) msgs.Add($"FFmpeg: {warningStr}");
        if (expectedDur > 0 && decodedDur < expectedDur - 30)
            msgs.Add($"Decoded {decodedDur:F1}s of expected {expectedDur:F1}s ({expectedDur - decodedDur:F1}s missing)");
        return (audio, msgs);
    }

    
    
    
    
    public static float[] BandReject(float[] audio, int sr, double center = 1000.0, double width = 2700.0)
    {
        double lo = center - width / 2;
        double hi = center + width / 2;
        
        
        int chunk = 262144;
        int overlap = sr * 2;
        int n = audio.Length;
        var output = new float[n];
        int pos = 0;

        while (pos < n)
        {
            int end = Math.Min(pos + chunk, n);
            int sn = end - pos;
            var seg = new double[sn];
            for (int i = 0; i < sn; i++) seg[i] = audio[pos + i];

            var freqs = RealFft.RfftFreq(sn, 1.0 / sr);
            var mask = new double[freqs.Length];
            for (int i = 0; i < freqs.Length; i++)
            {
                double f = freqs[i];
                if (f >= lo && f <= hi) mask[i] = 0.0;
                else if (f >= lo - 50 && f < lo) mask[i] = (lo - f) / 50.0;
                else if (f > hi && f <= hi + 50) mask[i] = (f - hi) / 50.0;
                else mask[i] = 1.0;
            }
            var fft = RealFft.Rfft(seg, sn);
            for (int i = 0; i < fft.Length; i++) fft[i] *= mask[i];
            var filtered = RealFft.Irfft(fft, sn);

            if (pos > 0 && overlap > 0)
            {
                int ol = Math.Min(Math.Min(overlap, sn), pos);
                for (int i = 0; i < ol; i++)
                {
                    double ramp = ol == 1 ? 1.0 : (double)i / (ol - 1);
                    output[pos + i] = (float)(output[pos + i] * (1 - ramp) + filtered[i] * ramp);
                }
                for (int i = ol; i < sn; i++)
                    output[pos + i] = (float)filtered[i];
            }
            else
            {
                for (int i = 0; i < sn; i++)
                    output[pos + i] = (float)filtered[i];
            }
            pos = end < n ? end - overlap : n;
        }
        return output;
    }
}
