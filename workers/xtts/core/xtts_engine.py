# core/xtts_engine.py
import os
import json
import time
import hashlib
import re
import io
import logging
import threading
import requests
from pathlib import Path
from typing import Optional, List, Generator

logger = logging.getLogger(__name__)

MODEL_REPO = "coqui/XTTS-v2"
MODEL_VERSION = "v2.0.2"
MODEL_FILES = {
    "config.json": f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_VERSION}/config.json?download=true",
    "model.pth": f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_VERSION}/model.pth?download=true",
    "dvae.pth": f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_VERSION}/dvae.pth?download=true",
    "mel_stats.pth": f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_VERSION}/mel_stats.pth?download=true",
    "speakers_xtts.pth": f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_VERSION}/speakers_xtts.pth?download=true",
    "vocab.json": f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_VERSION}/vocab.json?download=true",
}

LANGS = [
    "ar", "zh-cn", "cs", "nl", "en", "fr", "de", "hu",
    "hi", "it", "ja", "ko", "pl", "pt", "ru", "es", "tr",
]

def remove_trailing_punctuation(text):
    return text.rstrip('.!?,;:')


def split_text(text, sent_window=100, max_window=120, min_chunk=20):
    sent_end = frozenset('.!?')
    any_punct = frozenset('.!?;,:\u2014\u2013\u2012"\'-\u00ab\u00bb')
    result = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        remaining = len(text) - pos
        if remaining <= min_chunk:
            if result:
                result[-1] += ' ' + text[pos:].strip()
            else:
                result.append(text[pos:].strip())
            break
        end = min(pos + sent_window, len(text))
        sent_split = -1
        for j in range(end - 1, pos - 1, -1):
            ch = text[j]
            if ch == '.' and (j + 1 >= len(text) or (j + 1 < len(text) and text[j + 1] == '.')):
                continue
            if ch in sent_end and j + 1 < len(text):
                sent_split = j + 1
                break
        if sent_split > 0:
            ahead = len(text) - sent_split
            if ahead < min_chunk and ahead > 0:
                result.append(text[pos:].strip())
                break
            if sent_split - pos >= min_chunk:
                result.append(text[pos:sent_split].strip())
                pos = sent_split
                while pos < len(text) and text[pos] in ' \t\n\r':
                    pos += 1
                continue
        end = min(pos + max_window, len(text))
        punct_split = -1
        for j in range(end - 1, pos - 1, -1):
            if text[j] in any_punct and j + 1 < len(text):
                punct_split = j + 1
                break
        if punct_split > 0:
            ahead = len(text) - punct_split
            if ahead < min_chunk and ahead > 0:
                result.append(text[pos:].strip())
                break
            if punct_split - pos >= min_chunk:
                result.append(text[pos:punct_split].strip())
                pos = punct_split
                while pos < len(text) and text[pos] in ' \t\n\r':
                    pos += 1
                continue
        space_split = text.rfind(' ', pos, min(pos + max_window, len(text)))
        if space_split > 0:
            ahead = len(text) - space_split
            if ahead < min_chunk and ahead > 0:
                result.append(text[pos:].strip())
                break
            if space_split - pos >= min_chunk:
                result.append(text[pos:space_split].strip())
                pos = space_split + 1
                while pos < len(text) and text[pos] in ' \t\n\r':
                    pos += 1
                continue
        result.append(text[pos:pos + sent_window].strip())
        pos += sent_window
        while pos < len(text) and text[pos] in ' \t\n\r':
            pos += 1
    return result


