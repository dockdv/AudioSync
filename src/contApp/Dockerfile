FROM python:3.12-slim

ARG TARGETARCH

COPY src/webGUI/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

COPY src/webGUI/ ./src/webGUI/

RUN --mount=type=secret,id=ffmpeg_pat \
    apt-get update && apt-get install -y --no-install-recommends curl jq mkvtoolnix && \
    ARCH=$(if [ "$TARGETARCH" = "arm64" ]; then echo "linuxarm64"; else echo "linux64"; fi) && \
    TOKEN=$(cat /run/secrets/ffmpeg_pat) && \
    RELEASE_URL="https://api.github.com/repos/dockdv/ffmpeg-bin/releases/tags/latest" && \
    RELEASE_JSON=$(curl -sf -H "Authorization: token ${TOKEN}" "$RELEASE_URL") && \
    for BIN in ffmpeg ffprobe; do \
        ASSET_URL=$(echo "$RELEASE_JSON" \
            | jq -r ".assets[] | select(.name == \"${BIN}-${ARCH}\") | .url") && \
        curl -sfL -H "Authorization: token ${TOKEN}" \
            -H "Accept: application/octet-stream" \
            -o /app/src/webGUI/${BIN} "$ASSET_URL" && \
        chmod +x /app/src/webGUI/${BIN}; \
    done && \
    apt-get purge -y jq && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/src/webGUI/uploads

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

WORKDIR /app/src/webGUI

CMD ["python", "app.py"]
