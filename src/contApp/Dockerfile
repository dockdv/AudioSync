FROM python:3.12-slim

ARG TARGETARCH

COPY src/webGUI/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

COPY src/webGUI/ ./src/webGUI/

RUN --mount=type=secret,id=ffmpeg_pat \
    apt-get update && apt-get install -y --no-install-recommends curl && \
    ARCH=$(if [ "$TARGETARCH" = "arm64" ]; then echo "linuxarm64"; else echo "linux64"; fi) && \
    TOKEN=$(cat /run/secrets/ffmpeg_pat) && \
    RELEASE_URL="https://api.github.com/repos/dockdv/ffmpeg-bin/releases/latest" && \
    for BIN in ffmpeg ffprobe; do \
        URL=$(curl -sf -H "Authorization: token ${TOKEN}" "$RELEASE_URL" \
            | grep -o "\"browser_download_url\": *\"[^\"]*${BIN}-${ARCH}\"" \
            | cut -d'"' -f4) && \
        curl -sfL -H "Authorization: token ${TOKEN}" \
            -H "Accept: application/octet-stream" \
            -o /app/src/webGUI/${BIN} "$URL" && \
        chmod +x /app/src/webGUI/${BIN}; \
    done && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/src/webGUI/uploads

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

WORKDIR /app/src/webGUI

CMD ["python", "app.py"]
