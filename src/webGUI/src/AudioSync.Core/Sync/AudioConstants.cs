namespace AudioSync.Core.Sync;

/// <summary>Mirror of audio.py module-level constants.</summary>
public static class AudioConstants
{
    public const int AudioSampleRate = 8000;
    public const double AudioWindowSec = 0.5;
    public const double AudioHopSec = 0.2;
    public const int AudioMaxSamples = 8000;
    public const int AudioNBands = 40;
    public const int AudioMatchTopK = 3;
    public const int AudioRansacIterations = 3000;
    public const double AudioRansacThresholdSec = 0.3;
    public const double AudioXcorrWindowSec = 10.0;
    public const int XcorrDownsampleRate = 100;
    public const double SpeedSnapTolerance = 0.005;
    public const int AudioNMels = 128;

    public static readonly double[] SpeedCandidates =
    {
        23.976 / 25.0,
        24.0 / 25.0,
        23.976 / 24.0,
        1.0,
        24.0 / 23.976,
        25.0 / 24.0,
        25.0 / 23.976,
    };
}
