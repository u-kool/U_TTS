#!/usr/bin/env python3
import os
import sys
import json
import hashlib
import time
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

os.environ.setdefault('CUDA_LAUNCH_BLOCKING', '1')
from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# Split text helper (inline to avoid missing module dependency)
def _split_text(text, sent_window=100, max_window=120, min_chunk=20):
    sentences = text.replace("!", ".").replace("?", ".").replace("\n", ".").split(".")
    sentences = [s.strip() for s in sentences if s.strip()]
    chunks = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) + 1 <= max_window:
            buf = (buf + " " + s).strip()
        else:
            if buf and len(buf) >= min_chunk:
                chunks.append(buf)
            buf = s
    if buf and len(buf) >= min_chunk:
        chunks.append(buf)
    if not chunks and text.strip():
        chunks = [text.strip()[:max_window]]
    return chunks

from core.qwen3tts_engine import Qwen3TTSEngine

PROJECT_ROOT = Path(__file__).parent.parent.parent
if os.environ.get("TTS_DATA_ROOT"):
    _DATA_ROOT = Path(os.environ["TTS_DATA_ROOT"])
else:
    _exe_dir = Path(sys.executable).parent
    if (_exe_dir / "data" / "outputs").exists():
        _DATA_ROOT = _exe_dir
    else:
        _DATA_ROOT = PROJECT_ROOT
OUTPUTS_DIR = _DATA_ROOT / "data" / "outputs"
VOICES_DIR = _DATA_ROOT / "data" / "voices"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)

try:
    with open(PROJECT_ROOT / "data" / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except:
    config = {}

engine = Qwen3TTSEngine(
    model_type=config.get("qwen3_model_type", "CustomVoice"),
    model_name=config.get("qwen3_model_name", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
    device_map=config.get("qwen3_device_map", "cuda:0"),
    language=config.get("qwen3_language", "Russian"),
    speaker=config.get("qwen3_speaker", "Vivian"),
    dtype=config.get("qwen3_dtype", "bfloat16"),
    clone_dtype=config.get("qwen3_clone_dtype", "bfloat16"),
    max_window=config.get("qwen3_max_window", 80),
)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
print("Loading Qwen3 model...")
_ = engine.model
engine.wait_ready(180)
print(f"Qwen3 worker ready on port {5002}")

@app.route("/generate", methods=["POST"])
def generate():
    data = request.json
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    try:
        output_path = engine.generate(
            text=text,
            voice=data.get("voice"),
            language=data.get("language"),
            speaker=data.get("speaker"),
            instruct=data.get("instruct", ""),
        )
        return jsonify({"success": True, "filename": Path(output_path).name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/generate_chunks", methods=["POST"])
def generate_chunks():
    data = request.json
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400

    ignore_chars = data.get("ignore_chars", "")
    max_new_tokens = data.get("max_new_tokens", 512)
    max_window = data.get("max_window") or config.get("qwen3_max_window")

    def generate():
        try:
            for chunk_path in engine.generate_chunks(
                text=text,
                voice=data.get("voice"),
                language=data.get("language"),
                speaker=data.get("speaker"),
                instruct=data.get("instruct", ""),
                ref_text=data.get("ref_text"),
                ignore_chars=ignore_chars,
                max_new_tokens=max_new_tokens,
                max_window=max_window,
            ):
                yield json.dumps({"file": Path(chunk_path).name}) + "\n"
        except Exception as e:
            logger.error(f"Qwen3 generate_chunks failed: {e}")
            yield json.dumps({"error": str(e)}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

@app.route("/generate_bytes", methods=["POST"])
def generate_bytes():
    data = request.json
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    try:
        audio_bytes = engine.generate_bytes(
            text=text,
            voice=data.get("voice"),
            language=data.get("language"),
            speaker=data.get("speaker"),
            instruct=data.get("instruct", ""),
            ref_text=data.get("ref_text"),
        )
        return Response(audio_bytes, mimetype="audio/wav")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/list_clones", methods=["GET"])
def list_clones():
    return jsonify(engine.list_clones())

@app.route("/upload_clone", methods=["POST"])
def upload_clone():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    filename = secure_filename(file.filename)
    dest = VOICES_DIR / filename
    file.save(str(dest))
    return jsonify({"status": "ok", "filename": filename, "voice": f"qwen3-clone:{filename}"})

@app.route("/configure", methods=["POST"])
def configure():
    data = request.json or {}
    voices_dir = data.get("voices_dir")
    outputs_dir = data.get("outputs_dir")
    if voices_dir:
        p = Path(voices_dir)
        if not (p.exists() or p.resolve().exists()):
            # Convert Windows host path (e.g. G:\dist\data\voices) → container path (/app/data/voices)
            if "\\" in voices_dir or ":" in voices_dir:
                normalized = voices_dir.replace("\\", "/")
                idx = normalized.find("/data/")
                if idx != -1:
                    p = Path("/app" + normalized[idx:])
        if p.exists() or p.resolve().exists():
            p = p if p.exists() else p.resolve()
            p.mkdir(parents=True, exist_ok=True)
            engine.voices_dir = p
            global VOICES_DIR
            VOICES_DIR = p
    if outputs_dir:
        p = Path(outputs_dir)
        if not (p.exists() or p.resolve().exists()):
            if "\\" in outputs_dir or ":" in outputs_dir:
                normalized = outputs_dir.replace("\\", "/")
                idx = normalized.find("/data/")
                if idx != -1:
                    p = Path("/app" + normalized[idx:])
        if p.exists() or p.resolve().exists():
            p = p if p.exists() else p.resolve()
            p.mkdir(parents=True, exist_ok=True)
            engine.outputs_dir = p
            global OUTPUTS_DIR
            OUTPUTS_DIR = p
    return jsonify({"status": "ok"})

@app.route("/test_clone", methods=["POST"])
def test_clone():
    data = request.json
    voice = data.get("voice")
    text = data.get("text", "Тест клонирования голоса.")
    ref_text = data.get("ref_text")
    audio_bytes = engine.generate_bytes(text=text, voice=voice, ref_text=ref_text)
    filename = f"clone_test_{hashlib.md5(f'{text}{voice}{time.time()}'.encode()).hexdigest()[:10]}.wav"
    out_path = OUTPUTS_DIR / filename
    with open(out_path, "wb") as f:
        f.write(audio_bytes)
    return jsonify({"filename": filename})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)