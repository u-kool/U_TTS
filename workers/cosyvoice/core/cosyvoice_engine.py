import os
import io
import re
import time
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, List

import torch
import torchaudio
import numpy as np

from core.text_splitter import split_text

logger = logging.getLogger(__name__)

COSYVOICE3_MODEL = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
COSYVOICE3_MODEL_DIR = "models/CosyVoice3/Fun-CosyVoice3-0.5B"

SUPPORTED_LANGUAGES = [
    "Chinese", "English", "Japanese", "Korean",
    "German", "French", "Russian", "Portuguese",
    "Spanish", "Italian",
]

CLONE_EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac")
MIN_AUDIO_SAMPLES = 256


class CosyVoiceEngine:
    def __init__(self, model_dir: str = None,
                 device_map: str = "cuda:0",
                 language: str = "Russian",
                 fp16: bool = True,
                 min_chars: int = 4,
                 sent_window: int = 80,
                 max_window: int = 120,
                 stream: bool = False,
                 cache_enabled: bool = True,
                 cache_maxsize: int = 256,
                 instruct_max_length: int = 200,
                 text_frontend: bool = True,
                 deduplicate_chars: bool = False,
                 first_chunk_buffer_sec: float = 4.0):
        self.model_dir = model_dir or COSYVOICE3_MODEL_DIR
        self.device_map = device_map
        self.language = language
        self.fp16 = fp16
        self.min_chars = min_chars
        self.sent_window = sent_window
        self.max_window = max_window
        self.stream = stream
        self.cache_enabled = cache_enabled
        self.cache_maxsize = cache_maxsize
        self.instruct_max_length = instruct_max_length
        self.text_frontend = text_frontend
        self.deduplicate_chars = deduplicate_chars
        self.first_chunk_buffer_sec = min(max(first_chunk_buffer_sec, 0.5), 30.0)
        self._model = None
        self._lock = threading.Lock()
        
        _project_root = Path(__file__).parent.parent.parent.parent
        if os.environ.get("TTS_DATA_ROOT"):
            _project_root = Path(os.environ["TTS_DATA_ROOT"])
        self.voices_dir = _project_root / "data" / "voices"
        self.outputs_dir = _project_root / "data" / "outputs"
        self.outputs_dir.mkdir(exist_ok=True)
        self.voices_dir.mkdir(exist_ok=True)
        self._load_complete = threading.Event()
        self.sample_rate = 24000

        self._cache = {}
        self._cache_lock = threading.Lock()

    @property
    def model(self):
        if self._model is None:
            self._load_model()
        return self._model

    def _load_model(self):
        with self._lock:
            if self._model is not None:
                return
            logger.info(f"Loading CosyVoice3 model from {self.model_dir}...")
            from cosyvoice.cli.cosyvoice import AutoModel
            self._model = AutoModel(
                model_dir=self.model_dir,
                fp16=self.fp16,
            )
            self.sample_rate = self._model.sample_rate
            self._load_complete.set()
            logger.info(f"CosyVoice3 model loaded (sample_rate={self.sample_rate})")

    def load_async(self, wait=False):
        self._load_complete.clear()

        def _load():
            try:
                self.model
                self._load_complete.set()
                logger.info("CosyVoice3 model ready")
            except Exception as e:
                logger.error(f"Failed to load CosyVoice3 model: {e}")
                self._load_complete.set()

        threading.Thread(target=_load, daemon=True).start()
        if wait:
            self._load_complete.wait(timeout=120)

    def wait_ready(self, timeout=120):
        self._load_complete.wait(timeout)

    def is_ready(self) -> bool:
        return self._model is not None

    def unload(self):
        with self._lock:
            if self._model is not None:
                try:
                    del self._model
                    self._model = None
                    logger.info("CosyVoice3 model unloaded")
                except Exception as e:
                    logger.error(f"Error unloading CosyVoice3 model: {e}")
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
        except Exception:
            pass
        self._load_complete.clear()

    def _get_ref_text(self, audio_path: str) -> Optional[str]:
        p = Path(audio_path)
        txt_path = p.with_suffix(".txt")
        if txt_path.exists() and txt_path.is_file():
            try:
                text = txt_path.read_text(encoding="utf-8").strip()
                if text:
                    logger.info(f"Loaded ref_text from {txt_path.name}")
                    return text
            except Exception as e:
                logger.warning(f"Failed to read ref_text from {txt_path}: {e}")
        return None

    def _validate_ref_audio(self, path: str) -> bool:
        p = Path(path)
        if not p.exists() or not p.is_file():
            logger.error(f"Reference audio not found: {path}")
            return False
        if p.stat().st_size < 1024:
            logger.error(f"Reference audio too small (<1KB): {path}")
            return False
        if p.suffix.lower() not in CLONE_EXTENSIONS:
            logger.error(f"Unsupported audio format: {path}")
            return False
        try:
            import soundfile as sf
            with sf.SoundFile(str(p)) as f:
                if f.frames < 1000:
                    logger.warning(f"Reference audio too short: {p.name} ({f.frames} frames)")
                    return False
                dur = f.frames / f.samplerate
                if dur > 28:
                    logger.warning(f"Reference audio {p.name} is {dur:.1f}s, will be truncated to 30s at 16kHz")
        except Exception as e:
            logger.error(f"Cannot read reference audio {path}: {e}")
            return False
        return True

    def _get_ref_audio(self, voice: str) -> Optional[str]:
        if voice.startswith("cosyvoice-spk:"):
            return self.voices_dir / voice[len("cosyvoice-spk:"):]
        if voice.startswith("cosyvoice-clone:"):
            filename = voice[len("cosyvoice-clone:"):]
            path = self.voices_dir / filename
            if not path.exists():
                raise FileNotFoundError(f"Voice file not found: {path}")
            if not self._validate_ref_audio(str(path)):
                raise ValueError(f"Invalid reference audio: {path}")
            return str(path)
        return None

    def list_voices(self) -> List[dict]:
        voices = []
        for f in sorted(self.voices_dir.iterdir()):
            if f.suffix.lower() in CLONE_EXTENSIONS:
                txt_path = f.with_suffix(".txt")
                desc = txt_path.read_text(encoding="utf-8").strip() if txt_path.exists() else ""
                voices.append({
                    "name": f"cosyvoice-clone:{f.name}",
                    "path": str(f),
                    "engine": "cosyvoice",
                    "type": "clone",
                    "description": desc,
                })
        return voices

    def list_clones(self) -> List[dict]:
        clones = []
        for f in sorted(self.voices_dir.iterdir()):
            if f.suffix.lower() in CLONE_EXTENSIONS:
                clones.append({
                    "name": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
        return clones

    def delete_clone(self, filename: str) -> bool:
        path = self.voices_dir / filename
        if path.exists() and path.suffix.lower() in CLONE_EXTENSIONS:
            path.unlink()
            txt_path = path.with_suffix(".txt")
            if txt_path.exists():
                txt_path.unlink()
            logger.info(f"Deleted clone: {filename}")
            return True
        return False

    def list_languages(self) -> List[dict]:
        return [{"code": lang, "name": lang} for lang in SUPPORTED_LANGUAGES]

    # ---- short chunk helpers ----

    def _merge_short_chunks(self, chunks: List[str]) -> List[str]:
        if not chunks:
            return chunks
        merged = []
        i = 0
        while i < len(chunks):
            if len(chunks[i]) < self.min_chars and i + 1 < len(chunks):
                candidate = chunks[i] + " " + chunks[i + 1]
                if len(candidate) <= self.max_window:
                    merged.append(candidate)
                    i += 2
                    continue
            merged.append(chunks[i])
            i += 1
        return merged

    def _pad_audio(self, audio: torch.Tensor) -> torch.Tensor:
        if audio.shape[-1] < MIN_AUDIO_SAMPLES:
            pad_size = MIN_AUDIO_SAMPLES - audio.shape[-1]
            audio = torch.nn.functional.pad(audio, (0, pad_size), mode='constant', value=0.0)
        return audio

    # ---- caching ----

    def _cache_key(self, text: str, voice: Optional[str], instruct: Optional[str], language: Optional[str]) -> str:
        return hashlib.md5(f"{text}|{voice}|{instruct}|{language}".encode()).hexdigest()

    def _cache_get(self, key: str):
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_set(self, key: str, audio: torch.Tensor):
        if not self.cache_enabled:
            return
        if len(self._cache) >= self.cache_maxsize:
            with self._cache_lock:
                if len(self._cache) >= self.cache_maxsize:
                    self._cache.clear()
        with self._cache_lock:
            self._cache[key] = audio

    # ---- inference ----

    def _split_sentences(self, text: str) -> List[str]:
        # Split by sentence punctuation; each sentence is one chunk.
        # If no punctuation found, force-split by sent_window.
        for ch in ("!", "?", "\n"):
            text = text.replace(ch, ".")
        sents = [s.strip() for s in text.split(".") if s.strip()]
        if not sents:
            return [text]
        if len(sents) == 1:
            # No punctuation was found — force-split by character count
            result = []
            if len(sents[0]) > self.sent_window:
                for i in range(0, len(sents[0]), self.sent_window):
                    chunk = sents[0][i:i + self.sent_window].strip()
                    if chunk:
                        result.append(chunk)
                return result
            return sents
        return sents

    @torch.inference_mode()
    def _infer(self, model, text: str, ref_audio: Optional[str], ref_text: Optional[str], instruct: Optional[str]) -> torch.Tensor:
        prompt_text = ("<|endofprompt|> " + ref_text) if ref_text else "<|endofprompt|>"
        logger.info(f"CosyVoice3 | zero_shot | text='{text[:80]}' | ref_audio={ref_audio} | prompt_text_len={len(prompt_text)}")
        sents = self._split_sentences(text)
        if len(sents) > 1:
            logger.info(f"CosyVoice3 | split into {len(sents)} chunks (max_window={self.max_window})")
        chunks = []
        for sent in sents:
            for result in model.inference_zero_shot(
                tts_text=sent,
                prompt_text=prompt_text,
                prompt_wav=ref_audio,
                stream=self.stream,
                text_frontend=False,
            ):
                audio = result['tts_speech']
                if audio is not None:
                    chunks.append(audio)

        if not chunks:
            raise RuntimeError("CosyVoice3: model returned None for all results")

        if len(chunks) == 1:
            return chunks[0]

        return torch.cat(chunks, dim=-1)

    def _infer_stream(self, model, text: str, ref_audio: Optional[str], ref_text: Optional[str], instruct: Optional[str]):
        prompt_text = ("<|endofprompt|> " + ref_text) if ref_text else "<|endofprompt|>"
        logger.info(f"CosyVoice3 | stream | text='{text[:80]}'")
        sents = self._split_sentences(text)
        for sent in sents:
            for result in model.inference_zero_shot(
                tts_text=sent,
                prompt_text=prompt_text,
                prompt_wav=ref_audio,
                stream=True,
                text_frontend=False,
            ):
                audio = result['tts_speech']
                if audio is not None:
                    yield self._pad_audio(audio)

    def _should_cache(self, text: str) -> bool:
        return self.cache_enabled and len(text) <= 50

    def _apply_ignore_chars(self, text: str, ignore_chars: str) -> str:
        if not ignore_chars:
            return text
        for ch in ignore_chars:
            text = text.replace(ch, '')
        return text

    def _deduplicate_chars(self, text: str) -> str:
        if not self.deduplicate_chars:
            return text
        return re.sub(r'(.)\1{2,}', r'\1', text)

    def _build_chunks(self, text: str, ignore_chars: str) -> List[str]:
        raw_chunks = split_text(text, sent_window=self.sent_window,
                                max_window=self.max_window, min_chunk=self.min_chars)
        if len(raw_chunks) <= 1:
            raw_chunks = self._merge_short_chunks(raw_chunks)
            raw_chunks = [c for c in raw_chunks if len(c) >= self.min_chars]
        if not raw_chunks:
            raw_chunks = [text.strip()]
        chunks = [self._apply_ignore_chars(c.strip(), ignore_chars) for c in raw_chunks if c.strip()]
        chunks = [self._deduplicate_chars(c) for c in chunks if c]
        return [c for c in chunks if c]

    def generate(self, text: str, voice: Optional[str] = None,
                 language: Optional[str] = None,
                 instruct: Optional[str] = None,
                 ref_text: Optional[str] = None,
                 ignore_chars: str = "",
                 **kwargs) -> str:
        text = text.strip()
        if not text:
            raise ValueError("Empty text")
        m = self.model
        ref_audio = self._get_ref_audio(voice) if voice else None

        if ref_audio:
            if not ref_text:
                ref_text = self._get_ref_text(ref_audio)

            ckey = self._cache_key(text, voice, instruct, language) if self._should_cache(text) else None
            cached = self._cache_get(ckey) if ckey else None
            if cached is not None:
                logger.info(f"CosyVoice3 | cache hit | text='{text[:60]}'")
                return self._save_audio(cached)

            chunks = self._build_chunks(text, ignore_chars)

            if not chunks:
                audio = self._infer(m, text, ref_audio, ref_text, instruct)
            elif len(chunks) == 1:
                audio = self._infer(m, chunks[0], ref_audio, ref_text, instruct)
            else:
                all_audio = []
                for chunk in chunks:
                    chunk_audio = self._infer(m, chunk, ref_audio, ref_text, instruct)
                    all_audio.append(chunk_audio)
                audio = torch.cat(all_audio, dim=-1)

            audio = self._pad_audio(audio)

            if ckey:
                self._cache_set(ckey, audio)

            return self._save_audio(audio)
        elif voice is None:
            default = self._get_default_voice()
            if default:
                return self.generate(text=text, voice=default,
                                     language=language, instruct=instruct, ref_text=ref_text, **kwargs)
            raise RuntimeError("No voice file found in data/voices/. CosyVoice3 requires a reference audio.")
        else:
            raise RuntimeError(f"Voice file not found: {voice}. Place reference audio in data/voices/.")

    def generate_bytes(self, text: str, voice: Optional[str] = None,
                       language: Optional[str] = None,
                       instruct: Optional[str] = None,
                       ref_text: Optional[str] = None,
                       ignore_chars: str = "",
                       **kwargs) -> bytes:
        text = text.strip()
        if not text:
            raise ValueError("Empty text")
        m = self.model
        ref_audio = self._get_ref_audio(voice) if voice else None

        if ref_audio:
            if not ref_text:
                ref_text = self._get_ref_text(ref_audio)

            ckey = self._cache_key(text, voice, instruct, language) if self._should_cache(text) else None
            cached = self._cache_get(ckey) if ckey else None
            if cached is not None:
                logger.info(f"CosyVoice3 | cache hit | text='{text[:60]}'")
                return self._audio_to_bytes(cached)

            chunks = self._build_chunks(text, ignore_chars)

            if not chunks:
                audio = self._infer(m, text, ref_audio, ref_text, instruct)
            elif len(chunks) == 1:
                audio = self._infer(m, chunks[0], ref_audio, ref_text, instruct)
            else:
                all_audio = []
                for chunk in chunks:
                    chunk_audio = self._infer(m, chunk, ref_audio, ref_text, instruct)
                    all_audio.append(chunk_audio)
                audio = torch.cat(all_audio, dim=-1)

            audio = self._pad_audio(audio)

            if ckey:
                self._cache_set(ckey, audio)

            return self._audio_to_bytes(audio)
        elif voice is None:
            default = self._get_default_voice()
            if default:
                return self.generate_bytes(text=text, voice=default,
                                           language=language, instruct=instruct, ref_text=ref_text, **kwargs)
            raise RuntimeError("No voice file found in data/voices/. CosyVoice3 requires a reference audio.")
        else:
            raise RuntimeError(f"Voice file not found: {voice}. Place reference audio in data/voices/.")

    def generate_chunks(self, text: str, voice: Optional[str] = None,
                        language: Optional[str] = None,
                        instruct: Optional[str] = None,
                        ref_text: Optional[str] = None,
                        ignore_chars: str = "",
                        **kwargs):
        text_chunks = self._build_chunks(text, ignore_chars)
        if not text_chunks:
            logger.warning("CosyVoice3 | empty or invalid text, generation skipped")
            return

        skipped = sum(1 for c in text_chunks if len(c) < self.min_chars)
        if skipped:
            logger.warning(f"CosyVoice3 | skipped {skipped} chunk(s) shorter than {self.min_chars} chars after merge")
        text_chunks = [c for c in text_chunks if len(c) >= self.min_chars]
        if not text_chunks:
            logger.warning("CosyVoice3 | all chunks too short after merge, generation skipped")
            return

        m = self.model
        ref_audio = self._get_ref_audio(voice) if voice else None

        if not ref_audio and voice is None:
            default = self._get_default_voice()
            if default:
                ref_audio = default
            else:
                logger.error("CosyVoice3 | no voice file found in data/voices/")
                return
        elif not ref_audio and voice:
            logger.error(f"CosyVoice3 | voice file not found: {voice}")
            return

        if not ref_text and ref_audio:
            ref_text = self._get_ref_text(ref_audio)

        t_start = time.time()
        step = 0
        buffer_samples = int(self.first_chunk_buffer_sec * self.sample_rate)

        for ci, text_chunk in enumerate(text_chunks, 1):
            ckey = self._cache_key(text_chunk, voice, instruct, language) if self._should_cache(text_chunk) else None
            cached = self._cache_get(ckey) if ckey else None
            if cached is not None:
                logger.info(f"CosyVoice3 | cache hit chunk {ci} | text='{text_chunk[:40]}'")
                path = self._save_audio(cached, prefix=f"cv_{ci}_cached")
                yield path
                continue

            stream = self._infer_stream(m, text_chunk, ref_audio, ref_text, instruct)

            if ci == 1 and buffer_samples > 0:
                # Accumulate first chunk up to buffer threshold for smoother playback start
                acc = []
                acc_len = 0
                threshold_reached = False
                for audio in stream:
                    if audio.shape[-1] < MIN_AUDIO_SAMPLES:
                        continue
                    if not threshold_reached:
                        acc.append(audio)
                        acc_len += audio.shape[-1]
                        if acc_len >= buffer_samples:
                            combined = torch.cat(acc, dim=-1)
                            step += 1
                            path = self._save_audio(combined, prefix=f"cv_s{step}")
                            logger.info(f"CosyVoice3 | 1/{len(text_chunks)} buffered {acc_len/self.sample_rate:.1f}s | text='{text_chunk[:40]}'")
                            yield path
                            threshold_reached = True
                        continue
                    # After threshold — stream remaining immediately
                    step += 1
                    path = self._save_audio(audio, prefix=f"cv_s{step}")
                    yield path

                if not threshold_reached and acc:
                    combined = torch.cat(acc, dim=-1)
                    step += 1
                    path = self._save_audio(combined, prefix=f"cv_s{step}")
                    logger.info(f"CosyVoice3 | 1/{len(text_chunks)} buffered {acc_len/self.sample_rate:.1f}s (end) | text='{text_chunk[:40]}'")
                    yield path
            else:
                # Chunks 2+: stream immediately without accumulation
                for audio in stream:
                    if audio.shape[-1] < MIN_AUDIO_SAMPLES:
                        continue
                    step += 1
                    path = self._save_audio(audio, prefix=f"cv_s{step}")
                    yield path

            if ckey:
                pass

        elapsed = time.time() - t_start
        logger.info(f"CosyVoice3 | generate_chunks done | text_chunks={len(text_chunks)} | steps={step} | total_ms={elapsed * 1000:.0f}")

    def _save_audio(self, audio_tensor: torch.Tensor, prefix: str = "cosyvoice") -> str:
        file_hash = hashlib.md5(f"{prefix}{time.time()}".encode()).hexdigest()[:10]
        output_path = self.outputs_dir / f"tts_{file_hash}.wav"
        torchaudio.save(str(output_path), audio_tensor.cpu(), self.sample_rate)
        return str(output_path)

    def _audio_to_bytes(self, audio_tensor: torch.Tensor) -> bytes:
        buf = io.BytesIO()
        torchaudio.save(buf, audio_tensor.cpu(), self.sample_rate, format="wav")
        return buf.getvalue()

    def _get_default_voice(self) -> str:
        for ext in CLONE_EXTENSIONS:
            for f in sorted(self.voices_dir.glob(f"*{ext}")):
                return str(f)
        return ""
