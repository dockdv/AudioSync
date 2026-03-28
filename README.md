# AudioSync

Audio Sync & Merge — add audio tracks from a secondary video file into a primary video file, automatically syncing them to match timing.

Useful when you have two recordings of the same content (e.g., different camera angles or audio sources) and want to combine the audio tracks into a single file. AudioSync detects the timing difference between the two recordings using audio fingerprinting, then merges the selected audio tracks with the correct offset and speed adjustment.

## Features

- Auto-alignment via audio fingerprinting and RANSAC-based matching
- Manual sync override (atempo and offset)
- Multi-audio track selection and metadata editing
- FFmpeg-based merge with progress tracking
- Server-side file browser and drag-and-drop upload
- Cross-platform: Windows, Linux, macOS, Docker

## Windows Standalone

Download `AudioSync.exe` from the [latest release](https://github.com/dockdv/AudioSync/releases/latest). Place `ffmpeg.exe` and `ffprobe.exe` in the same folder (or ensure they are on PATH), then run `AudioSync.exe` and open http://localhost:5000.

## webGUI

### Requirements

- Python 3.10+
- FFmpeg and FFprobe on PATH (or set `FFMPEG_PATH` / `FFPROBE_PATH` environment variables)

### Linux / macOS

```bash
cd src/webGUI
./start.sh
```

### Windows

```cmd
src\webGUI\start.bat
```

Then open http://localhost:5000.

## Docker

### Build and run

```bash
docker build -t audiosync -f src/contApp/Dockerfile .
docker run -p 5000:5000 -v /path/to/videos:/videos audiosync
```

### Docker Compose

```bash
cd src/contApp
docker compose up --build
```

### Docker Compose (published image)

```yaml
services:
  audiosync:
    image: ghcr.io/dockdv/audiosync:latest  # or dockdv/audiosync:latest for DockerHub
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

## License

[MIT](LICENSE)
