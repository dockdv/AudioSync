using System.Numerics;
using MathNet.Numerics.IntegralTransforms;

namespace AudioSync.Core.Sync;

/// <summary>
/// Real-input FFT helpers matching numpy.fft.rfft / irfft semantics:
/// rfft returns N/2+1 complex bins from a length-N real signal (no scaling),
/// irfft reconstructs a length-N real signal from N/2+1 bins (divided by N).
/// </summary>
public static class RealFft
{
    public static Complex[] Rfft(ReadOnlySpan<double> real, int n)
    {
        var buf = new Complex[n];
        int copy = Math.Min(real.Length, n);
        for (int i = 0; i < copy; i++) buf[i] = new Complex(real[i], 0);
        Fourier.Forward(buf, FourierOptions.NoScaling);
        var result = new Complex[n / 2 + 1];
        Array.Copy(buf, result, n / 2 + 1);
        return result;
    }

    public static Complex[] Rfft(ReadOnlySpan<float> real, int n)
    {
        var buf = new Complex[n];
        int copy = Math.Min(real.Length, n);
        for (int i = 0; i < copy; i++) buf[i] = new Complex(real[i], 0);
        Fourier.Forward(buf, FourierOptions.NoScaling);
        var result = new Complex[n / 2 + 1];
        Array.Copy(buf, result, n / 2 + 1);
        return result;
    }

    public static double[] Irfft(Complex[] bins, int n)
    {
        var buf = new Complex[n];
        int half = n / 2 + 1;
        for (int i = 0; i < Math.Min(bins.Length, half); i++) buf[i] = bins[i];
        // Hermitian symmetry: X[N-k] = conj(X[k])
        for (int i = 1; i < n - half + 1; i++)
            buf[n - i] = Complex.Conjugate(buf[i]);
        Fourier.Inverse(buf, FourierOptions.NoScaling);
        var result = new double[n];
        double inv = 1.0 / n;
        for (int i = 0; i < n; i++) result[i] = buf[i].Real * inv;
        return result;
    }

    /// <summary>numpy.fft.rfftfreq(n, d) → frequencies of length n/2+1.</summary>
    public static double[] RfftFreq(int n, double d)
    {
        int len = n / 2 + 1;
        var f = new double[len];
        double scale = 1.0 / (d * n);
        for (int i = 0; i < len; i++) f[i] = i * scale;
        return f;
    }
}