class XTTSv2Engine:
    def __init__(self, voice: str = "female_01.wav", language: str = "ru",
                 temperature: float = 0.85, repetition_penalty: float = 20,
                 half_precision: bool = False, speed: float = 1.0,
                 backend_url: str = None):
        self.voice = voice
        self.language = language
        self.temperature = temperature
        self.repetition_penalty = repetition_penalty
        self.half_precision = half_precision
        self.speed = speed
        self._model = None
        self._device = None
        self._lock = threading.Lock()
        
        _worker_root = Path(__file__).parent.parent
        _project_root = _worker_root.parent.parent
        if os.environ.get("TTS_DATA_ROOT"):
            _project_root = Path(os.environ["TTS_DATA_ROOT"])
        
        self.model_dir = _worker_root / "models" / "xttsv2_2.0.2"
        self.voices_dir = _project_root / "data" / "voices"
        self.outputs_dir = _project_root / "data" / "outputs"
        self.latents_dir = _project_root / "data" / "latents"
        self.outputs_dir.mkdir(exist_ok=True)
        self.voices_dir.mkdir(exist_ok=True)
        self.latents_dir.mkdir(exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)

    @property
    def model(self):
        if self._model is None:
            self._load_model()
        return self._model

    @property
    def device(self):
        if self._device is None:
            self._ensure_torch()
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"XTTS device: {self._device}")
        return self._device

    def is_model_downloaded(self) -> bool:
        return all(((self.model_dir / f).exists() and (self.model_dir / f).stat().st_size > 0) for f in MODEL_FILES)

    def download_progress(self) -> dict:
        total = len(MODEL_FILES)
        downloaded = sum(1 for f in MODEL_FILES if (self.model_dir / f).exists())
        sizes = {}
        for f in MODEL_FILES:
            p = self.model_dir / f
            sizes[f] = p.stat().st_size if p.exists() else 0
        return {"total": total, "downloaded": downloaded, "sizes": sizes}

    def download_model(self, progress_callback=None):
        for filename, url in MODEL_FILES.items():
            dest = self.model_dir / filename
            if dest.exists() and dest.stat().st_size > 0:
                continue
            logger.info(f"Downloading {filename}...")
            downloaded = 0
            total = 0
            # Determine existing partial size for resume
            if dest.exists():
                downloaded = dest.stat().st_size
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                    if downloaded > 0:
                        headers["Range"] = f"bytes={downloaded}-"
                    r = requests.get(url, stream=True, timeout=(30, 120), headers=headers)
                    if r.status_code == 416:
                        # Range not satisfiable - file is complete
                        break
                    if downloaded > 0 and r.status_code == 206:
                        total = int(r.headers.get("content-length", 0)) + downloaded
                    else:
                        r.raise_for_status()
                        total = int(r.headers.get("content-length", 0))
                        downloaded = 0
                    mode = "ab" if downloaded > 0 else "wb"
                    with open(dest, mode) as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback:
                                    progress_callback(filename, downloaded, total)
                    break
                except Exception as e:
                    logger.warning(f"Download {filename} attempt {attempt+1} failed: {e}")
                    if attempt < max_retries - 1:
                        import time as _time
                        _time.sleep(3 * (attempt + 1))
                    else:
                        logger.error(f"Failed to download {filename} after {max_retries} attempts")
                        raise
        logger.info("Model download complete")

    def _ensure_torch(self):
        missing = []
        try:
            import torch
        except ImportError:
            missing.append("torch")
        try:
            import torchaudio
        except ImportError:
            missing.append("torchaudio")
        try:
            import TTS
        except ImportError:
            missing.append("TTS")
        if missing:
            raise ImportError(
                f"Missing: {', '.join(missing)}. "
                "Install: pip install TTS torch torchaudio\n"
                "Requires Python 3.9-3.11 and CUDA-capable GPU (or CPU, very slow)."
            )
        # Check transformers compatibility
        try:
            import transformers
            from packaging import version
            if version.parse(transformers.__version__) >= version.parse("4.41.0"):
                raise ImportError(
                    f"Transformers {transformers.__version__} is too new for TTS. "
                    "Downgrade: pip install transformers==4.40.2"
                )
        except ImportError:
            pass
        except Exception:
            pass

    def _load_model(self):
        logger.info("Importing torch/TTS dependencies (may take 30-60s)...")
        self._ensure_torch()
        with self._lock:
            if self._model is not None:
                return
            if not self.is_model_downloaded():
                raise RuntimeError("Model not downloaded. Call download_model() first.")
            try:
                from TTS.tts.configs.xtts_config import XttsConfig
                from TTS.tts.models.xtts import Xtts
            except ImportError as e:
                raise ImportError(
                    f"Coqui TTS not installed ({e}). Run: pip install TTS torch torchaudio"
                )
            # PyTorch 2.6+ defaults weights_only=True, breaking TTS checkpoint loading
            import torch
            if not hasattr(torch, '_weights_only_patched'):
                _orig_torch_load = torch.load
                def _patched_torch_load(f, map_location=None, **kwargs):
                    kwargs.setdefault('weights_only', False)
                    return _orig_torch_load(f, map_location=map_location, **kwargs)
                torch.load = _patched_torch_load
                torch._weights_only_patched = True
            # torchaudio load/save work natively with cu124 build
            logger.info("Loading XTTSv2 model...")
            config_path = self.model_dir / "config.json"
            config = XttsConfig()
            config.load_json(str(config_path))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(
                config,
                checkpoint_dir=str(self.model_dir),
                vocab_path=str(self.model_dir / "vocab.json"),
                use_deepspeed=False,
            )
            model.to(self.device)
            self._model = model
            logger.info("XTTSv2 model loaded (FP32)")

    def _get_voice_path(self, voice: str) -> Path:
        p = self.voices_dir / voice
        if not p.exists():
            raise FileNotFoundError(f"Voice file not found: {p}")
        return p

    def _latent_cache_key(self, voice_path: Path) -> str:
        stat = voice_path.stat()
        raw = f"{voice_path.name}{stat.st_size}{stat.st_mtime}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_latents(self, voice: str):
        voice_path = self._get_voice_path(voice)
        cache_key = self._latent_cache_key(voice_path)
        cache_path = self.latents_dir / f"{cache_key}.pt"
        if cache_path.exists():
            import torch
            data = torch.load(cache_path, map_location=self.device, weights_only=False)
            logger.info(f"Loaded cached latents for {voice}")
            return data["gpt_cond_latent"], data["speaker_embedding"]
        m = self.model
        gpt_cond_latent, speaker_embedding = m.get_conditioning_latents(
            audio_path=[str(voice_path)],
            gpt_cond_len=m.config.gpt_cond_len,
            max_ref_length=m.config.max_ref_len,
            sound_norm_refs=m.config.sound_norm_refs,
        )
        import torch
        torch.save({"gpt_cond_latent": gpt_cond_latent, "speaker_embedding": speaker_embedding}, cache_path)
        logger.info(f"Cached latents for {voice} -> {cache_path.name}")
        return gpt_cond_latent, speaker_embedding

    def generate(self, text: str, voice: Optional[str] = None,
                 language: Optional[str] = None,
                 temperature: Optional[float] = None,
                 repetition_penalty: Optional[float] = None,
                 speed: Optional[float] = None,
                 **kwargs) -> str:
        import torch
        import torchaudio
        voice = voice or self.voice
        language = language or self.language
        temperature = temperature if temperature is not None else self.temperature
        repetition_penalty = repetition_penalty if repetition_penalty is not None else self.repetition_penalty
        m = self.model
        gpt_cond_latent, speaker_embedding = self._get_latents(voice)
        temperature = max(temperature, 0.01)
        repetition_penalty = max(repetition_penalty, 1.0)
        if self.half_precision:
            with torch.amp.autocast(device_type=self.device, dtype=torch.float16):
                output = m.inference(
                    text=text,
                    language=language,
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=temperature,
                    length_penalty=float(m.config.length_penalty),
                    repetition_penalty=repetition_penalty,
                    top_k=int(m.config.top_k),
                    top_p=float(m.config.top_p),
                    speed=speed if speed is not None else self.speed,
                    enable_text_splitting=len(text) > 180,
                    max_new_tokens=300,
                )
        else:
            output = m.inference(
                text=text,
                language=language,
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=temperature,
                length_penalty=float(m.config.length_penalty),
                repetition_penalty=repetition_penalty,
                top_k=int(m.config.top_k),
                top_p=float(m.config.top_p),
                speed=speed if speed is not None else self.speed,
                enable_text_splitting=len(text) > 180,
                max_new_tokens=300,
            )
        file_hash = hashlib.md5(f"{text}{voice}{language}{time.time()}".encode()).hexdigest()[:10]
        output_path = self.outputs_dir / f"tts_{file_hash}.wav"
        torchaudio.save(str(output_path), torch.tensor(output["wav"]).unsqueeze(0).float(), 24000)
        logger.info(f"XTTS generated: {text[:50]}... -> {output_path.name}")
        return str(output_path)

    def generate_chunks(self, text: str, voice: Optional[str] = None,
                        language: Optional[str] = None,
                        temperature: Optional[float] = None,
                        repetition_penalty: Optional[float] = None,
                        speed: Optional[float] = None):
        """Split text into chunks, generate each separately, yield file paths."""
        import torch
        import torchaudio
        voice = voice or self.voice
        language = language or self.language
        temperature = temperature if temperature is not None else self.temperature
        repetition_penalty = repetition_penalty if repetition_penalty is not None else self.repetition_penalty
        m = self.model
        gpt_cond_latent, speaker_embedding = self._get_latents(voice)
        temperature = max(temperature, 0.01)
        repetition_penalty = max(repetition_penalty, 1.0)
        chunks = split_text(text)
        is_multi = len(chunks) > 1
        if is_multi:
            logger.info(f"Text split into {len(chunks)} chunks")
        for cidx, chunk in enumerate(chunks):
            chunk = remove_trailing_punctuation(chunk)
            if not chunk:
                continue
            logger.info(f"Chunk {cidx + 1}/{len(chunks)}: {chunk[:60]}...")
            if self.half_precision:
                with torch.amp.autocast(device_type=self.device, dtype=torch.float16):
                    output = m.inference(
                        text=chunk, language=language,
                        gpt_cond_latent=gpt_cond_latent, speaker_embedding=speaker_embedding,
                        temperature=temperature, length_penalty=float(m.config.length_penalty),
                        repetition_penalty=repetition_penalty, top_k=int(m.config.top_k), top_p=float(m.config.top_p),
                        speed=speed if speed is not None else self.speed,
                        enable_text_splitting=False, max_new_tokens=300,
                    )
            else:
                output = m.inference(
                    text=chunk, language=language,
                    gpt_cond_latent=gpt_cond_latent, speaker_embedding=speaker_embedding,
                    temperature=temperature, length_penalty=float(m.config.length_penalty),
                    repetition_penalty=repetition_penalty, top_k=int(m.config.top_k), top_p=float(m.config.top_p),
                    speed=speed if speed is not None else self.speed,
                    enable_text_splitting=False, max_new_tokens=300,
                )
            wav = torch.tensor(output["wav"]).unsqueeze(0).float()
            # Prepend ~1.5s silence to first chunk of multi-chunk text for smooth transition
            if is_multi and cidx == 0:
                silence = torch.zeros(1, int(24000 * 1.5))
                wav = torch.cat([silence, wav], dim=1)
            file_hash = hashlib.md5(f"{chunk}{voice}{language}{time.time()}{cidx}".encode()).hexdigest()[:10]
            output_path = self.outputs_dir / f"tts_{file_hash}.wav"
            torchaudio.save(str(output_path), wav, 24000)
            logger.info(f"  Chunk {cidx + 1} -> {output_path.name} ({wav.shape[1]/24000:.2f}s)")
            yield str(output_path)

    def generate_bytes(self, text: str, voice: Optional[str] = None,
                       language: Optional[str] = None,
                       temperature: Optional[float] = None,
                       repetition_penalty: Optional[float] = None,
                       speed: Optional[float] = None) -> bytes:
        import torch
        import torchaudio
        import io
        voice = voice or self.voice
        language = language or self.language
        temperature = temperature if temperature is not None else self.temperature
        repetition_penalty = repetition_penalty if repetition_penalty is not None else self.repetition_penalty
        m = self.model
        gpt_cond_latent, speaker_embedding = self._get_latents(voice)
        temperature = max(temperature, 0.01)
        repetition_penalty = max(repetition_penalty, 1.0)
        if self.half_precision:
            with torch.amp.autocast(device_type=self.device, dtype=torch.float16):
                output = m.inference(
                    text=text,
                    language=language,
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=temperature,
                    length_penalty=float(m.config.length_penalty),
                    repetition_penalty=repetition_penalty,
                    top_k=int(m.config.top_k),
                    top_p=float(m.config.top_p),
                    speed=speed if speed is not None else self.speed,
                    enable_text_splitting=len(text) > 180,
                    max_new_tokens=300,
                )
        else:
            output = m.inference(
                text=text,
                language=language,
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=temperature,
                length_penalty=float(m.config.length_penalty),
                repetition_penalty=repetition_penalty,
                top_k=int(m.config.top_k),
                top_p=float(m.config.top_p),
                speed=speed if speed is not None else self.speed,
                enable_text_splitting=len(text) > 180,
                max_new_tokens=300,
            )
        buf = io.BytesIO()
        torchaudio.save(buf, torch.tensor(output["wav"]).unsqueeze(0).float(), 24000, format="wav")
        wav_bytes = buf.getvalue()
        logger.info(f"XTTS generated bytes: {len(wav_bytes)} for '{text[:50]}...'")
        return wav_bytes

    def generate_stream(self, text: str, voice: Optional[str] = None,
                        language: Optional[str] = None,
                        temperature: Optional[float] = None,
                        repetition_penalty: Optional[float] = None,
                        speed: Optional[float] = None):
        import torch
        voice = voice or self.voice
        language = language or self.language
        temperature = temperature if temperature is not None else self.temperature
        repetition_penalty = repetition_penalty if repetition_penalty is not None else self.repetition_penalty
        m = self.model
        gpt_cond_latent, speaker_embedding = self._get_latents(voice)
        temperature = max(temperature, 0.01)
        repetition_penalty = max(repetition_penalty, 1.0)
        if self.half_precision:
            with torch.amp.autocast(device_type=self.device, dtype=torch.float16):
                output = m.inference_stream(
                    text=text,
                    language=language,
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=temperature,
                    length_penalty=float(m.config.length_penalty),
                    repetition_penalty=repetition_penalty,
                    top_k=int(m.config.top_k),
                    top_p=float(m.config.top_p),
                    enable_text_splitting=len(text) > 180,
                    speed=speed if speed is not None else self.speed,
                    stream_chunk_size=20,
                )
                for chunk in output:
                    yield chunk.cpu().numpy().tobytes()
        else:
            output = m.inference_stream(
                text=text,
                language=language,
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=temperature,
                length_penalty=float(m.config.length_penalty),
                repetition_penalty=repetition_penalty,
                top_k=int(m.config.top_k),
                top_p=float(m.config.top_p),
                enable_text_splitting=len(text) > 180,
                speed=speed if speed is not None else self.speed,
                stream_chunk_size=20,
            )
            for chunk in output:
                yield chunk.cpu().numpy().tobytes()

    def generate_stream_wav(self, text: str, voice: Optional[str] = None,
                             language: Optional[str] = None,
                             temperature: Optional[float] = None,
                             repetition_penalty: Optional[float] = None,
                             speed: Optional[float] = None):
        import struct
        sample_rate = 24000
        bits = 16
        channels = 1
        block_align = channels * bits // 8
        byte_rate = sample_rate * block_align
        yield struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', 0xFFFFFFFF, b'WAVE',
            b'fmt ', 16, 1, channels,
            sample_rate, byte_rate, block_align, bits,
            b'data', 0xFFFFFFFF)
        yield from self.generate_stream(
            text=text, voice=voice, language=language,
            temperature=temperature, repetition_penalty=repetition_penalty,
            speed=speed,
        )

    def list_voices(self) -> List[dict]:
        voices = []
        for f in sorted(self.voices_dir.iterdir()):
            if f.suffix.lower() in (".wav", ".mp3", ".ogg", ".flac"):
                voices.append({
                    "name": f.name,
                    "path": str(f),
                    "engine": "xtts",
                })
        return voices

    def list_languages(self) -> List[dict]:
        return [{"code": lang, "name": lang} for lang in LANGS]

    def is_ready(self) -> bool:
        return self._model is not None
