FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/data/outputs /app/data/latents

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --prefer-binary \
        TTS==0.22.0 \
        "transformers<4.47" \
        "Flask>=3.1.3" \
        "soundfile>=0.12.1" \
        "requests>=2.31.0"

# core + download script — редко меняются, модель кэшируется
COPY workers/xtts/core/ /app/workers/xtts/core/
COPY workers/xtts/download_xtts_model.py /app/workers/xtts/download_xtts_model.py
WORKDIR /app/workers/xtts
RUN python download_xtts_model.py

# Код воркера — меняется часто, копируем после модели
COPY workers/xtts/xtts_worker.py /app/workers/xtts/xtts_worker.py
COPY data/config.json /app/data/config.json
COPY data/voices/ /app/data/voices/

ENV PYTHONPATH=/app

EXPOSE 5004

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5004/health')" || exit 1

CMD ["python", "/app/workers/xtts/xtts_worker.py"]
