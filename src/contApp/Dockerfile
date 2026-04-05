FROM python:3.12-slim

COPY src/webGUI/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
    /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg mkvtoolnix \
        intel-media-va-driver-non-free libva-drm2 \
        libnvcuvid1 libnvidia-encode1 && \
    rm -f /usr/bin/ffplay && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY src/webGUI/ ./src/webGUI/

RUN mkdir -p /app/src/webGUI/uploads

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

WORKDIR /app/src/webGUI

CMD ["python", "app.py"]
