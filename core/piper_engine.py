# core/piper_engine.py
import hashlib
import json
import logging
import struct
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 22050, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Конвертирует сырой PCM в WAV с заголовком."""
    if not pcm_data:
        return b''
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_size = len(pcm_data)
    wav_header = struct.pack('<4sI4s', b'RIFF', 36 + data_size, b'WAVE')
    wav_header += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample)
    wav_header += struct.pack('<4sI', b'data', data_size)
    return wav_header + pcm_data


class PiperEngine:
    def __init__(self, exe_path: Path, voices_dir: Path, default_voice: str = "ru_RU-mari-medium_epoch6399"):
        self.exe_path = exe_path
        self.voices_dir = voices_dir
        self.default_voice = default_voice
        self._available_voices = None

    def _find_model_path(self, voice_name: str) -> Path:
        """Возвращает путь к ONNX-файлу по имени голоса (с или без .onnx)."""
        if voice_name.endswith(".onnx"):
            model_path = self.voices_dir / voice_name
        else:
            model_path = self.voices_dir / f"{voice_name}.onnx"
        if not model_path.exists():
            raise FileNotFoundError(f"Piper voice model not found: {model_path}")
        return model_path

    def synthesize(self, text: str, voice: Optional[str] = None, speaker_id: Optional[int] = None) -> bytes:
        """
        Синтезирует текст в WAV (байты).
        """
        voice = voice or self.default_voice
        # Убираем префикс "piper-", если есть
        if voice.startswith("piper-"):
            voice = voice[6:]
        model_path = self._find_model_path(voice)

        cmd = [str(self.exe_path), "-m", str(model_path), "--output_raw"]
        if speaker_id is not None:
            cmd += ["--speaker", str(speaker_id)]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            # Передаём текст в кодировке UTF-8
            pcm_data, stderr = proc.communicate(input=text.encode('utf-8'), timeout=30)

            if proc.returncode != 0:
                error_msg = stderr.decode() if stderr else "unknown error"
                logger.error(f"Piper process failed (code {proc.returncode}): {error_msg}")
                raise RuntimeError(f"Piper synthesis failed: {error_msg}")

            wav_data = pcm_to_wav(pcm_data)
            return wav_data

        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("Piper synthesis timeout")
        except Exception as e:
            logger.error(f"Piper synthesis exception: {e}")
            raise

    def synthesize_to_file(self, text: str, voice: Optional[str] = None, speaker_id: Optional[int] = None) -> str:
        """
        Синтезирует текст, сохраняет WAV-файл в data/outputs и возвращает имя файла.
        """
        wav_data = self.synthesize(text, voice, speaker_id)

        outputs_dir = Path("data/outputs")
        outputs_dir.mkdir(parents=True, exist_ok=True)
        filename = f"piper_{hashlib.md5(f'{text}{voice}{time.time()}'.encode()).hexdigest()[:12]}.wav"
        out_path = outputs_dir / filename
        with open(out_path, "wb") as f:
            f.write(wav_data)
        return filename

    def list_voices(self) -> List[Dict]:
        """Возвращает список доступных голосов с метаданными."""
        if self._available_voices is not None:
            return self._available_voices

        voices = []
        for onnx_file in sorted(self.voices_dir.glob("*.onnx")):
            voice_name = onnx_file.stem
            json_file = onnx_file.with_suffix(".onnx.json")
            lang = "unknown"
            if json_file.exists():
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    lang = meta.get("language", {}).get("code", "unknown")
                except Exception:
                    pass
            voices.append({
                "name": f"piper-{voice_name}",
                "engine": "piper",
                "language": lang,
                "gender": "unknown",
                "type": "local"
            })
        self._available_voices = voices
        return voices