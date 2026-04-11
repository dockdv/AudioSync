namespace AudioSync.Core.Visual;

public static class CutDetector
{
    public const double MseThreshold = 500.0;

    public static double[] ToFrame(byte[] bytes)
    {
        var d = new double[bytes.Length];
        for (int i = 0; i < bytes.Length; i++) d[i] = bytes[i];
        return d;
    }

    public static (double[] Cropped, int H, int W) CropLetterbox(
        double[] frame, int h, int w, double frameAr, double targetAr)
    {
        if (frameAr >= targetAr) return (frame, h, w);
        int newH = (int)(w / targetAr);
        int margin = (h - newH) / 2;
        var cropped = new double[newH * w];
        Array.Copy(frame, margin * w, cropped, 0, newH * w);
        return (cropped, newH, w);
    }
}
