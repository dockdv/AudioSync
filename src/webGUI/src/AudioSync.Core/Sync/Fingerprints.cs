namespace AudioSync.Core.Sync;

public sealed class FingerprintResult
{
    public double[] Timestamps { get; init; } = Array.Empty<double>();
    public double[][] Fingerprints { get; init; } = Array.Empty<double[]>();
}





public static class Fingerprints
{
    
    public static double[][] BuildMelFilterbank(int nFft, int sr,
        int nMels = AudioConstants.AudioNMels, double fmin = 60.0, double? fmax = null)
    {
        double fmaxVal = fmax ?? sr / 2.0;
        double melMin = 2595.0 * Math.Log10(1.0 + fmin / 700.0);
        double melMax = 2595.0 * Math.Log10(1.0 + fmaxVal / 700.0);
        var bins = new int[nMels + 2];
        for (int i = 0; i < nMels + 2; i++)
        {
            double mel = melMin + (melMax - melMin) * i / (nMels + 1);
            double hz = 700.0 * (Math.Pow(10.0, mel / 2595.0) - 1.0);
            int b = (int)Math.Floor((nFft - 1) * 2 * hz / sr);
            if (b < 0) b = 0;
            if (b > nFft - 1) b = nFft - 1;
            bins[i] = b;
        }
        var fb = new double[nMels][];
        for (int m = 0; m < nMels; m++)
        {
            fb[m] = new double[nFft];
            int lo = bins[m], mid = bins[m + 1], hi = bins[m + 2];
            if (mid == lo) mid = lo + 1;
            if (hi == mid) hi = mid + 1;
            for (int k = lo; k < mid && k < nFft; k++)
                fb[m][k] = (double)(k - lo) / (mid - lo);
            for (int k = mid; k < hi && k < nFft; k++)
                fb[m][k] = (double)(hi - k) / (hi - mid);
        }
        return fb;
    }

    
    public static List<(int I, int J, double Sim)> Match(
        double[][] fp1, double[][] fp2, int topK = AudioConstants.AudioMatchTopK)
    {
        int n1 = fp1.Length, n2 = fp2.Length;
        if (n1 == 0 || n2 == 0) return new();
        int k = Math.Min(topK, n2);
        var result = new List<(int, int, double)>(n1 * k);
        var sims = new double[n2];
        for (int i = 0; i < n1; i++)
        {
            var a = fp1[i];
            int bands = a.Length;
            for (int j = 0; j < n2; j++)
            {
                var b = fp2[j];
                double s = 0;
                int len = Math.Min(bands, b.Length);
                for (int x = 0; x < len; x++) s += a[x] * b[x];
                sims[j] = s;
            }
            var idx = new int[n2];
            for (int j = 0; j < n2; j++) idx[j] = j;
            Array.Sort(idx, (x, y) => sims[y].CompareTo(sims[x]));
            for (int t = 0; t < k; t++)
                result.Add((i, idx[t], sims[idx[t]]));
        }
        return result;
    }

    
    public static List<(int I, int J, double Sim)> MutualNearestNeighbors(
        List<(int I, int J, double Sim)> matches, int n1, int n2,
        int topK = AudioConstants.AudioMatchTopK)
    {
        var reverse = new Dictionary<int, List<(int I, double Sim)>>();
        foreach (var (i, j, sim) in matches)
        {
            if (!reverse.TryGetValue(j, out var list))
                reverse[j] = list = new();
            list.Add((i, sim));
        }
        var reverseTop = new Dictionary<int, HashSet<int>>();
        foreach (var (j, candidates) in reverse)
        {
            candidates.Sort((a, b) => b.Sim.CompareTo(a.Sim));
            var set = new HashSet<int>();
            for (int t = 0; t < Math.Min(topK, candidates.Count); t++)
                set.Add(candidates[t].I);
            reverseTop[j] = set;
        }
        var filtered = new List<(int, int, double)>(matches.Count);
        foreach (var (i, j, sim) in matches)
            if (reverseTop.TryGetValue(j, out var set) && set.Contains(i))
                filtered.Add((i, j, sim));
        return filtered;
    }

    
    
    
    
