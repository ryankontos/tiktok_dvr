FROM python:3.11-slim

# System deps: git + ffmpeg + rclone
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ffmpeg rclone && \
    rm -rf /var/lib/apt/lists/*

# Clone the original recorder repo inside the image
WORKDIR /opt
RUN git clone https://github.com/Michele0303/tiktok-live-recorder.git

# Install its Python dependencies + tzdata (for zoneinfo)
WORKDIR /opt/tiktok-live-recorder/src
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir tzdata

# Copy our DVR app into the image
WORKDIR /app
COPY app /app

# Ensure Python can import from the recorder repo
ENV PYTHONPATH="/opt/tiktok-live-recorder/src:${PYTHONPATH}"

# Default config + output locations inside container
ENV TLDVR_COOKIES_PATH="/config/cookies.json"
ENV TLDVR_OUTPUT_DIR="/output"
ENV TLDVR_TIMEZONE="UTC"
ENV TLDVR_GDRIVE_FOLDER="BBAU2025_Live_Feed_Archive"

# Copy rclone config into the container so it's universal
RUN mkdir -p /root/.config/rclone
COPY rclone/rclone.conf /root/.config/rclone/rclone.conf

# Create directories (backed by Docker volumes/binds)
RUN mkdir -p /config /output

# Run our DVR app by default
ENTRYPOINT ["python", "-u", "/app/dvr.py"]