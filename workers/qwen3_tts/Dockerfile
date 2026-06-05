# Базовый образ с PyTorch 2.5.1 + CUDA 12.4 (совместим с RTX 3070)
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/models/huggingface
ENV PYTHONUNBUFFERED=1
ENV PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ffmpeg libsndfile1 build-essential sox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем requirements и устанавливаем ТОЛЬКО дополнительные пакеты
# (torch уже есть в образе — не переустанавливаем!)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY core/ ./core/
COPY qwen3_worker.py ./qwen3_worker.py
COPY entrypoint.sh ./entrypoint.sh

# Создание рабочих директорий
RUN mkdir -p /app/data/voices /app/data/outputs /models

# Настройка entrypoint
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]