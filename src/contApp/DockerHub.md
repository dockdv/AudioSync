# AudioSync

Audio Sync & Merge — add audio tracks from a secondary video file into a primary video file, automatically syncing them to match timing.

Useful when you have two recordings of the same content (e.g., different camera angles, different language dubs, PAL vs NTSC releases) and want to combine the audio tracks into a single file. AudioSync detects the timing difference between the two recordings using audio fingerprinting, then merges the selected audio tracks with the correct offset and speed adjustment.

## Features

- Auto-alignment via audio fingerprinting, cross-correlation, and RANSAC-based matching
- Cross-language and cross-framerate support (e.g., 24fps Blu-ray + 25fps PAL DVD)
- Vocal filter for cross-language matching (band-reject removes speech, keeps music/effects)
- Automatic content break detection with arbitrary segment count (censored scenes, different edits)
- Speed detection across 7 candidates (23.976/25, 24/25, 1.0, 25/24, etc.)
- Real-time decode and merge progress reporting
- Manual sync override (atempo and offset)
- Multi-audio track selection and metadata editing
- Attachment stream selection (cover art, embedded images)
- MKV muxing via mkvmerge with track ordering and default track selection
- FFmpeg-based merge with loudness matching

## How It Works

AudioSync uses a multi-stage alignment pipeline:

1. **Audio Fingerprinting** — Mel-frequency and energy-band fingerprints are extracted from windowed FFT frames at 8kHz.
2. **Cross-Correlation with Speed Search** — Downsampled audio envelopes (~100Hz) are cross-correlated across 7 speed candidates covering PAL/NTSC/film conversions.
3. **RANSAC Linear Fit** — Matched fingerprint pairs are fitted to `t1 = a * t2 + b` using RANSAC to find the speed ratio and offset.
4. **Content Break Detection** — Sliding-window cross-correlation scan detects content breaks (censored scenes, different edits) and aligns each segment independently. Supports arbitrary numbers of segments.
5. **Vocal Filter** — Optional in-memory band-reject filter (removes 300Hz-3kHz) for cross-language matching where dialogue differs but music/effects are shared.

## Screenshots

![AudioSync GUI](https://raw.githubusercontent.com/dockdv/AudioSync/main/screenshots/gui.png)

## Usage

1. Open http://localhost:5000 in your browser.
2. Load Video 1 (primary, keeps video) and Video 2 (audio source) using Browse or Upload.
3. Select which V1 streams to keep (audio, subtitles, attachments) and which V2 audio tracks to merge. Set the default audio track and edit track metadata.
4. Click Auto-Align to compute the speed ratio and offset. Enable "Filter vocals" for cross-language content.
5. Review the results (atempo, offset, inlier count, precision). Adjust manually if needed.
6. Click Run Merge to produce the output file.

## Quick Start

```bash
docker run -p 5000:5000 -v /path/to/videos:/videos dockdv/audiosync:latest
```

Then open http://localhost:5000.

## Docker Compose

```yaml
services:
  audiosync:
    image: dockdv/audiosync:latest
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

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FFMPEG_PATH` | Custom path to ffmpeg binary |
| `FFPROBE_PATH` | Custom path to ffprobe binary |
| `MKVMERGE_PATH` | Custom path to mkvmerge binary |

## Source Code

https://github.com/dockdv/AudioSync

## License

[MIT](https://github.com/dockdv/AudioSync/blob/main/LICENSE)
