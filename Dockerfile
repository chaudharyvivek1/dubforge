# A dubbing box: python + ffmpeg + dubforge. Nothing else, nothing to configure.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DUBFORGE_WORKDIR=/data/work

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[mcp]"

# Verify the image can actually dub before anyone runs it on a real file.
RUN python -m dubforge doctor

VOLUME ["/data"]
WORKDIR /data

ENTRYPOINT ["dubforge"]
CMD ["demo", "--out-dir", "/data/demo"]
