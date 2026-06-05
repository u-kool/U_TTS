import hashlib
import time
import sys
import logging
from pathlib import Path
from typing import List, Generator

logger = logging.getLogger(__name__)

if getattr(sys, 'frozen', False):
    _DATA_ROOT = Path(sys.executable).parent.resolve()
else:
    _DATA_ROOT = Path(__file__).resolve().parent.parent

try:
    import pyttsx3
    import pythoncom
    HAS_SAPI5 = True
except ImportError:
    HAS_SAPI5 = False


class TTSEngine:
    FALLBACK_VOICES = [
        {"name": "ru-RU-SvetlanaNeural", "gender": "female", "locale": "ru-RU"},
        {"name": "ru-RU-DmitryNeural", "gender": "male", "locale": "ru-RU"},
        {"name": "en-US-JennyNeural", "gender": "female", "locale": "en-US"},
        {"name": "en-US-GuyNeural", "gender": "male", "locale": "en-US"},
    ]

    def __init__(self, voice: str = "ru-RU-SvetlanaNeural"):
        self.voice = voice or "ru-RU-SvetlanaNeural"
        self.outputs_dir = _DATA_ROOT / "data" / "outputs"
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, text, voice=None, rate="+0%", volume="+0%", pitch="+0Hz", **kwargs) -> str:
        voice = voice or self.voice
        if not voice:
            voice = "ru-RU-SvetlanaNeural"
        file_hash = hashlib.md5(f"{text}{voice}{time.time()}".encode()).hexdigest()[:10]
        output_path = self.outputs_dir / f"tts_{file_hash}.mp3"
        try:
            _run_edge_async("save", text, voice, rate, volume, pitch, str(output_path))
            return str(output_path)
        except Exception as e:
            logger.error(f"edge-tts failed: {e}")
            raise RuntimeError(f"TTS generation failed: {e}")

    def generate_stream(self, text, voice=None, rate="+0%", volume="+0%", pitch="+0Hz") -> Generator[bytes, None, None]:
        voice = voice or self.voice or "ru-RU-SvetlanaNeural"
        file_hash = hashlib.md5(f"{text}{voice}{time.time()}".encode()).hexdigest()[:10]
        output_path = self.outputs_dir / f"tts_{file_hash}.mp3"
        try:
            _run_edge_async("save", text, voice, rate, volume, pitch, str(output_path))
            CHUNK_SIZE = 4096
            with open(output_path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e:
            logger.error(f"edge-tts stream failed: {e}")
            raise

    def list_voices(self) -> List[dict]:
        try:
            raw_voices = _run_edge_async("voices")
            voices = []
            for v in raw_voices:
                voices.append({
                    "name": v["ShortName"],
                    "gender": v["Gender"].lower(),
                    "locale": v["Locale"],
                })
            if voices:
                return voices
        except Exception as e:
            logger.warning(f"edge-tts list_voices error: {e}")
        return self.FALLBACK_VOICES

    def is_ready(self) -> bool:
        try:
            _run_edge_async("health")
            return True
        except Exception:
            return False


def _run_edge_async(cmd, *args):
    import asyncio, edge_tts
    async def _save(text, voice, rate, vol, pitch, out):
        await edge_tts.Communicate(text, voice, rate=rate, volume=vol, pitch=pitch, connect_timeout=5).save(out)
    async def _voices():
        return await edge_tts.list_voices()
    async def _health():
        await edge_tts.list_voices()
    try:
        if cmd == "save":
            asyncio.run(_save(*args))
        elif cmd == "voices":
            return asyncio.run(_voices())
        elif cmd == "health":
            asyncio.run(_health())
    except Exception as e:
        raise RuntimeError(f"edge-tts {cmd} failed: {e}")


class SAPI5Engine:
    def __init__(self):
        self.outputs_dir = _DATA_ROOT / "data" / "outputs"
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_com(self):
        try:
            pythoncom.CoInitialize()
        except:
            pass

    def _new_engine(self):
        self._ensure_com()
        return pyttsx3.init(driverName='sapi5')

    def generate(self, text, voice=None, rate=0, volume=1.0, **kwargs) -> str:
        file_hash = hashlib.md5(f"{text}{voice}{time.time()}".encode()).hexdigest()[:10]
        output_path = self.outputs_dir / f"sapi5_{file_hash}.wav"
        engine = None
        try:
            self._ensure_com()
            engine = self._new_engine()
            voice_set = False
            if voice:
                for v in engine.getProperty('voices'):
                    v_name = (v.name or '').strip()
                    v_id = (v.id or '').strip()
                    if voice.strip() in (v_name, v_id) or voice.strip() in v_name or voice.strip() in v_id:
                        engine.setProperty('voice', v.id)
                        voice_set = True
                        logger.info(f"SAPI5 voice set to '{v_name}' (id={v_id})")
                        break
                if not voice_set:
                    logger.warning(f"SAPI5 voice '{voice}' not found, using default")
            if rate != 0:
                current_rate = engine.getProperty('rate')
                engine.setProperty('rate', current_rate + rate)
            if volume != 1.0:
                engine.setProperty('volume', volume)
            engine.save_to_file(text, str(output_path))
            engine.runAndWait()
            logger.info(f"SAPI5 TTS: {text[:50]}... -> {output_path.name}")
            return str(output_path)
        except Exception as e:
            logger.error(f"SAPI5 TTS generation error: {e}")
            raise RuntimeError(f"SAPI5 TTS failed: {e}")
        finally:
            if engine:
                try:
                    engine.stop()
                except:
                    pass

    def list_voices(self) -> list:
        if not HAS_SAPI5:
            return []
        engine = None
        try:
            self._ensure_com()
            engine = self._new_engine()
            voices = []
            for v in engine.getProperty('voices'):
                voices.append({
                    "name": f"sapi5-{v.name}",
                    "gender": "unknown",
                    "locale": v.languages[0] if v.languages else "unknown",
                    "engine": "sapi5"
                })
            return voices
        except Exception as e:
            logger.warning(f"SAPI5 list_voices error: {e}")
            return []
        finally:
            if engine:
                try:
                    engine.stop()
                except:
                    pass

    def is_ready(self) -> bool:
        return HAS_SAPI5
