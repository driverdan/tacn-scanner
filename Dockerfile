# Whisper transcriber (tr_transcriber.py). Built and run by compose.yaml as the `transcriber` service.
#
# Ubuntu 24.04 with its own numpy and scipy packages, not python:slim with PyPI wheels, on purpose:
# that is the stack the test set results were measured on. The PyPI numpy wheel (OpenBLAS) rounds the
# mel features differently from Ubuntu's build (reference BLAS), which flips close beam-search choices
# and scored 12.3% WER against 10.7% on the same clips.
#
# Stages: `base` is the whole CPU image, `rocm` adds AMD GPU support (compose.rocm.yaml builds it),
# and the last stage is `base` again so that a plain build still gives the CPU image.
FROM ubuntu:24.04 AS base

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


# AMD GPUs: CTranslate2's ROCm build in place of the PyPI one, plus the ROCm libraries it loads
# (about 6 GB). The host needs only the amdgpu kernel driver, not ROCm. With no usable GPU in the
# container, this image transcribes on the CPU like the base one.
FROM base AS rocm
# The wheels are built against ROCm 7.2; keep CT2_VERSION in step with requirements.txt.
ARG ROCM_VERSION=7.2.4
ARG CT2_VERSION=4.8.5
ARG CT2_ROCM_SHA256=ff1a4d081c1f3c57ca7677152ae5f264735c3230a463868ded1fd39839f238e3
USER root
# hipblas and hiprand are what libctranslate2 links; rocrand is hiprand's undeclared dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gpg unzip \
    && curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor -o /etc/apt/keyrings/rocm.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${ROCM_VERSION} noble main" \
        > /etc/apt/sources.list.d/rocm.list \
    && printf 'Package: *\nPin: release o=repo.radeon.com\nPin-Priority: 600\n' > /etc/apt/preferences.d/rocm \
    && apt-get update \
    && apt-get install -y --no-install-recommends hipblas hiprand rocrand \
    && rm -rf /var/lib/apt/lists/*
# The ROCm wheels are only on GitHub's release page, one zip for every Python version.
RUN curl -fsSL -o /tmp/ct2.zip \
        "https://github.com/OpenNMT/CTranslate2/releases/download/v${CT2_VERSION}/rocm-python-wheels-Linux.zip" \
    && echo "${CT2_ROCM_SHA256}  /tmp/ct2.zip" | sha256sum -c - \
    && unzip -j -d /tmp/ct2 /tmp/ct2.zip "*/ctranslate2-${CT2_VERSION}-cp312-cp312-*.whl" \
    && pip install --no-cache-dir --no-deps --force-reinstall /tmp/ct2/*.whl \
    && rm -rf /tmp/ct2 /tmp/ct2.zip
ENV LD_LIBRARY_PATH=/opt/rocm-${ROCM_VERSION}/lib
USER 1000:1000

FROM base
