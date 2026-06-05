#!/bin/bash
set -e

echo "========================================"
echo " CosyVoice3 Worker"
echo " Model:  FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
echo " Port:   ${WORKER_PORT:-5003}"
echo " HF Cache: ${HF_HOME}"
echo "========================================"

MODEL_DIR="/app/pretrained_models/Fun-CosyVoice3-0.5B"

echo "[1/3] Cleaning stale cache/lock files from previous downloads..."
find "$MODEL_DIR" -name "*.lock" -o -name "*.incomplete" -o -name "*.metadata" 2>/dev/null | while read f; do
    echo "  rm: $(basename $(dirname $f))/$(basename $f)"
    rm -f "$f"
done
# Also clean huggingface cache lock files if any
if [ -d "/root/.cache/huggingface" ]; then
    find "/root/.cache/huggingface" -name "*.lock" -o -name "*.incomplete" 2>/dev/null -delete
fi

echo "[2/3] Ensuring model is cached..."
# Check for a real model file (llm.pt is a large essential file, ~2GB)
if [ ! -d "$MODEL_DIR" ] || [ ! -f "$MODEL_DIR/llm.pt" ] || [ ! -f "$MODEL_DIR/flow.pt" ]; then
    echo "  Model not found or incomplete, downloading from HuggingFace..."
    # Remove incomplete download to ensure clean re-download
    rm -rf "$MODEL_DIR"
    mkdir -p "$MODEL_DIR"
    if command -v huggingface-cli &> /dev/null; then
        huggingface-cli download FunAudioLLM/Fun-CosyVoice3-0.5B-2512 --local-dir "$MODEL_DIR" --resume-download
    elif python3 -c "from huggingface_hub import snapshot_download" 2>/dev/null; then
        python3 -c "
from huggingface_hub import snapshot_download
import os
md = '$MODEL_DIR'
if not os.path.exists(md) or not os.path.exists(os.path.join(md, 'cosyvoice3.yaml')):
    snapshot_download('FunAudioLLM/Fun-CosyVoice3-0.5B-2512', local_dir=md)
" 2>&1
    else
        echo "  WARNING: huggingface_hub not available, model may not be found"
    fi
else
    echo "  Model already cached at $MODEL_DIR"
fi

echo "[3/3] Starting Flask worker..."
exec python3 /app/cosyvoice_worker.py
