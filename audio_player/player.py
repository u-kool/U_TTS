#!/usr/bin/env python3
import sys
import time
import threading
import logging
from pathlib import Path
from queue import Queue, Empty
from flask import Flask, request, jsonify, send_file

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
except ImportError:
    logger.error("Install dependencies: pip install sounddevice soundfile numpy")
    sys.exit(1)

HOST = '127.0.0.1'
PORT = 5006

if getattr(sys, 'frozen', False):
    _DATA_ROOT = Path(sys.executable).parent.resolve()
    _ASSETS_ROOT = Path(sys._MEIPASS).resolve()
else:
    _DATA_ROOT = Path(__file__).parent.parent.resolve()
    _ASSETS_ROOT = _DATA_ROOT

OUTPUTS_DIR = _DATA_ROOT / "data" / "outputs"
ICON_PATH = _ASSETS_ROOT / "icons" / "icon.ico"

# Глобальные состояния
audio_queue = Queue()
current_volume = 50  # 0-100
stop_worker = False
is_playing = False
play_lock = threading.Lock()

app = Flask(__name__)


def audio_worker():
    """Поток воспроизведения аудио через SoundDevice."""
    global stop_worker, is_playing

    while not stop_worker:
        try:
            file_path = audio_queue.get(timeout=0.5)
        except Empty:
            continue

        if not Path(file_path).exists():
            logger.warning(f"File not found: {file_path}")
            audio_queue.task_done()
            continue

        try:
            with play_lock:
                is_playing = True

            data, samplerate = sf.read(file_path, dtype='float32')
            
            # Нормализация громкости
            volume_factor = current_volume / 100.0
            data = data * volume_factor

            logger.info(f"Playing: {Path(file_path).name} ({samplerate}Hz)")
            
            # Блокирующее воспроизведение; прерывается при вызове sd.stop()
            sd.play(data, samplerate=samplerate)
            sd.wait()

        except Exception as e:
            logger.error(f"Playback error: {e}")
        finally:
            with play_lock:
                is_playing = False
            audio_queue.task_done()

    logger.info("Audio worker stopped")


@app.route('/play', methods=['POST'])
def play():
    data = request.get_json()
    if not data or 'file' not in data:
        return jsonify({"error": "Missing 'file'"}), 400

    file_name = data['file']
    file_path = OUTPUTS_DIR / file_name

    if not file_path.exists():
        # Fallback: если frozen EXE, проверить source data/outputs (на случай ручного запуска воркеров)
        alt_path = None
        if getattr(sys, 'frozen', False):
            alt_path = Path(sys.executable).parent.parent / "data" / "outputs" / file_name
        if alt_path and alt_path.exists():
            file_path = alt_path
        else:
            return jsonify({"error": f"File not found: {file_name}"}), 404

    audio_queue.put(str(file_path))
    logger.info(f"Queued: {file_name}")
    return jsonify({"status": "queued"})


@app.route('/volume', methods=['POST'])
def set_volume():
    global current_volume
    data = request.get_json()
    if not data or 'volume' not in data:
        return jsonify({"error": "Missing 'volume'"}), 400

    try:
        vol = int(data['volume'])
        vol = max(0, min(100, vol))
        current_volume = vol
        
        # Примечание: SoundDevice не поддерживает изменение громкости 
        # на лету для уже играющего потока без пересчета данных.
        # Новая громкость применится к следующему файлу в очереди.
        
        logger.info(f"Volume set to {vol}")
        return jsonify({"status": "ok", "volume": vol})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/favicon.ico')
def favicon():
    if ICON_PATH.exists():
        return send_file(str(ICON_PATH), mimetype='image/x-icon')
    return "", 204

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200


@app.route('/status', methods=['GET'])
def status():
    with play_lock:
        playing = is_playing
    return jsonify({
        "queue_size": audio_queue.qsize(),
        "volume": current_volume,
        "is_playing": playing
    })


@app.route('/stop', methods=['POST'])
def stop_route():
    """Останавливает текущее воспроизведение и очищает очередь."""
    try:
        sd.stop()
    except Exception:
        pass
    
    # Очистка очереди
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
            audio_queue.task_done()
        except Empty:
            break
            
    logger.info("Playback stopped and queue cleared")
    return jsonify({"status": "stopped"})


def run_player():
    threading.Thread(target=audio_worker, daemon=True).start()
    logger.info(f"Audio Player (SoundDevice) on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    run_player()