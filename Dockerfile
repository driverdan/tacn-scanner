# Whisper transcriber (tr_transcriber.py). Built and run by compose.yaml as the `transcriber` service.
#
# Ubuntu 24.04 with its own numpy and scipy packages, not python:slim with PyPI wheels, on purpose:
# that is the stack the test set results were measured on. The PyPI numpy wheel (OpenBLAS) rounds the
# mel features differently from Ubuntu's build (reference BLAS), which flips close beam-search choices
# and scored 12.3% WER against 10.7% on the same clips.
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-numpy python3-scipy \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCANNER_CONFIG_DIR=/config \
    SCANNER_DATA_DIR=/data \
    SCANNER_HEALTH_FILE=/tmp/transcriber.alive \
    HF_HOME=/models \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tr_transcriber.py .

# /data: trunk-recorder's run dir (media/ in, transcripts out). /config: the repo's config/ (read-only).
# /models: Hugging Face cache, so the Whisper model is downloaded once.
VOLUME ["/data", "/models"]
USER 1000:1000

# The first start may spend minutes downloading the model; after that the loop beats every few seconds.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15m --retries=3 \
    CMD ["python", "/app/tr_transcriber.py", "--healthcheck"]

ENTRYPOINT ["python", "/app/tr_transcriber.py"]
