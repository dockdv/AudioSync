namespace AudioSync.Core.Visual;

/// <summary>
/// Mirror of visual.py _dct2 / _phash / frame_similarity.
/// 32×32 grayscale frame → 2D Type-II DCT → 8×8 top-left → median threshold → 64-bit hash.
/// </summary>
public static class PerceptualHash
{
    public const int DctSize = 32;
    public const int HashSize = 8;

    private static readonly double[,] DctBasis = BuildBasis(DctSize);

    private static double[,] BuildBasis(int n)
    {
        var b = new double[n, n];
        for (int row = 0; row < n; row++)
            for (int col = 0; col < n; col++)
                b[row, col] = Math.Cos(Math.PI * (2 * row + 1) * col / (2.0 * n));
        return b;
    }

    /// <summary>Separable Type-II 2D DCT (no orthonormal scaling), matching visual._dct2.</summary>
    public static double[,] Dct2(double[,] block)
    {
        int n = block.GetLength(0);
        // tmp = basis.T @ block  (n,n) = (k1,n1) · (n1,n2)
        var tmp = new double[n, n];
        for (int k1 = 0; k1 < n; k1++)
        {
            for (int n2 = 0; n2 < n; n2++)
            {
                double s = 0;
                for (int n1 = 0; n1 < n; n1++)
                    s += DctBasis[n1, k1] * block[n1, n2];
                tmp[k1, n2] = s;
            }
        }
        // out = tmp @ basis  (k1,k2) = (k1,n2) · (n2,k2)
        var outArr = new double[n, n];
        for (int k1 = 0; k1 < n; k1++)
        {
            for (int k2 = 0; k2 < n; k2++)
            {
                double s = 0;
                for (int n2 = 0; n2 < n; n2++)
                    s += tmp[k1, n2] * DctBasis[n2, k2];
                outArr[k1, k2] = s;
            }
        }
        return outArr;
    }

    /// <summary>
    /// Mirror of visual._phash. Resizes frame to 32×32 (block-mean if downsampling,
    /// nearest-via-linspace if upsampling), runs DCT, hashes 64 low-frequency
    /// coefficients (skipping DC for the median).
    /// </summary>
    public static ulong PHash(double[] frame, int height, int width)
    {
        var resized = new double[DctSize, DctSize];
        int rh = height / DctSize;
        int rw = width / DctSize;
        if (rh >= 1 && rw >= 1)
        {
            // Block-mean reduction: frame[:rh*32, :rw*32] reshape (32,rh,32,rw) mean over (1,3)
            int ch = rh * DctSize;
            int cw = rw * DctSize;
            double inv = 1.0 / (rh * rw);
            for (int dy = 0; dy < DctSize; dy++)
            {
                for (int dx = 0; dx < DctSize; dx++)
                {
                    double sum = 0;
                    int yBase = dy * rh;
                    int xBase = dx * rw;
                    for (int yy = 0; yy < rh; yy++)
                    {
                        int row = (yBase + yy) * width;
                        for (int xx = 0; xx < rw; xx++)
                            sum += frame[row + xBase + xx];
                    }
                    resized[dy, dx] = sum * inv;
                }
            }
        }
        else
        {
            // numpy linspace(0, w-1, 32).astype(int) — truncate toward zero
            var xs = new int[DctSize];
            var ys = new int[DctSize];
            for (int i = 0; i < DctSize; i++)
            {
                xs[i] = (int)((width - 1) * i / (double)(DctSize - 1));
                ys[i] = (int)((height - 1) * i / (double)(DctSize - 1));
            }
            for (int dy = 0; dy < DctSize; dy++)
                for (int dx = 0; dx < DctSize; dx++)
                    resized[dy, dx] = frame[ys[dy] * width + xs[dx]];
        }

        var dct = Dct2(resized);

        // Take 8×8 top-left, flatten row-major (matching numpy ravel order)
        Span<double> low = stackalloc double[HashSize * HashSize];
        for (int y = 0; y < HashSize; y++)
            for (int x = 0; x < HashSize; x++)
                low[y * HashSize + x] = dct[y, x];

        // Median of low[1:]
        Span<double> tail = stackalloc double[HashSize * HashSize - 1];
        for (int i = 1; i < low.Length; i++) tail[i - 1] = low[i];
        var sorted = tail.ToArray();
        Array.Sort(sorted);
        double med;
        int n = sorted.Length;
        if ((n & 1) == 1) med = sorted[n / 2];
        else med = (sorted[n / 2 - 1] + sorted[n / 2]) / 2;

        ulong hash = 0;
        for (int i = 0; i < low.Length; i++)
            if (low[i] > med) hash |= 1UL << i;
        return hash;
    }

    /// <summary>Mirror of visual.frame_similarity (1 - hamming/64).</summary>
    public static double FrameSimilarity(double[]? f1, int h1, int w1, double[]? f2, int h2, int w2)
    {
        if (f1 is null || f2 is null) return -1.0;
        var hashA = PHash(f1, h1, w1);
        var hashB = PHash(f2, h2, w2);
        int hamming = System.Numerics.BitOperations.PopCount(hashA ^ hashB);
        return 1.0 - hamming / 64.0;
    }
}
