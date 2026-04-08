# AudioSync (.NET)

Audio Sync & Merge — add audio tracks from a secondary video file into a primary video file, automatically syncing them to match timing.

A C# / ASP.NET Core 10 application that ships as a single self-contained executable.

Useful when you have two recordings of the same content (different camera angles, different language dubs, PAL vs NTSC releases) and want to combine the audio tracks into a single file. AudioSync detects the timing difference between the two recordings using audio fingerprinting, then merges the selected audio tracks with the correct offset and speed adjustment.

## Status

Sync engine, visual fine-tune, merger, prober, and sessions are implemented; build is green and the test suite passes.

## Features

- Auto-alignment via audio fingerprinting, cross-correlation, and RANSAC-based matching
- Cross-language and cross-framerate support (e.g., 24fps Blu-ray + 25fps PAL DVD)
- Vocal filter for cross-language matching (band-reject removes speech, keeps music/effects)
- Automatic content break detection with arbitrary segment count
- Speed detection across 7 candidates (23.976/25, 24/25, 1.0, 25/24, etc.)
- Real-time decode and merge progress reporting
- Manual sync override (atempo and offset)
- Multi-audio track selection and metadata editing
- Attachment stream selection (cover art, embedded images)
- MKV muxing via mkvmerge with track ordering and default track selection
- Visual fine-tuning of offset by matching hard cuts (scene changes) across video tracks
- FFmpeg-based merge with loudness matching
- Cross-platform: Windows, Linux, macOS

## How It Works

### Alignment Algorithm

A multi-stage pipeline determines the speed ratio and time offset between two video files:

1. **Audio Decoding** — Full audio is decoded from both files using FFmpeg at 8kHz mono with real-time progress.
2. **Fingerprint Extraction** — Mel-frequency fingerprints (128-band, primary) and energy-band fingerprints (40-band, fallback) from windowed FFT frames.
3. **Cross-Correlation with Speed Search** — Envelopes downsampled to ~100Hz; for each of 7 speed candidates, V2 is time-stretched and FFT cross-correlated against V1. The peak gives coarse speed and offset.
4. **Fingerprint Matching** — Cosine similarity with top-k retrieval, mutual nearest neighbor consistency, filtered by the coarse estimate.
5. **RANSAC Linear Fit** — `t1 = a * t2 + b` fitted via RANSAC (3000 iterations). Slope = speed ratio, intercept = offset. Speed snapped to nearest known candidate within 0.5%.
6. **Quality Fallback** — If RANSAC inliers <15, residuals high, or V1 coverage poor, the cross-correlation result is used.
7. **Content Break Detection** — Sliding-window cross-correlation across the file detects content breaks; each segment gets its own offset; merge uses FFmpeg `concat`. Minimum segment length 60s.
8. **Visual Fine-Tune** — When both files have video, offset is refined by detecting hard cuts in V1 (MSE on keyframe pairs), then matching each cut in V2 via perceptual hashing (pHash). Frames are tone-mapped to BT.709 and letterbox-cropped before comparison. Requires 3 cuts agreeing within ±2 frames.

### Vocal Filter (Cross-Language Mode)

In-memory band-reject filter on already-decoded audio. Removes 300Hz–3kHz vocal range, keeping bass and treble for cross-correlation.

### Merge

FFmpeg complex filtergraph applies:
- `atempo` for speed adjustment (chained for extreme values)
- `adelay` for positive offsets
- `atrim` for negative offsets
- `concat` for piecewise segments

For MKV output, **mkvmerge** handles final muxing with track inclusion, reordering, default track flag, per-track language/title metadata, and stream-copy when no re-encoding is needed.

## Screenshots

![AudioSync GUI](https://raw.githubusercontent.com/dockdv/AudioSync/main/screenshots/gui.png)

## Requirements

- **.NET 10 SDK** (build) or .NET 10 runtime (run framework-dependent build)
- **ffmpeg**, **ffprobe**, **mkvmerge** binaries — placed alongside the executable, or on PATH, or located via configuration (see `appsettings.json`)

## Build

```bash
cd src/webGUI
dotnet build
```

## Run (development)

```bash
cd src/webGUI
dotnet run --project src/AudioSync.Web
```

Then open http://localhost:5000.

## Docker

Pre-built Linux image: [`dockdv/audiosync` on Docker Hub](https://hub.docker.com/r/dockdv/audiosync).

```yaml
services:
  audiosync:
    image: ghcr.io/dockdv/audiosync:latest
    ports:
      - 5000:5000
    volumes:
      - /path/to/videos:/videos
      # - /usr/local/bin/ffmpeg:/usr/local/bin/ffmpeg:ro
      # - /usr/local/bin/ffprobe:/usr/local/bin/ffprobe:ro
    environment:
      # - FFMPEG_PATH=/usr/local/bin/ffmpeg
      # - FFPROBE_PATH=/usr/local/bin/ffprobe
    restart: unless-stopped
```

Then open http://localhost:5000.

## Publish (single-file Windows executable)

```bash
cd src/webGUI
dotnet publish src/AudioSync.Web -c Release -r win-x64 --self-contained -p:PublishSingleFile=true
```

Drop `ffmpeg.exe`, `ffprobe.exe`, `mkvmerge.exe` next to the produced `AudioSync.Web.exe`.

## License

[MIT](../LICENSE)
