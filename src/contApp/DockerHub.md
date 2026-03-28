# AudioSync

Audio Sync & Merge — add audio tracks from a secondary video file into a primary video file, automatically syncing them to match timing.

Useful when you have two recordings of the same content (e.g., different camera angles or audio sources) and want to combine the audio tracks into a single file. AudioSync detects the timing difference between the two recordings using audio fingerprinting, then merges the selected audio tracks with the correct offset and speed adjustment.

## Features

- Auto-alignment via audio fingerprinting and RANSAC-based matching
- Manual sync override (atempo and offset)
- Multi-audio track selection and metadata editing
- FFmpeg-based merge with progress tracking
- Server-side file browser and drag-and-drop upload

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

## Source Code

https://github.com/dockdv/AudioSync

## License

[MIT](https://github.com/dockdv/AudioSync/blob/main/LICENSE)
