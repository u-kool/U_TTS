#!/usr/bin/env python3
"""
Download CosyVoice3 model from HuggingFace or ModelScope.
Usage: python download_cosyvoice_model.py
"""
import os
import sys
from pathlib import Path

MODEL_DIR = Path("models/Fun-CosyVoice3-0.5B")
MODEL_NAME = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"


def download_hf():
    try:
        from huggingface_hub import snapshot_download
        print(f"Downloading {MODEL_NAME} from HuggingFace...")
        snapshot_download(MODEL_NAME, local_dir=str(MODEL_DIR))
        print(f"Model downloaded to {MODEL_DIR}")
        return True
    except ImportError:
        print("huggingface_hub not installed")
    except Exception as e:
        print(f"HuggingFace download failed: {e}")
    return False


def download_ms():
    try:
        from modelscope import snapshot_download
        print(f"Downloading {MODEL_NAME} from ModelScope...")
        snapshot_download(MODEL_NAME, local_dir=str(MODEL_DIR))
        print(f"Model downloaded to {MODEL_DIR}")
        return True
    except ImportError:
        print("modelscope not installed")
    except Exception as e:
        print(f"ModelScope download failed: {e}")
    return False


if __name__ == "__main__":
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if download_hf():
        sys.exit(0)
    if download_ms():
        sys.exit(0)
    print("ERROR: Failed to download model. Install huggingface_hub or modelscope.")
    sys.exit(1)
