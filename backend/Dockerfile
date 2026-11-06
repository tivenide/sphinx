# Sphinx only needs FFmpeg's pitch-preserving atempo filter over PCM pipes.
# Build the official release with that small feature set instead of installing
# Debian's full multimedia stack (video codecs, desktop/GPU libraries, etc.).
FROM python:3.12.13-slim-trixie@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28 AS ffmpeg-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libc6-dev \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:e3963a50831c985933e1a625ed566ec4c7adb5c012c34fa9f84438e1d61bdacc \
    https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/refs/tags/n7.1.5 /tmp/ffmpeg.tar.gz
WORKDIR /src/ffmpeg
RUN tar -xzf /tmp/ffmpeg.tar.gz --strip-components=1 \
    && ./configure --disable-everything --disable-autodetect --disable-network \
        --disable-doc --disable-debug --disable-x86asm --disable-shared \
        --disable-avdevice --disable-postproc --disable-swscale \
        --disable-programs --enable-ffmpeg --enable-static --enable-small \
        --enable-protocol=pipe --enable-demuxer=pcm_s16le,wav --enable-muxer=pcm_s16le,wav \
        --enable-decoder=pcm_s16le --enable-encoder=pcm_s16le \
        --enable-filter=atempo,aresample,aformat,anull,abuffer,abuffersink \
    && (make -j4 ffmpeg > /tmp/ffmpeg-build.log 2>&1 || { tail -80 /tmp/ffmpeg-build.log; exit 1; }) \
    && strip ffmpeg

FROM python:3.12.13-slim-trixie@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
COPY --from=ffmpeg-build /src/ffmpeg/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-build /src/ffmpeg/COPYING.LGPLv2.1 /usr/local/share/licenses/ffmpeg/COPYING.LGPLv2.1
RUN groupadd --gid 10001 sphinx && useradd --uid 10001 --gid sphinx --no-create-home sphinx \
    && install -d -o sphinx -g sphinx /data
WORKDIR /app
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock
COPY app ./app
USER 10001:10001
EXPOSE 8000
# A single worker owns the bounded voice queue and session context.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*", "--ws", "websockets", "--ws-max-size", "2048", "--ws-max-queue", "4", "--ws-per-message-deflate", "false", "--no-access-log"]
