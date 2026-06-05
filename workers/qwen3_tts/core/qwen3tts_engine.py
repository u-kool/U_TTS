import os
import io
import time
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, List

import soundfile as sf
import numpy as np
import torch

from core.text_splitter import split_text

logger = logging.getLogger(__name__)

QWEN3_CUSTOMVOICE_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
QWEN3_BASE_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"

CUSTOMVOICE_SPEAKERS = [
    {"name": "Vivian", "description": "Bright young female voice", "native": "Chinese"},
    {"name": "Serena", "description": "Warm, gentle young female voice", "native": "Chinese"},
    {"name": "Uncle_Fu", "description": "Seasoned male voice, mellow timbre", "native": "Chinese"},
    {"name": "Dylan", "description": "Youthful Beijing male voice", "native": "Chinese"},
    {"name": "Eric", "description": "Lively Chengdu male voice", "native": "Chinese"},
    {"name": "Ryan", "description": "Dynamic male voice with rhythm", "native": "English"},
    {"name": "Aiden", "description": "Sunny American male voice", "native": "English"},
    {"name": "Ono_Anna", "description": "Playful Japanese female voice", "native": "Japanese"},
    {"name": "Sohee", "description": "Warm Korean female voice", "native": "Korean"},
]

SUPPORTED_LANGUAGES = [
    "Chinese", "English", "Japanese", "Korean",
    "German", "French", "Russian", "Portuguese",
    "Spanish", "Italian",
]

CLONE_EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac")