    public static FingerprintResult Extract(
        float[] audio, int sr, int maxSamples, double hopSec, double windowSec,
        Func<double[], double[]> frameFn,
        Action<int, int>? progressCallback = null,
        CancellationToken ct = default)
    {
        int windowSamples = (int)(windowSec * sr);
        int hopSamples = (int)(hopSec * sr);
        if (audio.Length < windowSamples)
            throw new InvalidOperationException("Could not extract enough audio data");

        var hann = new double[windowSamples];
        for (int i = 0; i < windowSamples; i++)
            hann[i] = 0.5 * (1.0 - Math.Cos(2.0 * Math.PI * i / (windowSamples - 1)));

        var timestamps = new List<double>();
        var fps = new List<double[]>();
        int pos = 0;
        int count = 0;
        int totalPossible = Math.Min(maxSamples,
            Math.Max(0, (audio.Length - windowSamples) / hopSamples + 1));
        progressCallback?.Invoke(0, maxSamples);

        var frame = new double[windowSamples];
        while (pos + windowSamples <= audio.Length && count < maxSamples)
        {
            if ((count & 0xFF) == 0) ct.ThrowIfCancellationRequested();
            for (int i = 0; i < windowSamples; i++)
                frame[i] = audio[pos + i] * hann[i];
            var bins = RealFft.Rfft(frame, windowSamples);
            var spectrum = new double[bins.Length];
            for (int i = 0; i < bins.Length; i++) spectrum[i] = bins[i].Magnitude;
            var fp = frameFn(spectrum);
            double norm = 0;
            for (int i = 0; i < fp.Length; i++) norm += fp[i] * fp[i];
            norm = Math.Sqrt(norm);
            if (norm > 0)
                for (int i = 0; i < fp.Length; i++) fp[i] /= norm;
            timestamps.Add((double)pos / sr);
            fps.Add(fp);
            pos += hopSamples;
            count++;
            if (progressCallback != null && count % 500 == 0)
                progressCallback(count, totalPossible);
        }
        progressCallback?.Invoke(count, count);
        if (count < 10) throw new InvalidOperationException($"Only {count} fingerprints extracted");

        return new FingerprintResult
        {
            Timestamps = timestamps.ToArray(),
            Fingerprints = fps.ToArray(),
        };
    }

    public static FingerprintResult ExtractMel(
        float[] audio, int sr,
        int maxSamples = AudioConstants.AudioMaxSamples,
        double hopSec = AudioConstants.AudioHopSec,
        double windowSec = AudioConstants.AudioWindowSec,
        int nMels = AudioConstants.AudioNMels,
        Action<int, int>? progressCallback = null,
        CancellationToken ct = default)
    {
        int windowSamples = (int)(windowSec * sr);
        int nFftBins = windowSamples / 2 + 1;
        var fb = BuildMelFilterbank(nFftBins, sr, nMels);

        double[] FrameFn(double[] spectrum)
        {
            var fp = new double[nMels];
            for (int m = 0; m < nMels; m++)
            {
                double sum = 0;
                var row = fb[m];
                int len = Math.Min(row.Length, spectrum.Length);
                for (int k = 0; k < len; k++) sum += row[k] * spectrum[k];
                fp[m] = Math.Log(1.0 + sum); 
            }
            return fp;
        }
        return Extract(audio, sr, maxSamples, hopSec, windowSec, FrameFn, progressCallback, ct);
    }

    public static FingerprintResult ExtractEnergy(
        float[] audio, int sr,
        int maxSamples = AudioConstants.AudioMaxSamples,
        double hopSec = AudioConstants.AudioHopSec,
        double windowSec = AudioConstants.AudioWindowSec,
        Action<int, int>? progressCallback = null,
        CancellationToken ct = default)
    {
        int windowSamples = (int)(windowSec * sr);
        int nFftBins = windowSamples / 2 + 1;
        int nBands = AudioConstants.AudioNBands;

        int minBin = Math.Max(1, (int)(60.0 / ((double)sr / windowSamples)));
        var bandEdges = new int[nBands + 1];
        double logLo = Math.Log10(minBin);
        double logHi = Math.Log10(nFftBins - 1);
        for (int i = 0; i <= nBands; i++)
        {
            double v = Math.Pow(10, logLo + (logHi - logLo) * i / nBands);
            int e = (int)v;
            if (e < 0) e = 0;
            if (e > nFftBins - 1) e = nFftBins - 1;
            bandEdges[i] = e;
        }
        var safe = (int[])bandEdges.Clone();
        for (int i = 1; i < safe.Length; i++)
            if (safe[i] < safe[i - 1] + 1) safe[i] = safe[i - 1] + 1;
        var widths = new double[nBands];
        for (int i = 0; i < nBands; i++) widths[i] = safe[i + 1] - safe[i];

        double[] FrameFn(double[] spectrum)
        {
            var fp = new double[nBands];
            for (int b = 0; b < nBands; b++)
            {
                int lo = safe[b];
                int hi = (b == nBands - 1) ? spectrum.Length : safe[b + 1];
                if (lo > spectrum.Length) lo = spectrum.Length;
                if (hi > spectrum.Length) hi = spectrum.Length;
                double sum = 0;
                for (int k = lo; k < hi; k++) sum += spectrum[k];
                fp[b] = Math.Log(1.0 + sum / widths[b]);
            }
            return fp;
        }
        return Extract(audio, sr, maxSamples, hopSec, windowSec, FrameFn, progressCallback, ct);
    }
}
