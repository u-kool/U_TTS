FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/models/huggingface
ENV PYTHONUNBUFFERED=1
ENV PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ffmpeg libsndfile1 build-essential sox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone the CosyVoice repo from GitHub instead of COPY from host
RUN git clone --depth 1 https://github.com/FunAudioLLM/CosyVoice.git /app/cosyvoice_repo

# Clone the Matcha-TTS submodule separately (submodule not initialized in depth=1 clone)
RUN mkdir -p /app/cosyvoice_repo/third_party && \
    git clone --depth 1 https://github.com/shivammehta25/Matcha-TTS.git /app/cosyvoice_repo/third_party/Matcha-TTS

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=60 setuptools
RUN pip install --no-cache-dir --default-timeout=60 -r requirements.txt

# Install openai-whisper separately with --no-build-isolation (needs setuptools at build time)
RUN pip install --no-cache-dir --default-timeout=60 "openai-whisper>=20240930,<20250101" --no-build-isolation

# Install flash-attn for optimized SDPA (sliding window attention)
RUN pip install --no-cache-dir --default-timeout=120 "flash-attn>=2.7.0,<3.0.0" --no-build-isolation

# Install x-transformers with its deps (no-deps to avoid torch conflicts)
RUN pip install --no-cache-dir x-transformers einx torch-einops-utils --no-deps

# Pre-install huggingface-hub for entrypoint model download
RUN pip install --no-cache-dir --default-timeout=60 huggingface-hub

# Copy worker code
COPY core/ ./core/
COPY cosyvoice_worker.py ./cosyvoice_worker.py
COPY entrypoint.sh ./entrypoint.sh

RUN mkdir -p /app/data/voices /app/data/outputs /models /app/pretrained_models

RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
