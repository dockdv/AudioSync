using System.Numerics;

namespace AudioSync.Core.Sync;

public sealed class XcorrResult
{
    public double Offset { get; init; }
    public double Speed { get; init; } = 1.0;
    public double Correlation { get; init; }
    public List<(double Offset, double Speed, double Correlation)>? AltOffsets { get; init; }
}




public static class CrossCorrelation
{
    
    public static (double[] Ds, double EffectiveRate) DownsampleAudio(
        float[] audio, int sr = AudioConstants.AudioSampleRate)
    {
        int block = Math.Max(1, sr / AudioConstants.XcorrDownsampleRate);
        double effectiveRate = (double)sr / block;
        int n = audio.Length - audio.Length % block;
        int outLen = n / block;
        var ds = new double[outLen];
        for (int i = 0; i < outLen; i++)
        {
            double sum = 0;
            int baseIdx = i * block;
            for (int k = 0; k < block; k++) sum += Math.Abs(audio[baseIdx + k]);
            ds[i] = sum / block;
        }
        return (ds, effectiveRate);
    }

    
    public static List<(int I, int J, double Sim)> FilterMatchesByOffset(
        List<(int I, int J, double Sim)> matches, double[] ts1, double[] ts2,
        double coarseOffset, double windowSec = AudioConstants.AudioXcorrWindowSec, double speed = 1.0)
    {
        var result = new List<(int, int, double)>(matches.Count);
        foreach (var (i, j, sim) in matches)
        {
            double predicted = speed * ts2[j] + coarseOffset;
            if (Math.Abs(ts1[i] - predicted) <= windowSec)
                result.Add((i, j, sim));
        }
        return result;
    }

    private static List<(double Value, double Lag)> FindXcorrPeaks(
        double[] xcorr, int nfft, double effectiveRate, int nPeaks = 3, double minSepSec = 5.0)
    {
        int minSep = (int)(minSepSec * effectiveRate);
        var peaks = new List<(double, double)>();
        var copy = (double[])xcorr.Clone();
        for (int p = 0; p < nPeaks; p++)
        {
            int pi = 0;
            double pv = double.NegativeInfinity;
            for (int i = 0; i < copy.Length; i++)
                if (copy[i] > pv) { pv = copy[i]; pi = i; }
            if (pv <= 0) break;
            int lag = pi <= nfft / 2 ? pi : pi - nfft;
            peaks.Add((pv, lag / effectiveRate));
            int lo = Math.Max(0, pi - minSep);
            int hi = Math.Min(copy.Length, pi + minSep + 1);
            for (int i = lo; i < hi; i++) copy[i] = 0;
        }
        return peaks;
    }

    
    public static double[] LinearResample(double[] data, int outLen)
    {
        if (data.Length == 0 || outLen <= 0) return Array.Empty<double>();
        if (data.Length == 1)
        {
            var r = new double[outLen];
            for (int i = 0; i < outLen; i++) r[i] = data[0];
            return r;
        }
        var result = new double[outLen];
        double xMax = data.Length - 1;
        for (int i = 0; i < outLen; i++)
        {
            double x = outLen == 1 ? 0 : xMax * i / (outLen - 1);
            int x0 = (int)Math.Floor(x);
            if (x0 < 0) { result[i] = data[0]; continue; }
            if (x0 >= data.Length - 1) { result[i] = data[^1]; continue; }
            double frac = x - x0;
            result[i] = data[x0] * (1 - frac) + data[x0 + 1] * frac;
        }
        return result;
    }

    
    public static XcorrResult XcorrOnDownsampled(
        double[] d1, double[] d2, double effectiveRate, double[] speedCandidates,
        bool returnAltOffsets = false)
    {
        double bestCorr = double.NegativeInfinity;
        double bestOffset = 0.0;
        double bestSpeed = 1.0;
        var allPeaks = returnAltOffsets ? new List<(double Norm, double Off, double Spd)>() : null;

        double mean1 = 0;
        for (int i = 0; i < d1.Length; i++) mean1 += d1[i];
        mean1 /= Math.Max(1, d1.Length);
        var d1n = new double[d1.Length];
        double var1 = 0;
        for (int i = 0; i < d1.Length; i++) { d1n[i] = d1[i] - mean1; var1 += d1n[i] * d1n[i]; }
        double s1 = Math.Sqrt(var1 / Math.Max(1, d1.Length));
        if (s1 > 0)
            for (int i = 0; i < d1.Length; i++) d1n[i] /= s1;

        foreach (var speed in speedCandidates)
        {
            int n2s = (int)(d2.Length * speed);
            if (n2s < 2) continue;
            var d2s = LinearResample(d2, n2s);
            double mean2 = 0;
            for (int i = 0; i < n2s; i++) mean2 += d2s[i];
            mean2 /= n2s;
            var d2n = new double[n2s];
            double var2 = 0;
            for (int i = 0; i < n2s; i++) { d2n[i] = d2s[i] - mean2; var2 += d2n[i] * d2n[i]; }
            double s2 = Math.Sqrt(var2 / n2s);
            if (s2 > 0)
                for (int i = 0; i < n2s; i++) d2n[i] /= s2;

            int n = d1n.Length + d2n.Length - 1;
            int nfft = 1;
            while (nfft < n) nfft <<= 1;

            var p1 = new double[nfft];
            var p2 = new double[nfft];
            Array.Copy(d1n, p1, d1n.Length);
            Array.Copy(d2n, p2, d2n.Length);

            var P1 = RealFft.Rfft(p1, nfft);
            var P2 = RealFft.Rfft(p2, nfft);
            var prod = new Complex[P1.Length];
            for (int i = 0; i < P1.Length; i++)
                prod[i] = P1[i] * Complex.Conjugate(P2[i]);
            var xcorr = RealFft.Irfft(prod, nfft);

            int overlap = Math.Min(d1n.Length, d2n.Length);
            double maxV = double.NegativeInfinity;
            int maxI = 0;
            for (int i = 0; i < xcorr.Length; i++)
                if (xcorr[i] > maxV) { maxV = xcorr[i]; maxI = i; }
            double pv = overlap > 0 ? maxV / overlap : 0.0;
            int pi = maxI;
            if (pi > nfft / 2) pi -= nfft;
            if (pv > bestCorr)
            {
                bestCorr = pv;
                bestOffset = (double)pi / effectiveRate;
                bestSpeed = speed;
            }
            if (returnAltOffsets)
            {
                var peaks = FindXcorrPeaks(xcorr, nfft, effectiveRate);
                foreach (var (peakV, peakOff) in peaks)
                {
                    double normPv = overlap > 0 ? peakV / overlap : 0.0;
                    allPeaks!.Add((normPv, peakOff, speed));
                }
            }
        }

        if (returnAltOffsets)
        {
            allPeaks!.Sort((a, b) => b.Norm.CompareTo(a.Norm));
            var alt = new List<(double, double, double)>();
            foreach (var (corr, off, spd) in allPeaks)
            {
                if (Math.Abs(off - bestOffset) > 5.0 || Math.Abs(spd - bestSpeed) > 0.001)
                    alt.Add((off, spd, corr));
            }
            return new XcorrResult { Offset = bestOffset, Speed = bestSpeed, Correlation = bestCorr, AltOffsets = alt };
        }
        return new XcorrResult { Offset = bestOffset, Speed = bestSpeed, Correlation = bestCorr };
    }
}
