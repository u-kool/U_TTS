#!/usr/bin/env python3
import os
import sys
import json
import hashlib
import time
import logging
from pathlib import Path

# os.environ.setdefault('CUDA_LAUNCH_BLOCKING', '1')  # debug only: forces sync CUDA ops
from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
repo_dir = Path(__file__).parent / "cosyvoice_repo"
if repo_dir.exists():
    sys.path.insert(0, str(repo_dir))
    third_party = repo_dir / "third_party" / "Matcha-TTS"
    if third_party.exists():
        sys.path.insert(0, str(third_party))

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

from core.cosyvoice_engine import CosyVoiceEngine

_data_root_env = os.environ.get("TTS_DATA_ROOT")
PROJECT_ROOT = Path(_data_root_env) if _data_root_env else Path(__file__).parent.parent.parent
WORKER_ROOT = Path(__file__).parent

app = Flask(__name__)


def _parse_json_body():
    """Parse request JSON body, auto-detecting encoding (UTF-8 / UTF-16LE / UTF-16BE)."""
    if hasattr(request, '_parsed_json'):
        return request._parsed_json
    raw = request.get_data()
    if not raw:
        data = {}
    else:
        for enc in ('utf-8', 'utf-16-le', 'utf-16-be', 'utf-8-sig'):
            try:
                text = raw.decode(enc)
                data = json.loads(text)
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        else:
            data = request.json or {}
    request._parsed_json = data
    return data


def _jget(key, default=None):
    return _parse_json_body().get(key, default)
OUTPUTS_DIR = PROJECT_ROOT / "data" / "outputs"
VOICES_DIR = PROJECT_ROOT / "data" / "voices"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)

try:
    with open(PROJECT_ROOT / "data" / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except:
    config = {}

_default_model_dir = WORKER_ROOT / "pretrained_models" / "Fun-CosyVoice3-0.5B"

engine = CosyVoiceEngine(
    model_dir=config.get("cosyvoice_model_dir", str(_default_model_dir)),
    device_map=config.get("cosyvoice_device_map", "cuda:0"),
    fp16=config.get("cosyvoice_fp16", True),
    min_chars=config.get("cosyvoice_min_chars", 4),
    sent_window=config.get("cosyvoice_sent_window", 80),
    max_window=config.get("cosyvoice_max_window", 120),
    stream=config.get("cosyvoice_stream", False),
    cache_enabled=config.get("cosyvoice_cache_enabled", True),
    cache_maxsize=config.get("cosyvoice_cache_maxsize", 256),
    instruct_max_length=config.get("cosyvoice_instruct_max_length", 200),
    text_frontend=config.get("cosyvoice_text_frontend", True),
    deduplicate_chars=config.get("deduplicate_chars", False),
    first_chunk_buffer_sec=config.get("cosyvoice_first_chunk_buffer_sec", 4.0),
)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
print("Loading CosyVoice3 model...")
engine.load_async(wait=True)
print(f"CosyVoice3 worker ready on port {5003}")


@app.route("/generate", methods=["POST"])
def generate():
    text = _jget("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    try:
        output_path = engine.generate(
            text=text,
            voice=_jget("voice"),
            language=_jget("language"),
            instruct=_jget("instruct", ""),
            ref_text=_jget("ref_text"),
            ignore_chars=_jget("ignore_chars", ""),
        )
        return jsonify({"success": True, "filename": Path(output_path).name})
    except Exception as e:
        logger.error(f"CosyVoice /generate error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/generate_chunks", methods=["POST"])
def generate_chunks():
    text = _jget("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400

    ignore_chars = _jget("ignore_chars", "")

    def generate():
        try:
            for chunk_path in engine.generate_chunks(
                text=text,
                voice=_jget("voice"),
                language=_jget("language"),
                instruct=_jget("instruct", ""),
                ref_text=_jget("ref_text"),
                ignore_chars=ignore_chars,
            ):
                yield json.dumps({"file": Path(chunk_path).name}) + "\n"
        except Exception as e:
            logger.error(f"CosyVoice generate_chunks failed: {e}")
            yield json.dumps({"error": str(e)}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/generate_bytes", methods=["POST"])
def generate_bytes():
    text = _jget("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    try:
        audio_bytes = engine.generate_bytes(
            text=text,
            voice=_jget("voice"),
            language=_jget("language"),
            instruct=_jget("instruct", ""),
            ref_text=_jget("ref_text"),
            ignore_chars=_jget("ignore_chars", ""),
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
    return jsonify({"status": "ok", "filename": filename, "voice": f"cosyvoice-clone:{filename}"})


@app.route("/test_clone", methods=["POST"])
def test_clone():
    voice = _jget("voice")
    text = _jget("text", "Тест клонирования голоса.")
    ref_text = _jget("ref_text")
    audio_bytes = engine.generate_bytes(text=text, voice=voice, ref_text=ref_text)
    filename = f"clone_test_{hashlib.md5(f'{text}{voice}{time.time()}'.encode()).hexdigest()[:10]}.wav"
    out_path = OUTPUTS_DIR / filename
    with open(out_path, "wb") as f:
        f.write(audio_bytes)
    return jsonify({"filename": filename})


@app.route("/configure", methods=["POST"])
def configure():
    data = request.json or {}
    voices_dir = data.get("voices_dir")
    outputs_dir = data.get("outputs_dir")
    if voices_dir:
        p = Path(voices_dir)
        p.mkdir(parents=True, exist_ok=True)
        engine.voices_dir = p
        global VOICES_DIR
        VOICES_DIR = p
    if outputs_dir:
        p = Path(outputs_dir)
        p.mkdir(parents=True, exist_ok=True)
        engine.outputs_dir = p
        global OUTPUTS_DIR
        OUTPUTS_DIR = p
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("WORKER_PORT", 5003))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
