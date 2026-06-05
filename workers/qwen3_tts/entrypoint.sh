#!/bin/bash
set -e

echo "============================================="
echo " Qwen3-TTS Custom Worker"
echo "============================================="
echo " Model:  ${QWEN3_MODEL_NAME:-Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice}"
echo " Dtype:  ${QWEN3_DTYPE:-bfloat16}"
echo " Port:   ${WORKER_PORT:-5002}"
echo " HF Cache: ${HF_HOME}"
echo "============================================="

# Предзагрузка моделей (ускоряет первый запуск Flask)
echo "[1/2] Ensuring models are cached..."
if command -v hf &> /dev/null; then
    hf download "${QWEN3_MODEL_NAME:-Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice}" --quiet || true
    hf download Qwen/Qwen3-TTS-Tokenizer-12Hz --quiet || true
elif command -v huggingface-cli &> /dev/null; then
    huggingface-cli download "${QWEN3_MODEL_NAME:-Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice}" --quiet || true
    huggingface-cli download Qwen/Qwen3-TTS-Tokenizer-12Hz --quiet || true
fi

echo "[2/2] Starting Flask worker..."
exec python3 /app/qwen3_worker.py