#!/usr/bin/env python3
import os
import sys
import json
import hashlib
import time
import logging
from pathlib import Path

os.environ.setdefault('CUDA_LAUNCH_BLOCKING', '0')
from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

from core.xtts_engine import XTTSv2Engine

PROJECT_ROOT = Path(__file__).parent.parent.parent

app = Flask(__name__)
OUTPUTS_DIR = PROJECT_ROOT / "data" / "outputs"
VOICES_DIR = PROJECT_ROOT / "data" / "voices"
LATENTS_DIR = PROJECT_ROOT / "data" / "latents"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)
LATENTS_DIR.mkdir(parents=True, exist_ok=True)

try:
    with open(PROJECT_ROOT / "data" / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except:
    config = {}

engine = XTTSv2Engine(
    voice=config.get("xtts_voice", "ref.wav"),
    language=config.get("xtts_language", "ru"),
    temperature=config.get("xtts_temperature", 0.85),
    repetition_penalty=config.get("xtts_repetition_penalty", 20),
    half_precision=config.get("xtts_half_precision", False),
    speed=config.get("xtts_speed", 1.0),
)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
print("Loading XTTS model...")
_ = engine.model
print(f"XTTS worker ready on port {5004}")


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


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
            temperature=data.get("temperature"),
            repetition_penalty=data.get("repetition_penalty"),
            speed=data.get("speed"),
        )
        return jsonify({"success": True, "filename": Path(output_path).name})
    except Exception as e:
        logger.error(f"XTTS /generate error: {e}")
        return jsonify({"error": str(e)}), 500


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
            temperature=data.get("temperature"),
            repetition_penalty=data.get("repetition_penalty"),
            speed=data.get("speed"),
        )
        return Response(audio_bytes, mimetype="audio/wav")
    except Exception as e:
        logger.error(f"XTTS /generate_bytes error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/generate_chunks", methods=["POST"])
def generate_chunks():
    data = request.json
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400

    def generate():
        try:
            for chunk_path in engine.generate_chunks(
                text=text,
                voice=data.get("voice"),
                language=data.get("language"),
                temperature=data.get("temperature"),
                repetition_penalty=data.get("repetition_penalty"),
                speed=data.get("speed"),
            ):
                yield json.dumps({"file": Path(chunk_path).name}) + "\n"
        except Exception as e:
            logger.error(f"XTTS generate_chunks failed: {e}")
            yield json.dumps({"error": str(e)}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/list_clones", methods=["GET"])
def list_clones():
    return jsonify(engine.list_voices())


@app.route("/configure", methods=["POST"])
def configure():
    data = request.json or {}
    voices_dir = data.get("voices_dir")
    outputs_dir = data.get("outputs_dir")
    latents_dir = data.get("latents_dir")

    def _resolve(p, candidate):
        if p.exists() or p.resolve().exists():
            p = p if p.exists() else p.resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        # Convert Windows host path → container path (/app/data/...)
        if "\\" in candidate or ":" in candidate:
            normalized = candidate.replace("\\", "/")
            idx = normalized.find("/data/")
            if idx != -1:
                cp = Path("/app" + normalized[idx:])
                if cp.exists() or cp.resolve().exists():
                    cp = cp if cp.exists() else cp.resolve()
                    cp.mkdir(parents=True, exist_ok=True)
                    return cp
        return None

    if voices_dir:
        p = _resolve(Path(voices_dir), voices_dir)
        if p:
            engine.voices_dir = p
            global VOICES_DIR
            VOICES_DIR = p
    if outputs_dir:
        p = _resolve(Path(outputs_dir), outputs_dir)
        if p:
            engine.outputs_dir = p
            global OUTPUTS_DIR
            OUTPUTS_DIR = p
    if latents_dir:
        p = _resolve(Path(latents_dir), latents_dir)
        if p:
            engine.latents_dir = p
            global LATENTS_DIR
            LATENTS_DIR = p
    return jsonify({"status": "ok"})

@app.route("/list_languages", methods=["GET"])
def list_languages():
    return jsonify(engine.list_languages())


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
    return jsonify({"status": "ok", "filename": filename, "voice": f"xtts-{filename}"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004, debug=False, threaded=True)
