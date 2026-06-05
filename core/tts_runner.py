import logging
import queue
import random
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_tts_param(value: str, suffix: str = "%") -> str:
    if not value:
        return f"+0{suffix}"
    value = value.strip()
    if value.startswith("+") or value.startswith("-"):
        return value
    return f"+{value}"


class TTSRunner:
    def __init__(self, engine, get_config, log_callback, event_callback,
                 max_queue_size: int = 200, concurrency_limit: int = 1,
                 xtts_worker=None, qwen3_worker=None, cosyvoice_worker=None,
                 piper_engine=None, sapi5_worker=None):
        self.engine = engine
        self.xtts_worker = xtts_worker
        self.qwen3_worker = qwen3_worker
        self.cosyvoice_worker = cosyvoice_worker
        self.piper_engine = piper_engine
        self.sapi5_worker = sapi5_worker
        self.get_config = get_config
        self.log_callback = log_callback
        self.event_callback = event_callback
        self.task_queue = queue.Queue(maxsize=max_queue_size)
        self.semaphore = threading.BoundedSemaphore(value=concurrency_limit)
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.task_queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def queue_size(self) -> int:
        return self.task_queue.qsize()

    def enqueue(self, text: str, voice: str = None, rate: str = None,
                volume: str = None, pitch: str = None, engine: str = None,
                **kwargs) -> bool:
        cfg = self.get_config()
        if not cfg.get("tts_enabled", True):
            return False
        try:
            self.task_queue.put_nowait({
                "text": text,
                "voice": voice,
                "rate": rate,
                "volume": volume,
                "pitch": pitch,
                "engine": engine,
                "kwargs": kwargs,
            })
            return True
        except queue.Full:
            logger.warning("TTS queue is full; dropping message")
            return False

    def _process(self, task):
        text = task["text"]
        voice = task["voice"]
        rate = task["rate"]
        volume = task["volume"]
        pitch = task["pitch"]
        engine_name = task.get("engine")
        kwargs = task["kwargs"]
        cfg = self.get_config()

        with self.semaphore:
            try:
                use_qwen3 = engine_name == "qwen3" and self.qwen3_worker is not None
                use_xtts = engine_name == "xtts" and self.xtts_worker is not None
                use_cosyvoice = engine_name == "cosyvoice" and self.cosyvoice_worker is not None
                use_piper = engine_name == "piper" and self.piper_engine is not None
                use_sapi5 = engine_name == "sapi5" and self.sapi5_worker is not None

                if use_sapi5:
                    s_voice = voice or cfg.get("sapi5_voice", "")
                    out = self.sapi5_worker.generate(
                        text=text,
                        voice=s_voice,
                        rate=int(cfg.get("sapi5_rate", 0)),
                        volume=float(cfg.get("sapi5_volume", 1.0)),
                    )
                    fname = Path(out).name
                    logger.info(f"SAPI5: {text[:50]}... -> {fname}")
                    self.log_callback("system", f"Озвучено: {text[:60]}...")
                    self.event_callback({"event": "new_audio", "filename": fname, "timestamp": time.time()})
                elif use_cosyvoice:
                    c_voice = voice or cfg.get("cosyvoice_voice", "")
                    c_lang = kwargs.get("language") or cfg.get("cosyvoice_language", "Russian")
                    c_instruct = kwargs.get("instruct") or cfg.get("cosyvoice_instruct", "")
                    c_ignore = cfg.get("ignore_chars", "")
                    c_delay = float(cfg.get("cosyvoice_initial_delay", 0))
                    _first = True
                    for fname in self.cosyvoice_worker.generate_chunks(
                        text=text, voice=c_voice, language=c_lang,
                        instruct=c_instruct, ignore_chars=c_ignore,
                    ):
                        if _first and c_delay > 0:
                            _first = False
                            logger.info(f"CosyVoice initial delay {c_delay}s")
                            time.sleep(c_delay)
                        logger.info(f"CosyVoice chunk: {text[:50]}... -> {fname}")
                        self.log_callback("system", f"Озвучено: {text[:60]}...")
                        self.event_callback({"event": "new_audio", "filename": fname, "timestamp": time.time()})
                elif use_qwen3:
                    q_voice = voice or cfg.get("qwen3_voice", "Vivian")
                    q_lang = kwargs.get("language") or cfg.get("qwen3_language", "Russian")
                    q_instruct = kwargs.get("instruct") or cfg.get("qwen3_instruct", "")
                    q_ignore = cfg.get("ignore_chars", "")
                    q_max_window = cfg.get("qwen3_max_window")
                    for fname in self.qwen3_worker.generate_chunks(
                        text=text, voice=q_voice, language=q_lang,
                        instruct=q_instruct, ignore_chars=q_ignore,
                        max_window=q_max_window,
                    ):
                        logger.info(f"Qwen3 chunk: {text[:50]}... -> {fname}")
                        self.log_callback("system", f"Озвучено: {text[:60]}...")
                        self.event_callback({"event": "new_audio", "filename": fname, "timestamp": time.time()})
                elif use_xtts:
                    lang = kwargs.get("language") or cfg.get("xtts_language", "ru")
                    temp = float(kwargs.get("temperature") or random.uniform(0.3, 1.0))
                    rep_p = float(kwargs.get("repetition_penalty") or cfg.get("xtts_repetition_penalty", 20))
                    speed = float(kwargs.get("speed") or cfg.get("xtts_speed", 1.0))
                    voice_to_use = voice or cfg.get("xtts_voice", "ref.wav")
                    for fname in self.xtts_worker.generate_chunks(
                        text=text, voice=voice_to_use, language=lang,
                        temperature=temp, repetition_penalty=rep_p,
                        speed=speed,
                    ):
                        logger.info(f"XTTS chunk: {text[:50]}... -> {fname}")
                        self.log_callback("system", f"Озвучено: {text[:60]}...")
                        self.event_callback({"event": "new_audio", "filename": fname, "timestamp": time.time()})
                elif use_piper:
                    # Предобработка текста для Piper
                    try:
                        from core.piper_preprocessor import preprocess_text
                        processed_text = preprocess_text(text)
                    except ImportError:
                        logger.warning("Piper preprocessor not available, using raw text")
                        processed_text = text
                    p_voice = voice or cfg.get("piper_voice", "")
                    # Убираем префикс "piper-", если он есть
                    if p_voice.startswith("piper-"):
                        p_voice = p_voice[6:]
                    fname = self.piper_engine.synthesize_to_file(
                        text=processed_text,
                        voice=p_voice,
                        speaker_id=kwargs.get("speaker_id"),
                    )
                    logger.info(f"Piper: {text[:50]}... -> {fname}")
                    self.log_callback("system", f"Озвучено: {text[:60]}...")
                    self.event_callback({"event": "new_audio", "filename": fname, "timestamp": time.time()})
                else:
                    raw_voice = voice or cfg.get("voice", "")
                    voice = raw_voice or "ru-RU-SvetlanaNeural"
                    rate = rate or normalize_tts_param(cfg.get("rate", "+0%"), "%")
                    volume = volume or normalize_tts_param(cfg.get("volume", "+0%"), "%")
                    pitch = pitch or normalize_tts_param(cfg.get("pitch", "+0Hz"), "Hz")
                    out = self.engine.generate(text=text, voice=voice, rate=rate, volume=volume, pitch=pitch)
                    fname = Path(out).name
                    logger.info(f"TTS: {text[:50]}... -> {fname}")
                    self.log_callback("system", f"Озвучено: {text[:60]}...")
                    self.event_callback({"event": "new_audio", "filename": fname, "timestamp": time.time()})
            except Exception as e:
                logger.error(f"TTS error: {e}")
                self.log_callback("error", f"TTS Error: {e}")

    def _worker(self):
        logger.info("TTS worker started")
        while not self._stop.is_set():
            try:
                task = self.task_queue.get(timeout=1)
            except queue.Empty:
                continue
            if task is None:
                break
            self._process(task)
        logger.info("TTS worker stopped")