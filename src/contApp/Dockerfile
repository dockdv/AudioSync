FROM python:3.12-slim

COPY src/webGUI/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg mkvtoolnix && \
    rm -f /usr/bin/ffplay && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY src/webGUI/ ./src/webGUI/

RUN mkdir -p /app/src/webGUI/uploads

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

WORKDIR /app/src/webGUI

CMD ["python", "app.py"]