class Qwen3TTSEngine:
    def __init__(self, model_type: str = "CustomVoice",
                 model_name: str = None,
                 device_map: str = "cuda:0",
                 language: str = "Russian",
                 speaker: str = "Vivian",
                 instruct: str = "",
                 dtype: str = "bfloat16",
                 clone_dtype: str = "bfloat16",
                 max_window: int = 80):
        self.model_type = model_type
        self.model_name = model_name or (QWEN3_CUSTOMVOICE_MODEL if model_type == "CustomVoice" else QWEN3_BASE_MODEL)
        self.device_map = device_map
        self.language = language
        self.speaker = speaker
        self.instruct = instruct
        self.dtype = dtype
        self.clone_dtype = clone_dtype
        self.max_window = max_window
        self._model = None
        self._clone_model = None
        self._lock = threading.Lock()
        self._clone_lock = threading.Lock()
        
        if os.environ.get("TTS_DATA_ROOT"):
            _project_root = Path(os.environ["TTS_DATA_ROOT"])
        else:
            _project_root = Path(__file__).parent.parent.parent.parent
            _exe_dir = Path(sys.executable).parent
            if (_exe_dir / "data" / "outputs").exists():
                _project_root = _exe_dir
        self.voices_dir = _project_root / "data" / "voices"
        self.outputs_dir = _project_root / "data" / "outputs"
        self.outputs_dir.mkdir(exist_ok=True)
        self.voices_dir.mkdir(exist_ok=True)
        self._clone_cache = {}
        self._clone_cache_lock = threading.Lock()
        self._clone_cache_ttl = 3600
        self._load_complete = threading.Event()

    @property
    def model(self):
        if self._model is None:
            self._load_model()
        return self._model

    @property
    def clone_model(self):
        if self._clone_model is None:
            self._load_clone_model()
        return self._clone_model

    def is_model_downloaded(self) -> bool:
        try:
            import os
            from huggingface_hub import snapshot_download
            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
            model_slug = self.model_name.replace("/", "--")
            model_path = os.path.join(cache_dir, f"models--{model_slug}")
            return os.path.isdir(model_path)
        except Exception:
            return False

    def is_base_downloaded(self) -> bool:
        try:
            import os
            from huggingface_hub import snapshot_download
            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
            model_slug = QWEN3_BASE_MODEL.replace("/", "--")
            model_path = os.path.join(cache_dir, f"models--{model_slug}")
            return os.path.isdir(model_path)
        except Exception:
            return False

    def _ensure_deps(self):
        missing = []
        try:
            from faster_qwen3_tts import FasterQwen3TTS
        except ImportError:
            missing.append("faster-qwen3-tts")
        if missing:
            raise ImportError(
                f"Missing: {', '.join(missing)}. "
                "Install: pip install faster-qwen3-tts\n "
                "Requires Python 3.10+ and CUDA-capable GPU. "
            )

    def _load_model(self):
        self._ensure_deps()
        with self._lock:
            if self._model is not None:
                return
            from faster_qwen3_tts import FasterQwen3TTS

            torch.set_float32_matmul_precision('high')
            dtype_map = {
                "bfloat16": torch.bfloat16,
                "float16": torch.float16,
                "float32": torch.float32,
            }
            selected_dtype = dtype_map.get(self.dtype, torch.bfloat16)
            logger.info(f"Loading Qwen3-TTS CustomVoice model via faster-qwen3-tts: {QWEN3_CUSTOMVOICE_MODEL}")
            logger.info(f"  device={self.device_map}, dtype={self.dtype} ({selected_dtype})")

            if "cuda" in self.device_map:
                torch.backends.cudnn.benchmark = True
                if self.dtype in ("float16", "bfloat16"):
                    torch.backends.cuda.matmul.allow_tf32 = True

            model = FasterQwen3TTS.from_pretrained(
                QWEN3_CUSTOMVOICE_MODEL,
                device=self.device_map,
                dtype=selected_dtype,
                attn_implementation="sdpa",
            )
            self._model = model
            self._load_complete.set()
            logger.info("Qwen3-TTS CustomVoice model loaded (CUDA graphs pending first generation)")

    def _load_clone_model(self):
        self._ensure_deps()
        with self._clone_lock:
            if self._clone_model is not None:
                return
            from faster_qwen3_tts import FasterQwen3TTS

            torch.set_float32_matmul_precision('high')
            dtype_map = {
                "bfloat16": torch.bfloat16,
                "float16": torch.float16,
                "float32": torch.float32,
            }
            selected_dtype = dtype_map.get(self.clone_dtype, torch.bfloat16)
            logger.info(f"Loading Qwen3-TTS Base model via faster-qwen3-tts: {QWEN3_BASE_MODEL}")
            logger.info(f"  device={self.device_map}, dtype={self.clone_dtype} ({selected_dtype})")

            if "cuda" in self.device_map:
                torch.backends.cudnn.benchmark = True
                if self.clone_dtype in ("float16", "bfloat16"):
                    torch.backends.cuda.matmul.allow_tf32 = True

            model = FasterQwen3TTS.from_pretrained(
                QWEN3_BASE_MODEL,
                device=self.device_map,
                dtype=selected_dtype,
                attn_implementation="sdpa",
            )
            self._clone_model = model
            self._load_complete.set()
            logger.info("Qwen3-TTS Base (clone) model loaded (CUDA graphs pending first generation)")

            self._warmup_clone()

    def load_async(self, wait=False):
        self._load_complete.clear()
        def _load_both():
            try:
                self.model
                self._load_complete.wait()
                logger.info("CustomVoice model ready")
            except Exception as e:
                logger.error(f"Failed to load CustomVoice model: {e}")
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                self.clone_model
                self._load_complete.wait()
                logger.info("Base clone model ready")
            except Exception as e:
                logger.error(f"Failed to load Base clone model: {e}")
        threading.Thread(target=_load_both, daemon=True).start()
        if wait:
            self._load_complete.wait(timeout=60)

    def wait_ready(self, timeout=60):
        self._load_complete.wait(timeout)

    def unload(self):
        with self._lock:
            if self._model is not None:
                try:
                    del self._model
                    self._model = None
                    logger.info("Qwen3 CustomVoice model unloaded")
                except Exception as e:
                    logger.error(f"Error unloading CustomVoice model: {e}")
        with self._clone_lock:
            if self._clone_model is not None:
                try:
                    del self._clone_model
                    self._clone_model = None
                    logger.info("Qwen3 Base (clone) model unloaded")
                except Exception as e:
                    logger.error(f"Error unloading Base clone model: {e}")
        with self._clone_cache_lock:
            self._clone_cache.clear()
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
                logger.info("CUDA cache cleared after Qwen3 unload")
        except Exception as e:
            logger.debug(f"Could not clear CUDA cache: {e}")
        self._load_complete.clear()

    def list_voices(self) -> List[dict]:
        voices = []
        for spk in CUSTOMVOICE_SPEAKERS:
            voices.append({
                "name": f"qwen3-{spk['name']}",
                "description": spk["description"],
                "native": spk["native"],
                "engine": "qwen3",
                "type": "customvoice",
            })
        for f in sorted(self.voices_dir.iterdir()):
            if f.suffix.lower() in CLONE_EXTENSIONS:
                voices.append({
                    "name": f"qwen3-clone:{f.name}",
                    "path": str(f),
                    "engine": "qwen3",
                    "type": "clone",
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
            with self._clone_cache_lock:
                self._clone_cache = {k: v for k, v in self._clone_cache.items() if filename not in k}
            logger.info(f"Deleted clone: {filename}")
            return True
        return False

    def list_languages(self) -> List[dict]:
        return [{"code": lang, "name": lang} for lang in SUPPORTED_LANGUAGES]

    def _warmup_clone(self):
        m = self._clone_model
        if m is None:
            return
        ref_files = sorted(self.voices_dir.iterdir()) if self.voices_dir.exists() else []
        ref_audio = None
        for f in ref_files:
            if f.suffix.lower() in CLONE_EXTENSIONS:
                ref_audio = str(f)
                break
        if not ref_audio:
            logger.info("Warmup clone: no reference audio found, skipping")
            return
        logger.info(f"Warmup clone (CUDA graph capture): {ref_audio}")
        try:
            with torch.inference_mode():
                wavs, sr = m.generate_voice_clone(
                    text="Hello",
                    language="English",
                    ref_audio=ref_audio,
                    ref_text="",
                    xvec_only=True,
                    max_new_tokens=16,
                )
            logger.info("Warmup clone: CUDA graphs captured")
        except Exception as e:
            logger.warning(f"Warmup clone failed (non-fatal): {e}")

    def is_ready(self) -> bool:
        return self._model is not None

    def is_clone_ready(self) -> bool:
        return self._clone_model is not None

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
            with sf.SoundFile(str(p)) as f:
                if f.frames < 1000:
                    logger.warning(f"Reference audio too short: {p.name} ({f.frames} frames)")
                    return False
        except Exception as e:
            logger.error(f"Cannot read reference audio {path}: {e}")
            return False
        return True

    def _get_ref_audio(self, voice: str):
        if voice.startswith("qwen3-clone:"):
            filename = voice[len("qwen3-clone:"):]
            path = self.voices_dir / filename
            if not path.exists():
                raise FileNotFoundError(f"Voice file not found: {path}")
            if not self._validate_ref_audio(str(path)):
                raise ValueError(f"Invalid reference audio: {path}")
            return str(path)
        return None

    def _safe_extract_audio(self, audio_list: list) -> np.ndarray:
        arr = audio_list[0]
        if hasattr(arr, "cpu"):
            arr = arr.cpu().numpy()
        return np.array(arr, dtype=np.float32)

    def generate(self, text: str, voice: Optional[str] = None,
                 language: Optional[str] = None,
                 speaker: Optional[str] = None,
                 instruct: Optional[str] = None,
                 ref_text: Optional[str] = None,
                 enable_text_splitting: bool = False,
                 **kwargs) -> str:
        voice = voice or self.speaker
        language = language or self.language
        instruct = instruct or self.instruct
        speaker_name = speaker

        ref_audio = self._get_ref_audio(voice)
        if ref_audio:
            if not ref_text:
                ref_text = self._get_ref_text(ref_audio)
            m = self.clone_model
            ref_text = ref_text or ""
            logger.info(f"Qwen3 clone: ref_text={ref_text!r}, xvec_only=True")
            with torch.inference_mode():
                wavs, sr = m.generate_voice_clone(
                    text=text, language=language,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    xvec_only=True,
                )
        else:
            m = self.model
            if not speaker_name:
                spk = voice
                if spk.startswith("qwen3-"):
                    spk = spk[6:]
                speaker_name = spk
            if speaker_name not in [s["name"] for s in CUSTOMVOICE_SPEAKERS]:
                speaker_name = "Vivian"
            logger.info(f"Qwen3 custom: text_len={len(text)}, engine_dtype={self.dtype}")
            with torch.inference_mode():
                wavs, sr = m.generate_custom_voice(
                    text=text,
                    language=language,
                    speaker=speaker_name,
                    instruct=instruct,
                )

        audio_array = self._safe_extract_audio(wavs)
        file_hash = hashlib.md5(f"{text}{voice}{language}{time.time()}".encode()).hexdigest()[:10]
        output_path = self.outputs_dir / f"tts_{file_hash}.wav"
        sf.write(str(output_path), audio_array, sr)
        logger.info(f"Qwen3-TTS generated: {text[:50]}... -> {output_path.name}")
        return str(output_path)

    def generate_bytes(self, text: str, voice: Optional[str] = None,
                       language: Optional[str] = None,
                       speaker: Optional[str] = None,
                       instruct: Optional[str] = None,
                       ref_text: Optional[str] = None,
                       enable_text_splitting: bool = False,
                       **kwargs) -> bytes:
        voice = voice or self.speaker
        language = language or self.language
        instruct = instruct or self.instruct
        speaker_name = speaker

        ref_audio = self._get_ref_audio(voice)
        if ref_audio:
            if not ref_text:
                ref_text = self._get_ref_text(ref_audio)
            m = self.clone_model
            ref_text = ref_text or ""
            logger.info(f"Qwen3 clone bytes: ref_text={ref_text!r}, xvec_only=True")
            with torch.inference_mode():
                wavs, sr = m.generate_voice_clone(
                    text=text, language=language,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    xvec_only=True,
                )
        else:
            m = self.model
            if not speaker_name:
                spk = voice
                if spk.startswith("qwen3-"):
                    spk = spk[6:]
                speaker_name = spk
            if speaker_name not in [s["name"] for s in CUSTOMVOICE_SPEAKERS]:
                speaker_name = "Vivian"
            with torch.inference_mode():
                wavs, sr = m.generate_custom_voice(
                    text=text,
                    language=language,
                    speaker=speaker_name,
                    instruct=instruct,
                )

        audio_array = self._safe_extract_audio(wavs)
        buf = io.BytesIO()
        sf.write(buf, audio_array, sr, format="wav")
        wav_bytes = buf.getvalue()
        logger.info(f"Qwen3-TTS generated bytes: {len(wav_bytes)} for '{text[:50]}...'")
        return wav_bytes

    def generate_chunks(self, text: str, voice: Optional[str] = None,
                        language: Optional[str] = None,
                        speaker: Optional[str] = None,
                        instruct: Optional[str] = None,
                        ref_text: Optional[str] = None,
                        enable_text_splitting: bool = False,
                        ignore_chars: str = "",
                        max_new_tokens: int = 512,
                        max_window: Optional[int] = None):
        if max_window is None:
            max_window = getattr(self, 'max_window', 80)
        raw_chunks = split_text(text, sent_window=60, max_window=max_window, min_chunk=20)
        is_multi = len(raw_chunks) > 1

        if is_multi:
            logger.info(f"Text split into {len(raw_chunks)} chunks (max_new_tokens={max_new_tokens})")
        elif not raw_chunks or all(not c.strip() for c in raw_chunks):
            logger.warning("Qwen3-TTS: empty or invalid text, generation skipped")
            return

        chunks = []
        for c in raw_chunks:
            c = c.strip()
            if ignore_chars:
                for ch in ignore_chars:
                    c = c.replace(ch, '')
            c = c.strip()
            if c:
                chunks.append(c)

        if not chunks:
            return

        for cidx, chunk in enumerate(chunks):
            logger.info(f"Chunk {cidx + 1}/{len(chunks)}: {chunk[:60]}...")

        ref_audio = self._get_ref_audio(voice) if voice else None

        for cidx, chunk in enumerate(chunks):
            try:
                if ref_audio:
                    if not ref_text:
                        ref_text = self._get_ref_text(ref_audio)
                    m = self.clone_model
                    ref_text_val = ref_text or ""
                    with torch.inference_mode():
                        wavs, sr = m.generate_voice_clone(
                            text=chunk,
                            language=language or self.language,
                            ref_audio=ref_audio,
                            ref_text=ref_text_val,
                            xvec_only=True,
                            max_new_tokens=max_new_tokens,
                        )
                else:
                    m = self.model
                    spk = speaker
                    if not spk:
                        spk = voice
                        if spk and spk.startswith("qwen3-"):
                            spk = spk[6:]
                    if not spk or spk not in [s["name"] for s in CUSTOMVOICE_SPEAKERS]:
                        spk = "Vivian"
                    with torch.inference_mode():
                        wavs, sr = m.generate_custom_voice(
                            text=chunk,
                            language=language or self.language,
                            speaker=spk,
                            instruct=instruct or self.instruct,
                            max_new_tokens=max_new_tokens,
                        )

                audio_array = self._safe_extract_audio(wavs)
                buf = io.BytesIO()
                sf.write(buf, audio_array, sr, format="wav")
                audio_bytes = buf.getvalue()
                file_hash = hashlib.md5(f"{chunk}{voice}{language}{time.time()}{cidx}".encode()).hexdigest()[:10]
                out_path = self.outputs_dir / f"tts_{file_hash}.wav"
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)
                logger.info(f"  Chunk {cidx + 1} -> {out_path.name} ({len(audio_bytes)} bytes)")
                yield str(out_path)

            except Exception as e:
                logger.error(f"Error generating chunk {cidx + 1}: {e}")
                raise
