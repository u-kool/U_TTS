#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎙️ Twitch TTS Server v7.9.3 – Встроенный Piper TTS (piper.exe)
"""
import os
import sys
import json
import time
import copy
import asyncio
import logging
import threading
import random
import queue
import requests
import hashlib
import re
from pathlib import Path

# Fix Windows console encoding for Unicode
if sys.stdout.encoding and sys.stdout.encoding.upper() not in ('UTF-8', 'UTF-16'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
from flask import Flask, render_template, request, jsonify, send_file, Response
from werkzeug.utils import secure_filename

# ========== НАСТРОЙКИ Twitch API ==========
CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")
if not CLIENT_ID or not CLIENT_SECRET:
    CLIENT_ID = "fsiif72enf4wf6jg4omgxtif5aj0y9"
    CLIENT_SECRET = "upktwhaxy4z4vzwosxjvew6jytbm5h"
REDIRECT_URI = "http://localhost:8080/redirect/"
OAUTH_PORT = 8080
# =========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('data/server.log', encoding='utf-8', mode='a')
    ]
)
logger = logging.getLogger(__name__)

logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

from core.config_store import ConfigStore
from core.runtime_events import RuntimeEvents
from core.tts_runner import TTSRunner
from core.tts_engine import TTSEngine, SAPI5Engine
from core.twitch_auth import TwitchAuth
from core.voice_map_db import VoiceMapDB, EventConfigDB
from core.twitch_eventsub_api import TwitchEventSubClient
from irc_bot import TwitchIRCBot

# === НОВЫЙ ВСТРОЕННЫЙ PIPER ENGINE ===
from core.piper_engine import PiperEngine
from core.piper_preprocessor import preprocess_text

if getattr(sys, 'frozen', False):
    PROJECT_ROOT = Path(sys.executable).parent.resolve()
    _ASSETS_ROOT = Path(sys._MEIPASS).resolve()
else:
    PROJECT_ROOT = Path(__file__).parent.resolve()
    _ASSETS_ROOT = PROJECT_ROOT

_xtts_core_path = _ASSETS_ROOT / "workers" / "xtts" / "core"
sys.path.insert(0, str(_xtts_core_path))
try:
    from xtts_engine import LANGS
except ImportError:
    LANGS = ["ru"]
QWEN3_WORKER_URL = "http://localhost:5002"

COSYVOICE_WORKER_URL = "http://localhost:5003"
XTTS_WORKER_URL = "http://127.0.0.1:5004"
# PIPER_WORKER_URL больше не нужен


class CosyVoiceWorkerClient:
    """HTTP client for the CosyVoice3 TTS worker (separate process, own venv)."""
    def __init__(self, base_url: str = COSYVOICE_WORKER_URL):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.timeout = 120

    def health(self) -> bool:
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def generate(self, text: str, voice: str = None, language: str = None,
                 instruct: str = "", ref_text: str = None) -> dict:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if instruct:
            payload["instruct"] = instruct
        if ref_text:
            payload["ref_text"] = ref_text
        r = self._session.post(f"{self.base_url}/generate", json=payload, timeout=300)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"CosyVoice worker /generate failed (HTTP {r.status_code}): {detail}")
        return r.json()

    def generate_bytes(self, text: str, voice: str = None, language: str = None,
                       instruct: str = "", ref_text: str = None) -> bytes:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if instruct:
            payload["instruct"] = instruct
        if ref_text:
            payload["ref_text"] = ref_text
        r = self._session.post(f"{self.base_url}/generate_bytes", json=payload, timeout=300)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"CosyVoice worker /generate_bytes failed (HTTP {r.status_code}): {detail}")
        return r.content

    def generate_chunks(self, text: str, voice: str = None, language: str = None,
                        instruct: str = "", ref_text: str = None,
                        ignore_chars: str = ""):
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if instruct:
            payload["instruct"] = instruct
        if ref_text:
            payload["ref_text"] = ref_text
        if ignore_chars:
            payload["ignore_chars"] = ignore_chars
        r = self._session.post(f"{self.base_url}/generate_chunks", json=payload, timeout=600, stream=True)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"CosyVoice worker /generate_chunks failed (HTTP {r.status_code}): {detail}")
        for line in r.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in data:
                raise RuntimeError(f"CosyVoice worker chunk error: {data['error']}")
            if "file" in data:
                yield data["file"]

    def list_clones(self) -> list:
        try:
            r = self._session.get(f"{self.base_url}/list_clones", timeout=10)
            return r.json() if r.status_code == 200 else []
        except requests.RequestException:
            return []

    def upload_clone(self, file_path: str) -> dict:
        try:
            with open(file_path, "rb") as f:
                r = self._session.post(
                    f"{self.base_url}/upload_clone",
                    files={"file": f},
                    timeout=60
                )
                return r.json() if r.status_code == 200 else {"error": "upload failed"}
        except Exception as e:
            return {"error": str(e)}

    def configure(self, voices_dir: str = None, outputs_dir: str = None) -> bool:
        try:
            payload = {}
            if voices_dir:
                payload["voices_dir"] = voices_dir
            if outputs_dir:
                payload["outputs_dir"] = outputs_dir
            if not payload:
                return True
            r = self._session.post(f"{self.base_url}/configure", json=payload, timeout=10)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_voices(self) -> list:
        try:
            r = self._session.get(f"{self.base_url}/list_clones", timeout=10)
            clones = r.json() if r.status_code == 200 else []
        except requests.RequestException:
            clones = []
        voices = []
        for c in clones:
            voices.append({"name": f"cosyvoice-clone:{c['name']}", "engine": "cosyvoice", "type": "clone"})
        return voices


class Qwen3WorkerClient:
    """HTTP client for the Qwen3 TTS worker (separate process, own venv)."""
    def __init__(self, base_url: str = QWEN3_WORKER_URL):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.timeout = 120

    def health(self) -> bool:
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def generate(self, text: str, voice: str = None, language: str = None,
                 speaker: str = None, instruct: str = "", ref_text: str = None) -> dict:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if speaker:
            payload["speaker"] = speaker
        if instruct:
            payload["instruct"] = instruct
        if ref_text:
            payload["ref_text"] = ref_text
        r = self._session.post(f"{self.base_url}/generate", json=payload, timeout=300)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Qwen3 worker /generate failed (HTTP {r.status_code}): {detail}")
        return r.json()

    def generate_bytes(self, text: str, voice: str = None, language: str = None,
                       speaker: str = None, instruct: str = "", ref_text: str = None) -> bytes:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if speaker:
            payload["speaker"] = speaker
        if instruct:
            payload["instruct"] = instruct
        if ref_text:
            payload["ref_text"] = ref_text
        r = self._session.post(f"{self.base_url}/generate_bytes", json=payload, timeout=300)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Qwen3 worker /generate_bytes failed (HTTP {r.status_code}): {detail}")
        return r.content

    def generate_chunks(self, text: str, voice: str = None, language: str = None,
                        speaker: str = None, instruct: str = "", ref_text: str = None,
                        ignore_chars: str = "", max_new_tokens: int = 512,
                        max_window: int = None):
        payload = {"text": text, "max_new_tokens": max_new_tokens}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if speaker:
            payload["speaker"] = speaker
        if instruct:
            payload["instruct"] = instruct
        if ref_text:
            payload["ref_text"] = ref_text
        if ignore_chars:
            payload["ignore_chars"] = ignore_chars
        if max_window is not None:
            payload["max_window"] = max_window
        r = self._session.post(f"{self.base_url}/generate_chunks", json=payload, timeout=600, stream=True)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Qwen3 worker /generate_chunks failed (HTTP {r.status_code}): {detail}")
        for line in r.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in data:
                raise RuntimeError(f"Qwen3 worker chunk error: {data['error']}")
            if "file" in data:
                yield data["file"]

    def list_clones(self) -> list:
        try:
            r = self._session.get(f"{self.base_url}/list_clones", timeout=10)
            return r.json() if r.status_code == 200 else []
        except requests.RequestException:
            return []

    def upload_clone(self, file_path: str) -> dict:
        try:
            with open(file_path, "rb") as f:
                r = self._session.post(
                    f"{self.base_url}/upload_clone",
                    files={"file": f},
                    timeout=60
                )
                return r.json() if r.status_code == 200 else {"error": "upload failed"}
        except Exception as e:
            return {"error": str(e)}

    def configure(self, voices_dir: str = None, outputs_dir: str = None) -> bool:
        try:
            payload = {}
            if voices_dir:
                payload["voices_dir"] = voices_dir
            if outputs_dir:
                payload["outputs_dir"] = outputs_dir
            if not payload:
                return True
            r = self._session.post(f"{self.base_url}/configure", json=payload, timeout=10)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_voices(self) -> list:
        if not self.health():
            return []
        try:
            r = self._session.get(f"{self.base_url}/list_clones", timeout=10)
            clones = r.json() if r.status_code == 200 else []
        except requests.RequestException:
            clones = []
        voices = []
        for spk in ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"]:
            voices.append({"name": f"qwen3-{spk}", "engine": "qwen3", "type": "customvoice", "native": "Chinese" if spk in ("Vivian","Serena","Uncle_Fu","Dylan","Eric") else ("English" if spk in ("Ryan","Aiden") else ("Japanese" if spk=="Ono_Anna" else "Korean"))})
        for c in clones:
            voices.append({"name": f"qwen3-clone:{c['name']}", "engine": "qwen3", "type": "clone"})
        return voices


class XTTSWorkerClient:
    """HTTP client for the XTTS worker (separate process, own venv)."""
    def __init__(self, base_url: str = XTTS_WORKER_URL):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.timeout = 120

    def health(self) -> bool:
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def generate(self, text: str, voice: str = None, language: str = None,
                 temperature: float = None, repetition_penalty: float = None,
                 speed: float = None) -> dict:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if temperature is not None:
            payload["temperature"] = temperature
        if repetition_penalty is not None:
            payload["repetition_penalty"] = repetition_penalty
        if speed is not None:
            payload["speed"] = speed
        r = self._session.post(f"{self.base_url}/generate", json=payload, timeout=300)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"XTTS worker /generate failed (HTTP {r.status_code}): {detail}")
        return r.json()

    def generate_bytes(self, text: str, voice: str = None, language: str = None,
                       temperature: float = None, repetition_penalty: float = None,
                       speed: float = None) -> bytes:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if temperature is not None:
            payload["temperature"] = temperature
        if repetition_penalty is not None:
            payload["repetition_penalty"] = repetition_penalty
        if speed is not None:
            payload["speed"] = speed
        r = self._session.post(f"{self.base_url}/generate_bytes", json=payload, timeout=300)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"XTTS worker /generate_bytes failed (HTTP {r.status_code}): {detail}")
        return r.content

    def generate_chunks(self, text: str, voice: str = None, language: str = None,
                        temperature: float = None, repetition_penalty: float = None,
                        speed: float = None):
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if temperature is not None:
            payload["temperature"] = temperature
        if repetition_penalty is not None:
            payload["repetition_penalty"] = repetition_penalty
        if speed is not None:
            payload["speed"] = speed
        r = self._session.post(f"{self.base_url}/generate_chunks", json=payload, timeout=600, stream=True)
        if not r.ok:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"XTTS worker /generate_chunks failed (HTTP {r.status_code}): {detail}")
        for line in r.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in data:
                raise RuntimeError(f"XTTS worker chunk error: {data['error']}")
            if "file" in data:
                yield data["file"]

    def list_clones(self) -> list:
        try:
            r = self._session.get(f"{self.base_url}/list_clones", timeout=10)
            return r.json() if r.status_code == 200 else []
        except requests.RequestException:
            return []

    def list_voices(self) -> list:
        try:
            clones = self.list_clones()
        except Exception:
            clones = []
        voices = []
        for c in clones:
            voices.append({"name": f"xtts-{c['name']}", "engine": "xtts"})
        return voices

    def configure(self, voices_dir: str = None, outputs_dir: str = None,
                   latents_dir: str = None) -> bool:
        try:
            payload = {}
            if voices_dir:
                payload["voices_dir"] = voices_dir
            if outputs_dir:
                payload["outputs_dir"] = outputs_dir
            if latents_dir:
                payload["latents_dir"] = latents_dir
            if not payload:
                return True
            r = self._session.post(f"{self.base_url}/configure", json=payload, timeout=10)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_languages(self) -> list:
        try:
            r = self._session.get(f"{self.base_url}/list_languages", timeout=10)
            return r.json() if r.status_code == 200 else []
        except requests.RequestException:
            return []

    def upload_clone(self, file_path: str) -> dict:
        try:
            with open(file_path, "rb") as f:
                r = self._session.post(
                    f"{self.base_url}/upload_clone",
                    files={"file": f},
                    timeout=60
                )
                return r.json() if r.status_code == 200 else {"error": "upload failed"}
        except Exception as e:
            return {"error": str(e)}

    def is_ready(self) -> bool:
        return self.health()


twitch_auth = TwitchAuth(CLIENT_ID, CLIENT_SECRET, redirect_uri=REDIRECT_URI, oauth_port=OAUTH_PORT)

CONFIG_FILE = PROJECT_ROOT / "data" / "config.json"
OUTPUTS_DIR = PROJECT_ROOT / "data" / "outputs"
VOICES_DIR = PROJECT_ROOT / "data" / "voices"
PIPER_VOICES_DIR = VOICES_DIR / "piper"   # папка для ONNX-моделей Piper
PIPER_EXE = PROJECT_ROOT / "workers" / "piper_tts" / "piper.exe"

for d in [OUTPUTS_DIR, VOICES_DIR, PIPER_VOICES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Расширенная конфигурация по умолчанию
DEFAULT_CONFIG = {
    "twitch_token": "",
    "twitch_refresh_token": "",
    "twitch_channel": "",
    "twitch_user_id": "",
    "twitch_login": "",
    "filter_mods": True,
    "filter_broadcaster": False,
    "min_length": 1,
    "max_length": 500,
    "user_cooldown": 1,
    "event_cooldown": 1,
    "voice": "ru-RU-SvetlanaNeural",
    "rate": "+0%",
    "volume": "+0%",
    "pitch": "+0Hz",
    "host": "127.0.0.1",
    "port": 5000,
    "save_audio": False,
    "tts_enabled": True,
    "read_all_messages": True,
    "read_only_answered": False,
    "role_filters": {
        "highlighted": True,
        "subscription": False,
        "vip": False,
        "moderator": True
    },
    "filter_links": True,
    "filter_emotes": True,
    "filter_emoji": True,
    "use_keywords": False,
    "keywords": ["!tts"],
    "strip_keywords_from_tts": True,
    "ignore_chars": "@",
    "deduplicate_chars": False,
    "blacklist_users": [],
    "whitelist_users": [],
    "user_voice_map": {},
    "text_replacements": [],
    "auto_random_voice": False,
    "tts_engine": "edge-tts",
    "xtts_voice": "female_01.wav",
    "xtts_language": "ru",
    "xtts_temperature": 0.85,
    "xtts_repetition_penalty": 20,
    "xtts_speed": 1.0,
    "xtts_half_precision": False,
    "qwen3_voice": "Vivian",
    "qwen3_language": "Russian",
    "qwen3_instruct": "",
    "qwen3_tone": "neutral",
    "qwen3_emotion": "neutral",
    "qwen3_speed": "normal",
    "qwen3_pauses": "normal",
    "qwen3_max_window": 120,
    "cosyvoice_voice": "",
    "cosyvoice_language": "Russian",
    "cosyvoice_instruct": "",
    "cosyvoice_model_dir": "models/CosyVoice3/Fun-CosyVoice3-0.5B",
    "cosyvoice_speed": 1.0,
    "cosyvoice_fp16": True,
    "piper_voice": "",
    "piper_default_voice": "ru_RU-mari-medium_epoch6399",  # изменено с epoch5699 на более новую, если нужно
    "piper_max_cached": 2,
    "sapi5_voice": "",
    "sapi5_rate": 0,
    "sapi5_volume": 1.0,
    "events": {
        "follow": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "Новый follow {UserName}", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "subscription": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "{UserName} подписался на {Service} канал (уровень {Tier})", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "subscription_gift": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "{UserName} подарил {Total} подписок", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "cheer": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "{UserName} отправил {Bits} битсов", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "raid": {"enabled": False, "voice": "ru-RU-SvetlanaNeural", "format": "{UserName} начал рейд на {Service} канал и привел {Viewers} зрителей", "min_viewers": 0, "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "reward": {
            "enabled": True,
            "voice": "ru-RU-SvetlanaNeural",
            "format_no_msg": "{UserName} использовал награду {RewardName}",
            "format_with_msg": "{UserName} использовал награду {RewardName} и сказал {Message}",
            "reward_voice_map": {},
            "rate": "+0%",
            "volume": "+0%",
            "pitch": "+0Hz"
        },
        "hype_train": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "🔥 Хайповоз! {Level} уровень от {UserName}", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "goal": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "🎯 Цель \"{GoalName}\": {CurrentAmount}/{TargetAmount}", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "watch_streak": {"enabled": True, "voice": "ru-RU-SvetlanaNeural", "format": "🔥 {UserName} смотрит стрим {Streak} дней подряд! {InputRaw}", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"}
    }
}

def _normalize_tts_param(value: str, suffix: str = '%') -> str:
    if not value:
        return f"+0{suffix}"
    value = value.strip()
    if not value.endswith(suffix):
        value += suffix
    if value.startswith('+') or value.startswith('-'):
        return value
    return f"+{value}"


def _parse_prefixed_voice(prefixed_voice: str) -> tuple:
    """(engine, actual_voice) из 'edge-xxx', 'xtts-xxx', 'qwen3-xxx', 'cosyvoice-xxx' или 'piper-xxx'."""
    if not prefixed_voice:
        return ("edge-tts", "ru-RU-SvetlanaNeural")
    if prefixed_voice.startswith("xtts-"):
        return ("xtts", prefixed_voice[5:])
    if prefixed_voice.startswith("edge-"):
        return ("edge-tts", prefixed_voice[5:])
    if prefixed_voice.startswith("qwen3-"):
        return ("qwen3", prefixed_voice)
    if prefixed_voice.startswith("cosyvoice-"):
        return ("cosyvoice", prefixed_voice)
    if prefixed_voice.startswith("piper-"):
        return ("piper", prefixed_voice)
    if prefixed_voice.startswith("sapi5-"):
        return ("sapi5", prefixed_voice[6:])
    cfg_snap = _get_config_snapshot()
    eng = cfg_snap.get("tts_engine", "edge-tts")
    return (eng, prefixed_voice)

_engine_ready_cache = {}
_ENGINE_READY_CACHE_TTL = 30

def _is_engine_ready(engine_name: str) -> bool:
    global _engine_ready_cache
    now = time.time()
    cached = _engine_ready_cache.get(engine_name)
    if cached and now - cached[1] < _ENGINE_READY_CACHE_TTL:
        return cached[0]
    ready = _is_engine_ready_uncached(engine_name)
    _engine_ready_cache[engine_name] = (ready, now)
    return ready

def _is_engine_ready_uncached(engine_name: str) -> bool:
    if engine_name == "qwen3":
        return qwen3_worker.health() if qwen3_worker else False
    elif engine_name == "xtts":
        return xtts_worker.health() if xtts_worker else False
    elif engine_name == "cosyvoice":
        return cosyvoice_worker.health() if cosyvoice_worker else False
    elif engine_name == "piper":
        return piper_engine is not None and piper_engine.exe_path.exists()  # Piper всегда готов, если бинарник есть
    elif engine_name == "sapi5":
        return sapi5_engine.is_ready() if sapi5_engine else False
    elif engine_name == "edge-tts":
        return tts_engine.is_ready() if tts_engine else True
    return False


def _get_all_available_voices() -> list:
    """Return all currently available voices from cache or fresh fetch."""
    global _voice_cache, _voice_cache_time
    try:
        now = time.time()
        if _voice_cache is None or now - _voice_cache_time >= _VOICE_CACHE_TTL:
            fresh = _build_voices_list()
            _voice_cache = fresh
            _voice_cache_time = now
        return _voice_cache.get("voices", [])
    except Exception:
        return []


def _ensure_user_in_db(user: str, cfg: dict):
    """Add user to voice_map_db if not already present. Assigns random voice from all available engines."""
    existing = voice_map_db.get(user)
    has_voice = existing is not None and existing.get("voice")
    if has_voice:
        voice_map_db.touch(user)
        return
    if not cfg.get("auto_random_voice"):
        return
    all_voices = _get_all_available_voices()
    if not all_voices:
        voice_map_db.set(user, voice=None, engine=None)
        return

    chosen_entry = random.choice(all_voices)
    chosen_name = chosen_entry["name"]
    engine = chosen_entry.get("engine", "edge-tts")

    if engine == "xtts":
        name_no_prefix = chosen_name[5:] if chosen_name.startswith("xtts-") else chosen_name
        lang = random.choice(LANGS)
        temp = round(random.uniform(0.3, 1.0), 2)
        voice_map_db.set(user, voice=name_no_prefix, engine="xtts",
                         xtts_language=lang, xtts_temperature=temp)
        logger.info(f"🎲 Auto-assigned XTTS voice '{name_no_prefix}' (lang={lang}) to new user '{user}'")
        broadcast_sse({
            "event": "voice_assigned",
            "user": user,
            "voice": f"xtts-{name_no_prefix}",
            "engine": "xtts",
            "xtts_language": lang,
            "xtts_temperature": temp,
        })
    elif engine == "edge-tts":
        name_no_prefix = chosen_name[5:] if chosen_name.startswith("edge-") else chosen_name
        rate = f"{random.randint(-80, 80)}%"
        pitch = f"{random.randint(-40, 40)}Hz"
        voice_map_db.set(user, voice=name_no_prefix, engine="edge-tts", rate=rate, pitch=pitch)
        logger.info(f"🎲 Auto-assigned Edge-TTS voice '{name_no_prefix}' (rate={rate}, pitch={pitch}) to new user '{user}'")
        broadcast_sse({
            "event": "voice_assigned",
            "user": user,
            "voice": f"edge-{name_no_prefix}",
            "engine": "edge-tts",
            "rate": rate,
            "pitch": pitch,
        })
    elif engine == "qwen3":
        voice_map_db.set(user, voice=chosen_name, engine="qwen3")
        logger.info(f"🎲 Auto-assigned Qwen3 voice '{chosen_name}' to new user '{user}'")
        broadcast_sse({
            "event": "voice_assigned",
            "user": user,
            "voice": chosen_name,
            "engine": "qwen3",
        })
    elif engine == "cosyvoice":
        voice_map_db.set(user, voice=chosen_name, engine="cosyvoice")
        logger.info(f"🎲 Auto-assigned CosyVoice voice '{chosen_name}' to new user '{user}'")
        broadcast_sse({
            "event": "voice_assigned",
            "user": user,
            "voice": chosen_name,
            "engine": "cosyvoice",
        })
    elif engine == "piper":
        name_no_prefix = chosen_name[6:] if chosen_name.startswith("piper-") else chosen_name
        voice_map_db.set(user, voice=name_no_prefix, engine="piper")
        logger.info(f"🎲 Auto-assigned Piper voice '{name_no_prefix}' to new user '{user}'")
        broadcast_sse({
            "event": "voice_assigned",
            "user": user,
            "voice": f"piper-{name_no_prefix}",
            "engine": "piper",
        })
    elif engine == "sapi5":
        name_no_prefix = chosen_name[6:] if chosen_name.startswith("sapi5-") else chosen_name
        voice_map_db.set(user, voice=name_no_prefix, engine="sapi5")
        logger.info(f"🎲 Auto-assigned SAPI5 voice '{name_no_prefix}' to new user '{user}'")
        broadcast_sse({
            "event": "voice_assigned",
            "user": user,
            "voice": f"sapi5-{name_no_prefix}",
            "engine": "sapi5",
        })
    else:
        voice_map_db.set(user, voice=None, engine=None)

    with config_lock:
        config.setdefault("user_voice_map", {})[user] = chosen_name
        save_config(config)


def deep_merge(base, overrides):
    for key, value in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base

config_store = ConfigStore(CONFIG_FILE, DEFAULT_CONFIG)

# Глобальная блокировка для thread-safe доступа к config и TTS-состоянию
config_lock = threading.RLock()


def save_config(config: dict) -> bool:
    try:
        config_store.save(config)
        return True
    except Exception as e:
        logger.error(f"❌ Save config error: {e}")
        return False


def _get_config_snapshot() -> dict:
    with config_lock:
        return copy.deepcopy(config)


config = config_store.load()
if not CONFIG_FILE.exists():
    logger.info(f"config.json не найден, создаю с настройками по умолчанию")
    config_store.save(config)
voice_map_db = VoiceMapDB()
voice_map_db.import_from_config(config.get("user_voice_map", {}))
event_db = EventConfigDB()
event_db.import_from_config(config.get("events", {}))
cached_emotes = {}
emotes_last_fetch = 0
EMOTES_CACHE_TTL = 600
tts_engine = TTSEngine(voice=config.get("voice", "ru-RU-SvetlanaNeural"))
runtime_events = RuntimeEvents()
twitch_bot: TwitchIRCBot = None
twitch_running = False
event_sub_client: TwitchEventSubClient = None
event_sub_thread: threading.Thread = None

# Буфер чат-сообщений для подавления дублирования TTS при наградах
_chat_tts_pending = {}
_chat_tts_pending_lock = threading.Lock()
_reward_tts_done = set()
_reward_tts_done_lock = threading.Lock()
_CHAT_TTS_DELAY = 0.1

# Голоса, временно заменённые из-за недоступности движка {user -> {"voice": str, "engine": str}}
_pending_voice_restore = {}
_pending_voice_restore_lock = threading.Lock()

# TTS worker: асинхронная очередь, чтобы IRC и EventSub не блокировались
tts_runner = None

# Qwen3 worker (отдельный процесс, свой venv)
qwen3_worker = Qwen3WorkerClient(QWEN3_WORKER_URL)

# CosyVoice worker (отдельный процесс, свой venv)
cosyvoice_worker = CosyVoiceWorkerClient(COSYVOICE_WORKER_URL)

# XTTS worker (отдельный процесс, свой venv)
xtts_worker = XTTSWorkerClient(XTTS_WORKER_URL)

# Piper engine (встроенный, через piper.exe)
piper_engine = None
if PIPER_EXE.exists() and PIPER_VOICES_DIR.exists():
    try:
        default_piper_voice = config.get("piper_default_voice", "ru_RU-mari-medium_epoch6399")
        piper_engine = PiperEngine(PIPER_EXE, PIPER_VOICES_DIR, default_voice=default_piper_voice)
        logger.info(f"✅ Piper engine initialized, voices: {len(piper_engine.list_voices())}")
    except Exception as e:
        logger.error(f"Failed to init Piper engine: {e}")
        piper_engine = None
else:
    logger.warning(f"Piper not found: exe={PIPER_EXE.exists()}, voices_dir={PIPER_VOICES_DIR.exists()}")

# SAPI5 engine (встроенный, через pyttsx3)
sapi5_engine = SAPI5Engine()

last_tts_time = {}
last_event_tts_time = 0

app = Flask(__name__,
            template_folder=str(_ASSETS_ROOT / "core" / "templates"),
            static_folder=str(_ASSETS_ROOT / "core" / "templates" / "static"),
            static_url_path="/static"
           )

emoteMap = {}

def refresh_twitch_token(refresh_token: str):
    new_access, new_refresh = twitch_auth.refresh_access_token(refresh_token)
    if new_access:
        logger.info("✅ Token refreshed successfully")
    else:
        logger.warning("Token refresh failed")
    return new_access, new_refresh

def perform_full_oauth():
    logger.info("🌐 Запуск OAuth-сервера...")
    result = twitch_auth.perform_full_oauth()
    if result[0]:
        logger.info(f"✅ Авторизация успешна: {result[2]} (ID: {result[1]})")
    else:
        logger.error("❌ Ошибка авторизации")
    return result

def log_to_queue(msg_type: str, text: str, user: str = None, emotes: dict = None):
    entry = runtime_events.log(msg_type=msg_type, text=text, user=user, emotes=emotes)
    broadcast_sse({"event": "log", "type": msg_type, "text": text, "user": user, "emotes": emotes, "timestamp": entry["timestamp"]})

def broadcast_sse(message: dict):
    if message.get("event") == "new_audio":
        filename = message.get("filename")
        if filename and USE_EXTERNAL_PLAYER:
            send_to_audio_player(filename)
            if not config.get("save_audio", False):
                def delete_file():
                    time.sleep(10)
                    file_path = OUTPUTS_DIR / filename
                    if file_path.exists():
                        try:
                            file_path.unlink()
                            logger.debug(f"Deleted temporary file: {filename}")
                        except Exception as e:
                            logger.warning(f"Failed to delete {filename}: {e}")
                threading.Thread(target=delete_file, daemon=True).start()
    runtime_events.broadcast(message)

import requests
import threading
import time

# === НАСТРОЙКИ АУДИО ПЛЕЕРА ===
AUDIO_PLAYER_URL = "http://127.0.0.1:5006"
AUDIO_PLAYER_RETRY_DELAY = 5
AUDIO_PLAYER_MAX_RETRIES = 3
USE_EXTERNAL_PLAYER = True

audio_player_lock = threading.Lock()
audio_player_available = True

def is_audio_player_ready():
    global audio_player_available
    try:
        response = requests.get(f"{AUDIO_PLAYER_URL}/health", timeout=2)
        if response.status_code == 200:
            if not audio_player_available:
                logger.info("✅ Аудиоплеер снова доступен")
                audio_player_available = True
            return True
        else:
            audio_player_available = False
            return False
    except requests.RequestException:
        if audio_player_available:
            logger.warning("⚠️ Аудиоплеер недоступен. Запустите audio_player/player.py")
        audio_player_available = False
        return False

def send_to_audio_player(file_name, max_retries=AUDIO_PLAYER_MAX_RETRIES):
    if not is_audio_player_ready():
        logger.warning(f"Аудиоплеер недоступен, файл {file_name} не будет воспроизведён")
        return False

    payload = {"file": file_name}
    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{AUDIO_PLAYER_URL}/play",
                json=payload,
                timeout=5
            )
            if response.status_code == 200:
                logger.info(f"Отправлено на аудиоплеер: {file_name}")
                return True
            else:
                error_msg = response.json().get('error', 'Unknown error')
                logger.warning(f"Попытка {attempt+1}/{max_retries} не удалась: {error_msg}")
        except requests.RequestException as e:
            logger.warning(f"Попытка {attempt+1}/{max_retries} не удалась: {e}")

        time.sleep(1)

    logger.error(f"Не удалось отправить файл {file_name} после {max_retries} попыток")
    return False

def tts_wrapper(text: str, voice: str = None, rate: str = None, volume: str = None, pitch: str = None, **kwargs):
    if tts_runner is None:
        return False
    return tts_runner.enqueue(text=text, voice=voice, rate=rate, volume=volume, pitch=pitch, **kwargs)

def _event_range_value(event: dict) -> float:
    et = event.get("type")
    if et == "raid":
        return event.get("viewers", 0)
    elif et == "cheer":
        return event.get("bits", 0)
    elif et == "subscription_gift":
        return event.get("total", 0)
    elif et == "hype_train":
        return event.get("level", 0)
    elif et == "goal":
        return event.get("current_amount", 0)
    return 0

def should_tts_message(event: dict) -> (bool, str, dict):
    cfg = _get_config_snapshot()
    event_type = event.get("type", "chat")
    if not cfg.get("tts_enabled", True):
        return False, "", {}
    user = event.get("user", "")

    # === Non-chat events (follow, sub, cheer, raid, reward) ===
    if event_type != "chat":
        events_cfg = event_db.to_config_dict()
        ev_cfg = events_cfg.get(event_type, {})
        if not ev_cfg.get("enabled", True):
            return False, "", {}
        show_in_chat = ev_cfg.get("show_in_chat", True)

        # Resolve template
        if event_type == "reward":
            msg = event.get("message", "").strip()
            if msg:
                template = ev_cfg.get("format_with_msg", "{UserName} использовал награду {RewardName} и сказал {Message}")
            else:
                template = ev_cfg.get("format_no_msg", "{UserName} использовал награду {RewardName}")
        else:
            template = ev_cfg.get("format", "")

        # Random message selection from messages list with range filtering
        raw_messages = ev_cfg.get("messages", [])
        if not raw_messages:
            raw_messages = event_db.get_messages(event_type) if event_db else []
        if raw_messages:
            valid = []
            range_val = _event_range_value(event)
            for m in raw_messages:
                if isinstance(m, dict):
                    rmin = m.get("min_range")
                    rmax = m.get("max_range")
                    if rmin is not None and range_val < rmin:
                        continue
                    if rmax is not None and range_val > rmax:
                        continue
                    valid.append(m.get("message", ""))
                else:
                    valid.append(str(m))
            if valid:
                if template:
                    valid.append(template)
                template = random.choice(valid)

        if not template:
            logger.warning(f"⚠️ Нет шаблона форматирования для события {event_type}")
            return False, "", {}

        text = template
        text = text.replace("{UserName}", user)
        text = text.replace("{Service}", "Twitch")
        if event_type == "subscription":
            text = text.replace("{Tier}", event.get("tier", ""))
        elif event_type == "subscription_gift":
            text = text.replace("{Total}", str(event.get("total", 0)))
        elif event_type == "cheer":
            text = text.replace("{Bits}", str(event.get("bits", 0)))
        elif event_type == "raid":
            text = text.replace("{Viewers}", str(event.get("viewers", 0)))
        elif event_type == "reward":
            text = text.replace("{RewardName}", event.get("reward_name", ""))
            text = text.replace("{Message}", event.get("message", ""))
        elif event_type == "hype_train":
            text = text.replace("{Level}", str(event.get("level", 1)))
            text = text.replace("{Total}", str(event.get("total", 0)))
        elif event_type == "goal":
            text = text.replace("{GoalName}", event.get("goal_name", ""))
            text = text.replace("{GoalType}", event.get("goal_type", ""))
            text = text.replace("{CurrentAmount}", str(event.get("current_amount", 0)))
            text = text.replace("{TargetAmount}", str(event.get("target_amount", 0)))
        elif event_type == "watch_streak":
            text = text.replace("{Streak}", str(event.get("streak", 1)))
            text = text.replace("{InputRaw}", event.get("input_raw", ""))

        if not text or not text.strip():
            logger.warning(f"⚠️ Текст для события {event_type} пуст после подстановки")
            return False, "", {}

        _voice_default = cfg.get("voice", DEFAULT_CONFIG.get("voice", "ru-RU-SvetlanaNeural"))

        def _ev_voice(raw):
            if raw == "__silent__":
                return ("edge-tts", "__silent__")
            return _parse_prefixed_voice(raw)

        # Resolve event cooldown
        now = time.time()
        global last_event_tts_time
        cooldown = ev_cfg.get("event_cooldown", cfg.get("event_cooldown", 1))
        if now - last_event_tts_time < cooldown:
            logger.debug(f"⏳ Event cooldown активен для {event_type}")
            return False, "", {}
        last_event_tts_time = now

        ev_engine = None
        if event_type == "reward":
            reward_name = event.get("reward_name", "")
            reward_map = ev_cfg.get("reward_voice_map", {})
            voice_cfg = reward_map.get(reward_name)
            if voice_cfg is None:
                ev_engine, voice = _ev_voice(ev_cfg.get("voice", _voice_default))
                rate = _normalize_tts_param(ev_cfg.get("rate", cfg.get("rate", "+0%")), '%')
                volume = _normalize_tts_param(ev_cfg.get("volume", cfg.get("volume", "+0%")), '%')
                pitch = _normalize_tts_param(ev_cfg.get("pitch", cfg.get("pitch", "+0Hz")), 'Hz')
            else:
                if isinstance(voice_cfg, str):
                    if voice_cfg == "__silent__":
                        logger.info(f"🔇 Награда '{reward_name}' отключена, событие залогировано")
                        if show_in_chat:
                            log_to_queue("event", text, user)
                        return False, "", {}
                    ev_engine, voice = _ev_voice(voice_cfg)
                    rate = _normalize_tts_param(ev_cfg.get("rate", cfg.get("rate", "+0%")), '%')
                    volume = _normalize_tts_param(ev_cfg.get("volume", cfg.get("volume", "+0%")), '%')
                    pitch = _normalize_tts_param(ev_cfg.get("pitch", cfg.get("pitch", "+0Hz")), 'Hz')
                else:
                    if voice_cfg.get("voice") == "__silent__":
                        logger.info(f"🔇 Награда '{reward_name}' отключена, событие залогировано")
                        if show_in_chat:
                            log_to_queue("event", text, user)
                        return False, "", {}
                    ev_engine, voice = _ev_voice(voice_cfg.get("voice", ev_cfg.get("voice", _voice_default)))
                    rate = _normalize_tts_param(voice_cfg.get("rate", ev_cfg.get("rate", cfg.get("rate", "+0%"))), '%')
                    volume = _normalize_tts_param(voice_cfg.get("volume", ev_cfg.get("volume", cfg.get("volume", "+0%"))), '%')
                    pitch = _normalize_tts_param(voice_cfg.get("pitch", ev_cfg.get("pitch", cfg.get("pitch", "+0Hz"))), 'Hz')
        else:
            voice_cfg = ev_cfg.get("voice", _voice_default)
            if isinstance(voice_cfg, dict):
                ev_engine, voice = _ev_voice(voice_cfg.get("voice", _voice_default))
                rate = _normalize_tts_param(voice_cfg.get("rate", ev_cfg.get("rate", cfg.get("rate", "+0%"))), '%')
                volume = _normalize_tts_param(voice_cfg.get("volume", ev_cfg.get("volume", cfg.get("volume", "+0%"))), '%')
                pitch = _normalize_tts_param(voice_cfg.get("pitch", ev_cfg.get("pitch", cfg.get("pitch", "+0Hz"))), 'Hz')
            else:
                ev_engine, voice = _ev_voice(voice_cfg)
                rate = _normalize_tts_param(ev_cfg.get("rate", cfg.get("rate", "+0%")), '%')
                volume = _normalize_tts_param(ev_cfg.get("volume", cfg.get("volume", "+0%")), '%')
                pitch = _normalize_tts_param(ev_cfg.get("pitch", cfg.get("pitch", "+0Hz")), 'Hz')

        if voice == "__silent__":
            logger.info(f"🔇 Событие '{event_type}' отключено, событие залогировано")
            if show_in_chat:
                log_to_queue("event", text, user)
            return False, "", {}

        logger.info(f"🔊 Событие {event_type}: '{text}' (voice={voice})")

        xtts_cfg = voice_cfg if isinstance(voice_cfg, dict) else ev_cfg
        ev_language = xtts_cfg.get("language", ev_cfg.get("language", "ru")) if ev_engine == "xtts" else None
        ev_temperature = random.uniform(0.3, 1.0) if ev_engine == "xtts" else None
        ev_repetition = xtts_cfg.get("repetition_penalty", ev_cfg.get("repetition_penalty", 20)) if ev_engine == "xtts" else None
        ev_speed = xtts_cfg.get("speed", ev_cfg.get("speed", None)) if ev_engine == "xtts" else None

        tts_wrapper(text, voice=voice, rate=rate, volume=volume, pitch=pitch, engine=ev_engine,
                    language=ev_language, temperature=ev_temperature,
                    repetition_penalty=ev_repetition, speed=ev_speed)

        if show_in_chat:
            log_to_queue("event", text, user)
        return False, "", {}

    # === Chat messages ===
    text = event.get("text", "").strip()
    if not text or not user:
        return False, "", {}
    skip_role_check = False

    whitelist = cfg.get("whitelist_users", [])
    if user in whitelist:
        skip_role_check = True
    else:
        if user in cfg.get("blacklist_users", []):
            return False, "", {}
        is_broadcaster = event.get("is_broadcaster", False)
        if is_broadcaster:
            if cfg.get("filter_broadcaster", False):
                return False, "", {}
            else:
                skip_role_check = True

    min_len = cfg.get("min_length", 3)
    max_len = cfg.get("max_length", 200)
    if len(text) < min_len or len(text) > max_len:
        return False, "", {}
    now = time.time()
    cooldown = cfg.get("user_cooldown", 1)
    with config_lock:
        if user in last_tts_time and (now - last_tts_time[user]) < cooldown:
            logger.debug(f"User {user} on cooldown ({now - last_tts_time[user]:.1f}s < {cooldown}s)")
            return False, "", {}
        last_tts_time[user] = now

    if cfg.get("filter_links", True):
        text = re.sub(r'https?://\S+|www\.\S+', '', text).strip()
    global emoteMap
    if cfg.get("filter_emotes", True) and emoteMap:
        words = text.split()
        new_words = []
        for w in words:
            clean_w = w.strip()
            if clean_w not in emoteMap:
                new_words.append(w)
        text = ' '.join(new_words)
    if cfg.get("filter_emoji", True):
        text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002600-\U000026FF\U00002700-\U000027BF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U0000FE00-\U0000FE0F\U0000200D\U0001F3FB-\U0001F3FF]', '', text).strip()
    ignore_chars = cfg.get("ignore_chars", "")
    if ignore_chars:
        for ch in ignore_chars:
            text = text.replace(ch, '')
    if cfg.get("deduplicate_chars", False):
        text = re.sub(r'(.)\1{2,}', r'\1', text)
    replacements = cfg.get("text_replacements", [])
    for rep in replacements:
        old = rep.get("from", "")
        new = rep.get("to", "")
        if old:
            text = text.replace(old, new)
    if cfg.get("use_keywords", False):
        keywords = cfg.get("keywords", [])
        found = any(kw in text for kw in keywords)
        if not found:
            return False, "", {}
        if cfg.get("strip_keywords_from_tts", True):
            for kw in keywords:
                text = text.replace(kw, '').strip()
    if not text:
        return False, "", {}

    if not skip_role_check and not cfg.get("read_all_messages", True):
        role_filters = cfg.get("role_filters", {})
        is_sub = event.get("is_subscriber", False)
        is_vip = event.get("is_vip", False)
        is_mod = event.get("is_moderator", False)
        is_highlighted = event.get("is_highlighted", False)

        allowed_by_role = False
        if role_filters.get("subscription") and is_sub:
            allowed_by_role = True
        if role_filters.get("vip") and is_vip:
            allowed_by_role = True
        if role_filters.get("moderator") and is_mod:
            allowed_by_role = True
        if role_filters.get("highlighted") and is_highlighted:
            allowed_by_role = True

        if cfg.get("read_only_answered", False) and not event.get("is_reply", False):
            return False, "", {}

        if not allowed_by_role:
            return False, "", {}

    if cfg.get("tts_engine") == "xtts":
        default_voice = cfg.get("xtts_voice", "ref.wav")
    elif cfg.get("tts_engine") == "cosyvoice":
        default_voice = cfg.get("cosyvoice_voice", "")
    elif cfg.get("tts_engine") == "piper":
         default_voice = cfg.get("piper_voice", "") or f"piper-{cfg.get('piper_default_voice', 'ru_RU-mari-medium_epoch6399')}"
    elif cfg.get("tts_engine") == "sapi5":
        default_voice = cfg.get("sapi5_voice", "Microsoft Zira Desktop - English (United States)")
    else:
        default_voice = cfg.get("voice", "ru-RU-SvetlanaNeural")
    engine_type = None
    voice_to_use = default_voice
    xtts_language = "ru"
    xtts_temperature = random.uniform(0.3, 1.0)
    xtts_repetition_penalty = cfg.get("xtts_repetition_penalty", 20)
    # Try DB first (now has separate engine + voice columns)
    db_row = voice_map_db.get(user)
    if db_row and db_row.get("voice"):
        voice_to_use = db_row["voice"]
        engine_type = db_row.get("engine") or None
        rate = _normalize_tts_param(db_row.get("rate", cfg.get("rate", "+0%")), '%')
        volume = _normalize_tts_param(db_row.get("volume", cfg.get("volume", "+0%")), '%')
        pitch = _normalize_tts_param(db_row.get("pitch", cfg.get("pitch", "+0Hz")), 'Hz')
        xtts_language = db_row.get("xtts_language") or "ru"
        xtts_temperature = db_row.get("xtts_temperature") or random.uniform(0.3, 1.0)
    # xtts_repetition_penalty is global config, not per-user
    else:
        user_voice_cfg = cfg.get("user_voice_map", {}).get(user)
        if isinstance(user_voice_cfg, dict) and user_voice_cfg.get("voice"):
            voice_to_use = user_voice_cfg.get("voice")
            engine_type = user_voice_cfg.get("engine") or None
            rate = _normalize_tts_param(user_voice_cfg.get("rate", cfg.get("rate", "+0%")), '%')
            volume = _normalize_tts_param(user_voice_cfg.get("volume", cfg.get("volume", "+0%")), '%')
            pitch = _normalize_tts_param(user_voice_cfg.get("pitch", cfg.get("pitch", "+0Hz")), 'Hz')
            xtts_language = user_voice_cfg.get("xtts_language") or user_voice_cfg.get("language", "ru")
            xtts_temperature = user_voice_cfg.get("xtts_temperature") or user_voice_cfg.get("temperature", random.uniform(0.3, 1.0))
        elif isinstance(user_voice_cfg, str):
            engine_type, voice_to_use = _parse_prefixed_voice(user_voice_cfg)
            rate = _normalize_tts_param(cfg.get("rate", "+0%"), '%')
            volume = _normalize_tts_param(cfg.get("volume", "+0%"), '%')
            pitch = _normalize_tts_param(cfg.get("pitch", "+0Hz"), 'Hz')
        else:
            voice_to_use = default_voice
            if cfg.get("tts_engine") == "xtts":
                engine_type = "xtts"
            elif cfg.get("tts_engine") == "qwen3":
                engine_type = "qwen3"
            elif cfg.get("tts_engine") == "cosyvoice":
                engine_type = "cosyvoice"
            elif cfg.get("tts_engine") == "piper":
                engine_type = "piper"
            elif cfg.get("tts_engine") == "sapi5":
                engine_type = "sapi5"
            rate = _normalize_tts_param(cfg.get("rate", "+0%"), '%')
            volume = _normalize_tts_param(cfg.get("volume", "+0%"), '%')
            pitch = _normalize_tts_param(cfg.get("pitch", "+0Hz"), 'Hz')
            xtts_language = cfg.get("xtts_language", "ru")
            xtts_temperature = random.uniform(0.3, 1.0)
    # Проверка: если движок голоса не загружен — заменяем на голос по умолчанию
    if engine_type:
        if not _is_engine_ready(engine_type):
            with _pending_voice_restore_lock:
                _pending_voice_restore[user] = {
                    "voice": voice_to_use,
                    "engine": engine_type,
                    "rate": rate,
                    "volume": volume,
                    "pitch": pitch,
                    "xtts_language": xtts_language,
                    "xtts_temperature": xtts_temperature,
                }
            # Пробуем SAPI5 как надёжный fallback, затем tts_engine по умолчанию
            if _is_engine_ready("sapi5"):
                engine_type = "sapi5"
                voice_to_use = cfg.get("sapi5_voice", "Microsoft Irina Desktop - Russian")
            elif _is_engine_ready("edge-tts"):
                engine_type = "edge-tts"
                voice_to_use = cfg.get("voice", "ru-RU-SvetlanaNeural")
            else:
                engine_type = cfg.get("tts_engine", "edge-tts")
                if engine_type == "xtts":
                    voice_to_use = cfg.get("xtts_voice", "ref.wav")
                elif engine_type == "cosyvoice":
                    voice_to_use = cfg.get("cosyvoice_voice", "")
                elif engine_type == "piper":
                    voice_to_use = cfg.get("piper_voice", "") or f"piper-{cfg.get('piper_default_voice', 'ru_RU-mari-medium_epoch6399')}"
                elif engine_type == "sapi5":
                    voice_to_use = cfg.get("sapi5_voice", "Microsoft Irina Desktop - Russian")
                elif engine_type == "qwen3":
                    voice_to_use = cfg.get("qwen3_voice", "Vivian")
                else:
                    voice_to_use = cfg.get("voice", "ru-RU-SvetlanaNeural")
            rate = _normalize_tts_param(cfg.get("rate", "+0%"), '%')
            volume = _normalize_tts_param(cfg.get("volume", "+0%"), '%')
            pitch = _normalize_tts_param(cfg.get("pitch", "+0Hz"), 'Hz')
            xtts_language = cfg.get("xtts_language", "ru")
            xtts_temperature = random.uniform(0.3, 1.0)
            logger.info(f"🔄 Engine not ready, fallback to '{engine_type}' voice '{voice_to_use}' for user '{user}'")
        else:
            with _pending_voice_restore_lock:
                pending = _pending_voice_restore.pop(user, None)
            if pending and pending.get("engine") == engine_type:
                voice_to_use = pending["voice"]
                engine_type = pending["engine"]
                rate = pending.get("rate", rate)
                volume = pending.get("volume", volume)
                pitch = pending.get("pitch", pitch)
                xtts_language = pending.get("xtts_language", xtts_language)
                xtts_temperature = pending.get("xtts_temperature", xtts_temperature)
                logger.info(f"↩️ Restored original voice '{voice_to_use}' for user '{user}' (engine '{engine_type}' ready)")
                voice_map_db.set(user, voice=voice_to_use, engine=engine_type,
                                 rate=rate, volume=volume, pitch=pitch,
                                 xtts_language=xtts_language, xtts_temperature=xtts_temperature)
                with config_lock:
                    cfg_save = _get_config_snapshot()
                    save_config(cfg_save)

    if event_type == "chat":
        qwen3_language = cfg.get("qwen3_language", "Russian")
        cosyvoice_language = cfg.get("cosyvoice_language", "Russian")
        if engine_type == "xtts":
            language = xtts_language
        elif engine_type == "qwen3":
            language = qwen3_language
        elif engine_type == "cosyvoice":
            language = cosyvoice_language
        else:
            language = None
        chat_speed = cfg.get("xtts_speed", 1.0) if engine_type == "xtts" else None
        return True, text, {
            "voice": voice_to_use,
            "engine": engine_type,
            "rate": rate,
            "volume": volume,
            "pitch": pitch,
            "language": language,
            "temperature": xtts_temperature,
            "repetition_penalty": xtts_repetition_penalty,
            "speed": chat_speed
        }
    return False, "", {}

def process_event(event_data: dict):
    """Process an event (follow, sub, cheer, raid, reward) for TTS."""
    should_tts_message(event_data)

def handle_message(event: dict):
    """Обработка входящих сообщений от IRC и EventSub"""
    if event.get("_source") == "eventsub":
        process_event(event)
        if event.get("type") == "reward":
            msg = event.get("message", "").strip()
            user = event.get("user", "")
            if msg and user:
                with _reward_tts_done_lock:
                    _reward_tts_done.add((user, msg))
                with _chat_tts_pending_lock:
                    timer = _chat_tts_pending.pop((user, msg), None)
                    if timer:
                        timer.cancel()
        return
    
    event_type = event.get("type")
    if event_type == "chat":
        text = event.get("text", "").strip()
        user = event.get("user", "Аноним")
        
        _ensure_user_in_db(user, _get_config_snapshot())

        global emoteMap
        emote_positions = event.get("emote_positions", {})
        emotes_used = {}
        for eid, positions in emote_positions.items():
            if not eid:
                continue
            name = None
            for start, end in positions:
                if 0 <= start < end <= len(text):
                    name = text[start:end+1]
                    break
            if name:
                url = f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/1.0"
                emotes_used[name] = url
                if name not in emoteMap:
                    emoteMap[name] = url
                    broadcast_sse({"event": "new_emote", "name": name, "url": url})

        with _reward_tts_done_lock:
            if (user, text) in _reward_tts_done:
                _reward_tts_done.discard((user, text))
                return

        def _delayed_chat_log():
            try:
                with _chat_tts_pending_lock:
                    if (user, text) not in _chat_tts_pending:
                        logger.debug(f"Chat dropped: (user='{user}', text='{text[:30]}') not in _chat_tts_pending")
                        return
                    _chat_tts_pending.pop((user, text), None)
                with _reward_tts_done_lock:
                    if (user, text) in _reward_tts_done:
                        logger.debug(f"Chat dropped: (user='{user}', text='{text[:30]}') in _reward_tts_done")
                        _reward_tts_done.discard((user, text))
                        return
                log_to_queue("chat", text, user, emotes_used)
                logger.info(f"💬 {user}: {text[:120]}")
                allowed, processed_text, tts_params = should_tts_message(event)
                if not allowed:
                    logger.debug(f"TTS skipped for {user}: should_tts_message returned False")
                    return
            except Exception as e:
                logger.error(f"_delayed_chat_log error: {e}", exc_info=True)
                return
            tts_wrapper(
                processed_text,
                voice=tts_params.get("voice"),
                rate=tts_params.get("rate"),
                volume=tts_params.get("volume"),
                pitch=tts_params.get("pitch"),
                engine=tts_params.get("engine"),
                language=tts_params.get("language"),
                temperature=tts_params.get("temperature"),
                repetition_penalty=tts_params.get("repetition_penalty"),
                speed=tts_params.get("speed")
            )
        t = threading.Timer(_CHAT_TTS_DELAY, _delayed_chat_log)
        t.daemon = True
        with _chat_tts_pending_lock:
            _chat_tts_pending[(user, text)] = t
        t.start()
    elif event_type == "event":
        process_event(event.get("event_data", event))
    else:
        process_event(event)

def start_event_sub(token, refresh_token, user_id):
    global event_sub_client, event_sub_thread
    if event_sub_client is not None:
        logger.warning("EventSub client already running")
        return

    def eventsub_callback(msg_type, data):
        if msg_type == "event":
            data["_source"] = "eventsub"
            handle_message(data)

    async def async_token_refresher():
        nonlocal token
        r_token = config.get("twitch_refresh_token", "")
        if not r_token:
            return None
        new_token, new_refresh = refresh_twitch_token(r_token)
        if new_token:
            token = new_token
            config["twitch_token"] = new_token
            if new_refresh:
                config["twitch_refresh_token"] = new_refresh
            save_config(config)
            return new_token
        return None

    event_sub_client = TwitchEventSubClient(
        CLIENT_ID, CLIENT_SECRET, token, refresh_token, user_id,
        eventsub_callback, token_refresher=async_token_refresher
    )

    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(event_sub_client.start())
        except Exception as e:
            logger.error(f"❌ EventSub loop error: {e}")
        finally:
            loop.close()

    event_sub_thread = threading.Thread(target=run_loop, daemon=True)
    event_sub_thread.start()
    logger.info("📡 EventSub thread started")

def stop_event_sub():
    global event_sub_client, event_sub_thread
    if event_sub_client:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(event_sub_client.stop())
            loop.close()
        except Exception as e:
            logger.warning(f"Error stopping EventSub: {e}")
    event_sub_client = None
    event_sub_thread = None

_twitch_connecting_lock = threading.Lock()
_twitch_connecting = False

def auto_start_twitch():
    global twitch_bot, twitch_running, config, _twitch_connecting
    if twitch_running:
        logger.info("Twitch уже запущен")
        return
    with _twitch_connecting_lock:
        if _twitch_connecting:
            logger.info("Twitch уже подключается...")
            return
        _twitch_connecting = True

    token = config.get("twitch_token", "").strip()
    channel = config.get("twitch_channel", "").strip()

    if not channel:
        logger.info("⏸️ Нет канала для подключения")
        with _twitch_connecting_lock:
            _twitch_connecting = False
        return

    if token and channel:
        login = config.get("twitch_login", "").strip()
        token_valid = False
        try:
            headers = {
                "Client-ID": CLIENT_ID,
                "Authorization": f"Bearer {token}"
            }
            r = requests.get("https://api.twitch.tv/helix/users", headers=headers, timeout=10)
            if r.status_code == 200:
                token_valid = True
        except Exception as e:
            logger.warning(f"Ошибка проверки токена: {e}")

        if not token_valid:
            refresh = config.get("twitch_refresh_token", "")
            if refresh:
                logger.info("🔄 Токен истёк, пытаюсь обновить через refresh_token...")
                new_token, new_refresh = refresh_twitch_token(refresh)
                if new_token:
                    token = new_token
                    config["twitch_token"] = token
                    if new_refresh:
                        config["twitch_refresh_token"] = new_refresh
                    save_config(config)
                    token_valid = True
                    logger.info("✅ Токен обновлён через refresh_token")
            if not token_valid:
                logger.warning("⚠️ Токен недействителен, запуск без EventSub")
                token = ""

    if token:
        start_event_sub(token, config.get("twitch_refresh_token", ""), config.get("twitch_user_id", ""))
    else:
        logger.info(f"👤 Анонимный режим для {channel} (без токена)")

    try:
        nick = channel.lstrip("#")
        twitch_bot = TwitchIRCBot(
            token=token,
            nick=nick,
            channel=channel,
            tts_callback=handle_message
        )
        twitch_bot.start()
        if twitch_bot.wait_connected(timeout=15):
            twitch_running = True
            broadcast_sse({"event": "twitch_status", "running": True})
            logger.info(f"📺 Автоматический запуск бота для {channel} успешен")
        else:
            clean_ch = channel.lstrip("#")
            logger.error(f"❌ Не удалось подключиться к {clean_ch} (таймаут)")
            log_to_queue("error", f"Не удалось подключиться к {clean_ch}. Проверьте имя канала.")
            broadcast_sse({"event": "twitch_status", "running": False})
            if twitch_bot:
                twitch_bot.stop()
            twitch_bot = None
    except Exception as e:
        logger.error(f"❌ Ошибка автоматического запуска: {e}")
        log_to_queue("error", str(e))
    finally:
        with _twitch_connecting_lock:
            _twitch_connecting = False

# ========== МАРШРУТЫ FLASK ==========
@app.route("/favicon.ico")
def favicon():
    ico = _ASSETS_ROOT / "icons" / "icon.ico"
    if ico.exists():
        return send_file(str(ico), mimetype="image/x-icon")
    return "", 204

@app.route("/")
def index():
    return render_template("index.html", config=config)

@app.route("/api/status")
def api_status():
    engine_type = config.get("tts_engine", "edge-tts")
    return jsonify({
        "tts_ready": _is_engine_ready(engine_type),
        "tts_engine": engine_type,
        "twitch_running": twitch_running,
        "channel": config.get("twitch_channel", ""),
        "login": config.get("twitch_login", ""),
        "queue_size": runtime_events.log_count(),
        "has_token": bool(config.get("twitch_token")),
        "is_anonymous": not bool(config.get("twitch_token"))
    })

@app.route("/api/auth/status")
def auth_status():
    return jsonify({
        "has_token": bool(config.get("twitch_token")),
        "login": config.get("twitch_login", ""),
        "channel": config.get("twitch_channel", "")
    })

@app.route("/api/config", methods=["GET"])
def get_config():
    safe_keys = [
        "voice", "rate", "volume", "pitch", "event_cooldown", "min_length", "max_length",
        "user_cooldown", "filter_broadcaster", "save_audio", "tts_enabled", "read_all_messages",
        "read_only_answered",         "role_filters", "filter_links", "filter_emotes", "filter_emoji", "use_keywords",
        "keywords", "strip_keywords_from_tts", "ignore_chars", "deduplicate_chars", "blacklist_users", "whitelist_users",
        "user_voice_map", "text_replacements", "events",
        "auto_random_voice",
        "tts_engine", "xtts_voice", "xtts_language", "xtts_temperature", "xtts_repetition_penalty",
        "qwen3_voice", "qwen3_language", "qwen3_instruct",
        "qwen3_tone", "qwen3_emotion", "qwen3_speed", "qwen3_pauses",
        "cosyvoice_voice", "cosyvoice_language", "cosyvoice_instruct",
        "cosyvoice_model_dir", "cosyvoice_speed", "cosyvoice_fp16",
        "piper_voice", "piper_default_voice", "piper_max_cached",
        "sapi5_voice", "sapi5_rate", "sapi5_volume"
    ]
    with config_lock:
        result = {k: config.get(k) for k in safe_keys if k in config}
    # Override user_voice_map from DB (source of truth)
    result["user_voice_map"] = voice_map_db.to_config_map()
    # Override events from DB (source of truth)
    result["events"] = event_db.to_config_dict()
    # Convert top-level voice to prefixed form
    eng = result.get("tts_engine", "edge-tts")
    if eng == "qwen3":
        result["voice"] = f"qwen3-{result.get('qwen3_voice', 'Vivian')}"
    elif eng == "xtts":
        result["voice"] = f"xtts-{result.get('xtts_voice', 'ref.wav')}"
    elif eng == "cosyvoice":
        result["voice"] = result.get("cosyvoice_voice", "") or "cosyvoice-clone:default.wav"
    elif eng == "piper":
         result["voice"] = result.get("piper_voice", "") or f"piper-{result.get('piper_default_voice', 'ru_RU-mari-medium_epoch6399')}"
    elif eng == "sapi5":
        result["voice"] = f"sapi5-{result.get('sapi5_voice', 'Microsoft Zira Desktop - English (United States)')}"
    else:
        result["voice"] = f"edge-{result.get('voice', 'ru-RU-SvetlanaNeural')}"
    # Helper: recombine engine + voice into prefixed string for frontend
    def _ensure_prefix(v: str, eng_hint: str = None) -> str:
        if not v:
            return v
        if v.startswith("xtts-") or v.startswith("edge-") or v.startswith("qwen3-") or v.startswith("cosyvoice-") or v.startswith("piper-") or v.startswith("sapi5-"):
            return v
        if eng_hint == "qwen3":
            return f"qwen3-{v}" if not v.startswith("qwen3-") else v
        if eng_hint == "cosyvoice":
            return f"cosyvoice-clone:{v}" if not v.startswith("cosyvoice-") else v
        if eng_hint == "piper":
            return f"piper-{v}" if not v.startswith("piper-") else v
        if eng_hint == "sapi5":
            return f"sapi5-{v}" if not v.startswith("sapi5-") else v
        if eng_hint and eng_hint != "edge-tts":
            return f"xtts-{v}"
        if eng_hint == "edge-tts":
            return f"edge-{v}"
        # Legacy heuristic: .wav = XTTS
        return f"xtts-{v}" if v.endswith(".wav") else f"edge-{v}"
    # Convert user_voice_map voices (now dict with engine + voice from DB)
    uvm = result.get("user_voice_map", {})
    prefixed_uvm = {}
    for user, val in uvm.items():
        if isinstance(val, dict):
            new_val = dict(val)
            v = new_val.get("voice", "")
            e = new_val.pop("engine", None)
            if v:
                new_val["voice"] = _ensure_prefix(v, e)
            prefixed_uvm[user] = new_val
        elif isinstance(val, str):
            prefixed_uvm[user] = _ensure_prefix(val)
        else:
            prefixed_uvm[user] = val
    if prefixed_uvm:
        result["user_voice_map"] = prefixed_uvm
    # Convert event voices
    events = result.get("events", {})
    for ev_name, ev_cfg in events.items():
        if not isinstance(ev_cfg, dict):
            continue
        if "voice" in ev_cfg and ev_cfg["voice"]:
            ev_cfg["voice"] = _ensure_prefix(ev_cfg["voice"])
        if ev_name == "reward" and "reward_voice_map" in ev_cfg:
            rvm = ev_cfg["reward_voice_map"]
            for reward, rval in rvm.items():
                if isinstance(rval, dict) and "voice" in rval and rval["voice"]:
                    rval["voice"] = _ensure_prefix(rval["voice"])
                elif isinstance(rval, str):
                    rvm[reward] = _ensure_prefix(rval)
    return jsonify(result)

def _strip_voice_prefix(val: str) -> str:
    """Remove 'edge-' or 'xtts-' prefix; keep qwen3, cosyvoice, piper, sapi5 identifiers intact (prefix is part of voice name)."""
    if val.startswith("qwen3-") or val.startswith("cosyvoice-") or val.startswith("piper-") or val.startswith("sapi5-"):
        return val
    if val.startswith("xtts-") or val.startswith("edge-"):
        return val[5:]
    return val


def _process_voice_cfg_item(item):
    """Strip prefix from a voice config item (string or dict with 'voice' key)."""
    if isinstance(item, dict):
        if "voice" in item and isinstance(item["voice"], str):
            item["voice"] = _strip_voice_prefix(item["voice"])
        return item
    elif isinstance(item, str):
        return _strip_voice_prefix(item)
    return item


@app.route("/api/config", methods=["POST"])
def api_config():
    global config, tts_engine
    data = request.json or {}

    # Strip prefixes from voice fields BEFORE merging into config
    if "voice" in data:
        v = data["voice"]
        if v.startswith("qwen3-"):
            data["tts_engine"] = "qwen3"
            data["qwen3_voice"] = v[6:]
            data["voice"] = v
        elif v.startswith("cosyvoice-"):
            data["tts_engine"] = "cosyvoice"
            data["cosyvoice_voice"] = v
            data["voice"] = v
        elif v.startswith("piper-"):
            data["tts_engine"] = "piper"
            data["piper_voice"] = v
            data["voice"] = v
        elif v.startswith("sapi5-"):
            data["tts_engine"] = "sapi5"
            data["sapi5_voice"] = v[6:]
            data["voice"] = v[6:]
        elif v.startswith("xtts-"):
            data["tts_engine"] = "xtts"
            data["voice"] = v[5:]
            data["xtts_voice"] = v[5:]
        elif v.startswith("edge-"):
            data["tts_engine"] = "edge-tts"
            data["voice"] = v[5:]

    with config_lock:
        new_config = copy.deepcopy(config)
        for key in data:
            if key in DEFAULT_CONFIG:
                new_config[key] = data[key]

        # Нормализация rate/volume/pitch — добавляем + для неотрицательных значений
        for _param in ("rate", "volume", "pitch"):
            _val = new_config.get(_param)
            if _val is not None:
                new_config[_param] = _normalize_tts_param(str(_val), "Hz" if _param == "pitch" else "%")

        _voice_default = new_config.get("voice", DEFAULT_CONFIG.get("voice", "ru-RU-SvetlanaNeural"))

        # Нормализация user_voice_map — sync with DB
        if "user_voice_map" in data:
            submitted_users = set()
            new_map = {}
            for user, val in data["user_voice_map"].items():
                # Extract prefixed voice and engine BEFORE stripping
                if isinstance(val, dict):
                    raw_voice_val = val.get("voice", "")
                elif isinstance(val, str):
                    raw_voice_val = val
                else:
                    raw_voice_val = ""
                # Split "xtts-ref.wav" into engine="xtts", voice="ref.wav"
                db_engine, db_voice = _parse_prefixed_voice(raw_voice_val) if raw_voice_val else (None, "")
                val = _process_voice_cfg_item(val)
                if isinstance(val, dict):
                    new_map[user] = val
                    voice_map_db.set(user, voice=db_voice, engine=db_engine, rate=val.get("rate", "+0%"), volume=val.get("volume", "+0%"), pitch=val.get("pitch", "+0Hz"),
                                     xtts_language=val.get("xtts_language") or val.get("language", "ru"), xtts_temperature=val.get("xtts_temperature") or val.get("temperature", 0.85))
                elif isinstance(val, str):
                    new_map[user] = {"voice": val, "engine": db_engine, "rate": new_config.get("rate", "+0%"), "volume": new_config.get("volume", "+0%"), "pitch": new_config.get("pitch", "+0Hz")}
                    voice_map_db.set(user, voice=db_voice, engine=db_engine, rate=new_config.get("rate", "+0%"), volume=new_config.get("volume", "+0%"), pitch=new_config.get("pitch", "+0Hz"))
                else:
                    new_map[user] = {"voice": _voice_default, "rate": new_config.get("rate", "+0%"), "volume": new_config.get("volume", "+0%"), "pitch": new_config.get("pitch", "+0Hz")}
                    voice_map_db.set(user, voice="", engine=None, rate=new_config.get("rate", "+0%"), volume=new_config.get("volume", "+0%"), pitch=new_config.get("pitch", "+0Hz"))
                submitted_users.add(user)
            # Delete from DB users not in submitted list
            for existing_user in voice_map_db.get_all():
                if existing_user not in submitted_users:
                    voice_map_db.delete(existing_user)
                    logger.info(f"🗑️ Deleted user '{existing_user}' from voice DB")
            new_config["user_voice_map"] = new_map

        # Нормализация reward_voice_map в событиях
        if "events" in data:
            for ev_name, ev_cfg in data["events"].items():
                if not isinstance(ev_cfg, dict):
                    continue
                # Keep prefix on event voice (needed for runtime to determine engine)
                if ev_name == "reward" and "reward_voice_map" in ev_cfg:
                    reward_map = ev_cfg["reward_voice_map"]
                    new_reward_map = {}
                    for reward, val in reward_map.items():
                        if isinstance(val, dict):
                            new_reward_map[reward] = val
                        elif isinstance(val, str):
                            new_reward_map[reward] = {"voice": val, "rate": ev_cfg.get("rate", new_config.get("rate", "+0%")), "volume": ev_cfg.get("volume", new_config.get("volume", "+0%")), "pitch": ev_cfg.get("pitch", new_config.get("pitch", "+0Hz"))}
                        else:
                            new_reward_map[reward] = {"voice": _voice_default, "rate": new_config.get("rate", "+0%"), "volume": new_config.get("volume", "+0%"), "pitch": new_config.get("pitch", "+0Hz")}
                    ev_cfg["reward_voice_map"] = new_reward_map
                # Удаляем устаревшие ключи
                ev_cfg.pop("enable_unmapped_rewards", None)
                ev_cfg.pop("default_voice", None)
                # Сохраняем событие в event_db
                reward_map = ev_cfg.pop("reward_voice_map", {})
                event_db.set_config(ev_name, ev_cfg)
                for reward, val in reward_map.items():
                    if isinstance(val, dict):
                        event_db.set_reward_mapping(reward, val)
                    elif isinstance(val, str):
                        event_db.set_reward_mapping(reward, {"voice": val})
                if ev_name == "reward":
                    ev_cfg["reward_voice_map"] = reward_map

        if save_config(new_config):
            config = new_config
            config_was_saved = True
        else:
            config_was_saved = False

    if config_was_saved:
        new_engine = config.get("tts_engine", "edge-tts")
        if new_engine == "xtts":
            if not isinstance(tts_engine, TTSEngine):
                tts_engine = TTSEngine(voice=config.get("voice", DEFAULT_CONFIG.get("voice", "ru-RU-SvetlanaNeural")))
            logger.info("XTTS engine selected (separate worker)")
        elif new_engine in ("qwen3", "cosyvoice", "piper", "sapi5"):
            if not isinstance(tts_engine, TTSEngine):
                tts_engine = TTSEngine(voice=config.get("voice", DEFAULT_CONFIG.get("voice", "ru-RU-SvetlanaNeural")))
            engine_name = {"qwen3": "Qwen3", "cosyvoice": "CosyVoice3", "piper": "Piper", "sapi5": "SAPI5"}.get(new_engine, new_engine)
            logger.info(f"{engine_name} engine selected (separate worker)")
        else:
            if not isinstance(tts_engine, TTSEngine):
                tts_engine = TTSEngine(voice=config.get("voice", DEFAULT_CONFIG.get("voice", "ru-RU-SvetlanaNeural")))
            tts_engine.voice = config.get("voice", DEFAULT_CONFIG.get("voice", "ru-RU-SvetlanaNeural"))
        logger.info(" Config saved")
        return jsonify({"status": "saved"})
    return jsonify({"error": "Save failed"}), 500

@app.route("/api/logs")
def api_logs():
    return jsonify(runtime_events.logs(limit=50))

@app.route("/api/emotes")
def api_emotes():
    global cached_emotes, emotes_last_fetch, emoteMap
    now = time.time()
    if cached_emotes and (now - emotes_last_fetch) < EMOTES_CACHE_TTL:
        return jsonify(cached_emotes)

    token = config.get("twitch_token", "").strip()
    user_id = config.get("twitch_user_id", "").strip()
    login = config.get("twitch_login", "").strip()
    emotes = {}

    if not token or not user_id:
        cached_emotes = emotes
        emotes_last_fetch = now
        emoteMap = emotes
        return jsonify(emotes)

    headers = {
        "Client-ID": CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    session = requests.Session()
    session.headers.update({"User-Agent": "TwitchTTS/1.0"})

    def fetch_with_retry(url, headers=None, max_retries=1, timeout=8):
        for attempt in range(max_retries):
            try:
                r = session.get(url, headers=headers, timeout=timeout) if headers else session.get(url, timeout=timeout)
                return r
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                else:
                    raise
            except requests.exceptions.RequestException:
                raise

    try:
        # Twitch global emotes
        try:
            r = fetch_with_retry("https://api.twitch.tv/helix/chat/emotes/global", headers=headers)
            if r.status_code == 200:
                for e in r.json().get("data", []):
                    emotes[e["name"]] = f"https://static-cdn.jtvnw.net/emoticons/v2/{e['id']}/default/dark/1.0"
        except Exception as e:
            logger.warning(f"Twitch global emotes: {e}")

        # Twitch channel emotes
        try:
            r = fetch_with_retry(f"https://api.twitch.tv/helix/chat/emotes?broadcaster_id={user_id}", headers=headers)
            if r.status_code == 200:
                for e in r.json().get("data", []):
                    emotes[e["name"]] = f"https://static-cdn.jtvnw.net/emoticons/v2/{e['id']}/default/dark/1.0"
        except Exception as e:
            logger.warning(f"Twitch channel emotes: {e}")

        # Twitch emote sets (from IRC — subscriber emotes of other channels)
        if twitch_bot and twitch_bot.emote_sets:
            for es_id in twitch_bot.emote_sets:
                if es_id in ('0', ''):
                    continue
                try:
                    r = fetch_with_retry(f"https://api.twitch.tv/helix/chat/emotes/set?emote_set_id={es_id}", headers=headers)
                    if r.status_code == 200:
                        for e in r.json().get("data", []):
                            emotes[e["name"]] = f"https://static-cdn.jtvnw.net/emoticons/v2/{e['id']}/default/dark/1.0"
                except Exception as e:
                    logger.warning(f"Twitch emote set {es_id}: {e}")

        # BTTV global
        try:
            r = fetch_with_retry("https://api.betterttv.net/3/cached/emotes/global", timeout=15, max_retries=2)
            if r.status_code == 200:
                for e in r.json():
                    emotes[e["code"]] = f"https://cdn.betterttv.net/emote/{e['id']}/1x"
        except Exception as e:
            logger.warning(f"BTTV global: {e}")

        # BTTV channel
        if login:
            try:
                r = fetch_with_retry(f"https://api.betterttv.net/3/cached/users/twitch/{user_id}", timeout=15, max_retries=2)
                if r.status_code == 200:
                    bttv_data = r.json()
                    for e in bttv_data.get("channelEmotes", []):
                        emotes[e["code"]] = f"https://cdn.betterttv.net/emote/{e['id']}/1x"
                    for e in bttv_data.get("sharedEmotes", []):
                        emotes[e["code"]] = f"https://cdn.betterttv.net/emote/{e['id']}/1x"
            except Exception as e:
                logger.warning(f"BTTV channel: {e}")

        # 7TV global
        try:
            r = fetch_with_retry("https://7tv.io/v3/emote-sets/global", timeout=25, max_retries=3)
            if r.status_code == 200:
                data = r.json()
                for e in data.get("emotes", []):
                    name = e["name"]
                    host = e.get("host", {})
                    files = host.get("files", [])
                    if files:
                        url = next((f"https:{host['url']}/{f['name']}" for f in files if f.get("name", "").endswith("1x.webp")), None)
                        if not url:
                            url = f"https:{host['url']}/{files[0]['name']}"
                        emotes[name] = url
        except Exception as e:
            logger.warning(f"7TV global: {e}")

        # 7TV channel
        if login:
            try:
                r = fetch_with_retry(f"https://7tv.io/v3/users/twitch/{user_id}", timeout=25, max_retries=3)
                if r.status_code == 200:
                    user_data = r.json()
                    for e in user_data.get("emote_set", {}).get("emotes", []):
                        name = e["name"]
                        host = e.get("host", {})
                        files = host.get("files", [])
                        if files:
                            url = next((f"https:{host['url']}/{f['name']}" for f in files if f.get("name", "").endswith("1x.webp")), None)
                            if not url:
                                url = f"https:{host['url']}/{files[0]['name']}"
                            emotes[name] = url
            except Exception as e:
                logger.warning(f"7TV channel: {e}")

        # FFZ global
        try:
            r = fetch_with_retry("https://api.frankerfacez.com/v1/emotes", timeout=25, max_retries=3)
            if r.status_code == 200:
                data = r.json()
                for set_id, set_data in data.get("sets", {}).items():
                    for e in set_data.get("emoticons", []):
                        name = e.get("name")
                        urls = e.get("urls")
                        if name and urls and "1" in urls:
                            emotes[name] = urls["1"]
        except Exception as e:
            logger.warning(f"FFZ global: {e}")

        # FFZ channel
        if login:
            try:
                r = fetch_with_retry(f"https://api.frankerfacez.com/v1/room/{login}", timeout=25, max_retries=3)
                if r.status_code == 200:
                    data = r.json()
                    for set_id, set_data in data.get("sets", {}).items():
                        for e in set_data.get("emoticons", []):
                            name = e.get("name")
                            urls = e.get("urls")
                            if name and urls and "1" in urls:
                                emotes[name] = urls["1"]
            except Exception as e:
                logger.warning(f"FFZ channel: {e}")

    except Exception as e:
        logger.error(f"Unexpected error in emotes loading: {e}")

    cached_emotes = emotes
    emotes_last_fetch = now
    emoteMap = emotes
    logger.info(f"Эмоутов загружено: {len(emotes)} (кэш обновлён)")
    return jsonify(emotes)

@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    try:
        raw_voice = (data.get("voice") or "").strip()
        req_engine, actual_voice = _parse_prefixed_voice(raw_voice) if raw_voice else ("edge-tts", "")
        cfg_engine = config.get("tts_engine", "edge-tts")
        engine = data.get("engine") or req_engine or cfg_engine

        if engine == "qwen3":
            result = qwen3_worker.generate(
                text=text,
                voice=data.get("qwen3_voice") or actual_voice or config.get("qwen3_voice", "Vivian"),
                language=data.get("qwen3_language") or config.get("qwen3_language", "Russian"),
                instruct=data.get("qwen3_instruct") or config.get("qwen3_instruct", ""),
            )
            return jsonify({"success": True, "output": result.get("filename")})
        elif engine == "cosyvoice" and cosyvoice_worker:
            result = cosyvoice_worker.generate(
                text=text,
                voice=data.get("cosyvoice_voice") or actual_voice or config.get("cosyvoice_voice", ""),
                language=data.get("cosyvoice_language") or config.get("cosyvoice_language", "Russian"),
                instruct=data.get("cosyvoice_instruct") or config.get("cosyvoice_instruct", ""),
            )
            return jsonify({"success": True, "output": result.get("filename")})
        elif engine == "xtts" and xtts_worker:
            result = xtts_worker.generate(
                text=text,
                voice=actual_voice or data.get("xtts_voice") or config.get("xtts_voice", "female_01.wav"),
                language=data.get("language") or data.get("xtts_language") or config.get("xtts_language", "ru"),
                temperature=float(data.get("temperature") or data.get("xtts_temperature") or config.get("xtts_temperature", 0.85)),
                repetition_penalty=float(data.get("repetition_penalty") or data.get("xtts_repetition_penalty") or config.get("xtts_repetition_penalty", 20)),
            )
            output_path = result.get("filename")
        elif engine == "piper" and piper_engine:
            # Предобработка текста
            processed_text = preprocess_text(text)
            voice_name = actual_voice or data.get("piper_voice") or config.get("piper_voice", "")
            filename = piper_engine.synthesize_to_file(
                text=processed_text,
                voice=voice_name,
                speaker_id=data.get("speaker_id"),
            )
            return jsonify({"success": True, "output": filename})
        elif engine == "sapi5" and sapi5_engine.is_ready():
            output_path = sapi5_engine.generate(
                text=text,
                voice=actual_voice or data.get("sapi5_voice") or config.get("sapi5_voice", ""),
                rate=int(data.get("sapi5_rate") or config.get("sapi5_rate", 0)),
                volume=float(data.get("sapi5_volume") or config.get("sapi5_volume", 1.0)),
            )
            return jsonify({"success": True, "output": Path(output_path).name})
        else:
            voice = actual_voice or config.get("voice", "ru-RU-SvetlanaNeural")
            if voice and not voice.startswith("ru-RU-") and not voice.startswith("en-"):
                voice = "ru-RU-SvetlanaNeural"
            output_path = tts_engine.generate(
                text=text,
                voice=voice,
                rate=_normalize_tts_param(data.get("rate") or config.get("rate", "+0%"), '%'),
                volume=_normalize_tts_param(data.get("volume") or config.get("volume", "+0%"), '%'),
                pitch=_normalize_tts_param(data.get("pitch") or config.get("pitch", "+0Hz"), 'Hz'),
            )
        return jsonify({"success": True, "output": Path(output_path).name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/latest")
def api_latest():
    file_name = request.args.get("file")
    if file_name:
        target = OUTPUTS_DIR / secure_filename(file_name)
        if target.exists():
            mime = "audio/wav" if target.suffix.lower() == ".wav" else "audio/mpeg"
            resp = send_file(target, mimetype=mime)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
        return jsonify({"error": "File not found"}), 404
    files = list(OUTPUTS_DIR.glob("*.mp3"))
    if not files:
        return jsonify({"error": "No audio"}), 404
    latest = max(files, key=lambda f: f.stat().st_mtime)
    resp = send_file(latest, mimetype="audio/mpeg")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/api/tts/stream")
def api_tts_stream():
    text = request.args.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    rate = _normalize_tts_param(request.args.get("rate") or config.get("rate", "+0%"), '%')
    volume = _normalize_tts_param(request.args.get("volume") or config.get("volume", "+0%"), '%')
    pitch = _normalize_tts_param(request.args.get("pitch") or config.get("pitch", "+0Hz"), 'Hz')

    raw_voice = (request.args.get("voice") or "").strip()
    req_engine, voice = _parse_prefixed_voice(raw_voice) if raw_voice else ("edge-tts", "")
    req_engine = request.args.get("engine") or req_engine

    # When XTTS is the active engine, use it for requests without explicit engine
    if not request.args.get("engine") and config.get("tts_engine") == "xtts":
        req_engine = "xtts"

    # Use specified engine for this stream request
    if req_engine == "qwen3":
        q_voice = voice or config.get("qwen3_voice", "Vivian")
        q_lang = request.args.get("qwen3_language") or config.get("qwen3_language", "Russian")
        q_instruct = request.args.get("qwen3_instruct") or config.get("qwen3_instruct", "")
        audio_data = qwen3_worker.generate_bytes(
            text=text, voice=q_voice, language=q_lang, instruct=q_instruct,
        )
        return Response(audio_data, mimetype="audio/wav",
                        headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})
    if req_engine == "cosyvoice" and cosyvoice_worker:
        c_voice = voice or config.get("cosyvoice_voice", "")
        c_lang = request.args.get("cosyvoice_language") or config.get("cosyvoice_language", "Russian")
        c_instruct = request.args.get("cosyvoice_instruct") or config.get("cosyvoice_instruct", "")
        audio_data = cosyvoice_worker.generate_bytes(
            text=text, voice=c_voice, language=c_lang, instruct=c_instruct,
        )
        return Response(audio_data, mimetype="audio/wav",
                        headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})
    if req_engine == "xtts" and xtts_worker:
        xtts_voice = voice or config.get("xtts_voice", "female_01.wav")
        xtts_language = request.args.get("language") or config.get("xtts_language", "ru")
        xtts_temperature = float(request.args.get("temperature") or config.get("xtts_temperature", 0.85))
        xtts_repetition_penalty = float(request.args.get("repetition_penalty") or config.get("xtts_repetition_penalty", 20))
        xtts_speed = float(request.args.get("speed") or config.get("xtts_speed", 1.0))
        audio_data = xtts_worker.generate_bytes(
            text=text,
            voice=xtts_voice,
            language=xtts_language,
            temperature=xtts_temperature,
            repetition_penalty=xtts_repetition_penalty,
            speed=xtts_speed,
        )
        return Response(audio_data, mimetype="audio/wav",
                        headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})
    if req_engine == "piper" and piper_engine:
        piper_voice = voice or config.get("piper_voice", "")
        speaker_id = request.args.get("speaker_id")
        processed_text = preprocess_text(text)
        wav_data = piper_engine.synthesize(
            text=processed_text,
            voice=piper_voice,
            speaker_id=int(speaker_id) if speaker_id else None,
        )
        return Response(wav_data, mimetype="audio/wav",
                        headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})
    if req_engine == "sapi5" and sapi5_engine.is_ready():
        s_voice = voice or config.get("sapi5_voice", "")
        s_rate = int(request.args.get("sapi5_rate") or config.get("sapi5_rate", 0))
        s_volume = float(request.args.get("sapi5_volume") or config.get("sapi5_volume", 1.0))
        output_path = sapi5_engine.generate(
            text=text,
            voice=s_voice,
            rate=s_rate,
            volume=s_volume,
        )
        target = Path(output_path)
        if target.exists():
            return send_file(target, mimetype="audio/wav",
                            headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})
        return jsonify({"error": "File not found"}), 404

    voice = voice or config.get("voice", "ru-RU-SvetlanaNeural")
    if voice and not voice.startswith("ru-RU-") and not voice.startswith("en-"):
        voice = "ru-RU-SvetlanaNeural"

    def generate():
        try:
            for chunk in tts_engine.generate_stream(text=text, voice=voice, rate=rate, volume=volume, pitch=pitch):
                yield chunk
        except Exception as e:
            logger.error(f"Stream TTS error: {e}")
            yield b""

    return Response(generate(), mimetype="audio/mpeg", headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})

@app.route("/api/send_chat", methods=["POST"])
def api_send_chat():
    global twitch_bot
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "error": "Пустой текст"}), 400
    if not twitch_bot or not twitch_bot.is_connected():
        return jsonify({"success": False, "error": "Бот не подключён к чату"}), 503
    if twitch_bot.send_message(text):
        user = config.get("twitch_login", "Аноним")
        event = {
            "type": "chat",
            "user": user,
            "text": text,
            "is_broadcaster": True,
            "is_moderator": True,
            "is_vip": False,
            "is_subscriber": False,
            "is_highlighted": False,
            "is_reply": False,
        }
        handle_message(event)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Ошибка отправки"}), 500

_voice_cache = None
_voice_cache_time = 0
_VOICE_CACHE_TTL = 60


def invalidate_voice_cache():
    global _voice_cache, _voice_cache_time
    _voice_cache = None
    _voice_cache_time = 0


def _build_voices_list() -> dict:
    """Build fresh {voices, languages} from all engines."""
    voices = []
    languages = []
    try:
        for v in TTSEngine().list_voices():
            if v.get("locale") != "ru-RU":
                continue
            name = f"edge-{v['name']}"
            voices.append({"name": name, "gender": v.get("gender", ""), "locale": v.get("locale", ""), "engine": "edge-tts"})
    except Exception as e:
        logger.warning(f"edge-tts voices: {e}")
    try:
        for v in xtts_worker.list_voices():
            voices.append(v)
        languages = xtts_worker.list_languages()
        for i, lang in enumerate(languages):
            if lang["code"] == "ru":
                languages.insert(0, languages.pop(i))
                break
    except Exception as e:
        logger.warning(f"xtts voices: {e}")
    try:
        for v in qwen3_worker.list_voices():
            voices.append(v)
    except Exception as e:
        logger.warning(f"qwen3 voices: {e}")
    try:
        for v in cosyvoice_worker.list_voices():
            voices.append(v)
    except Exception as e:
        logger.warning(f"cosyvoice voices: {e}")
    # Piper voices from engine
    try:
        if piper_engine:
            for v in piper_engine.list_voices():
                voices.append(v)
            logger.info(f"Piper voices found: {len(piper_engine.list_voices())}")
    except Exception as e:
        logger.warning(f"piper voices: {e}")
    try:
        sapi5_voices = sapi5_engine.list_voices()
        logger.info(f"SAPI5 voices found: {len(sapi5_voices)}")
        for v in sapi5_voices:
            voices.append(v)
    except Exception as e:
        logger.warning(f"sapi5 voices: {e}")
    return {"voices": voices, "languages": languages}


@app.route("/api/voices")
def api_voices():
    global _voice_cache, _voice_cache_time
    now = time.time()
    if _voice_cache is not None and now - _voice_cache_time < _VOICE_CACHE_TTL:
        return jsonify(_voice_cache)
    try:
        result = _build_voices_list()
        _voice_cache = result
        _voice_cache_time = time.time()
        return jsonify(result)
    except:
        return jsonify({"voices": [], "languages": []})

@app.route("/api/tts/engine", methods=["GET"])
def api_tts_engine():
    engine_type = config.get("tts_engine", "edge-tts")
    ready = _is_engine_ready(engine_type)
    info = {"engine": engine_type, "ready": ready}
    if engine_type in ("xtts", "qwen3", "cosyvoice", "piper", "sapi5"):
        info["model_downloaded"] = True
        info["download_progress"] = {"total": 1, "downloaded": 1, "sizes": {}}
    return jsonify(info)

@app.route("/api/voices/upload", methods=["POST"])
def api_voices_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in (".wav", ".mp3", ".ogg", ".flac"):
        return jsonify({"error": "Unsupported format. Use wav, mp3, ogg, flac"}), 400
    filename = secure_filename(file.filename)
    dest = VOICES_DIR / filename
    file.save(str(dest))
    logger.info(f"Voice uploaded: {filename}")
    return jsonify({"status": "ok", "filename": filename})

@app.route("/api/tts/languages")
def api_tts_languages():
    if config.get("tts_engine") in ("qwen3", "cosyvoice"):
        return jsonify([{"code": lang, "name": lang} for lang in
                        ["Chinese", "English", "Japanese", "Korean",
                         "German", "French", "Russian", "Portuguese",
                         "Spanish", "Italian"]])
    if xtts_worker:
        return jsonify(xtts_worker.list_languages())
    return jsonify([])

@app.route("/api/sse")
def api_sse():
    def event_stream():
        client_queue = runtime_events.add_sse_client()
        try:
            yield f"data: {json.dumps({'event': 'connected'})}\n\n"
            while True:
                try:
                    msg = client_queue.get(timeout=30)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            runtime_events.remove_sse_client(client_queue)
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/api/voice_map/<username>", methods=["DELETE"])
def api_delete_voice_map_user(username: str):
    """Delete a single user from the voice DB immediately."""
    voice_map_db.delete(username)
    with config_lock:
        config.get("user_voice_map", {}).pop(username, None)
        save_config(config)
    logger.info(f"🗑️ Deleted user '{username}' from voice DB")
    return jsonify({"status": "deleted"})

@app.route("/api/test_event", methods=["POST"])
def test_event():
    """Универсальный тест событий: follow, subscription, subscription_gift, cheer, raid, reward"""
    data = request.json or {}
    event_type = data.get("type")
    if not event_type:
        return jsonify({"error": "Missing 'type' field"}), 400

    event_data = {"type": event_type}

    if event_type == "follow":
        event_data["user"] = data.get("user", "TestFollower")
    elif event_type == "subscription":
        event_data["user"] = data.get("user", "TestSubscriber")
        event_data["tier"] = data.get("tier", "Tier 1")
    elif event_type == "subscription_gift":
        event_data["user"] = data.get("user", "TestGifter")
        event_data["total"] = data.get("total", 5)
    elif event_type == "cheer":
        event_data["user"] = data.get("user", "TestCheerer")
        event_data["bits"] = data.get("bits", 100)
    elif event_type == "raid":
        event_data["user"] = data.get("user", "TestRaidLeader")
        event_data["viewers"] = data.get("viewers", 10)
    elif event_type == "reward":
        event_data["user"] = data.get("user", "TestUser")
        event_data["reward_name"] = data.get("reward_name", "Тестовая награда")
        event_data["message"] = data.get("message", "")
    elif event_type == "hype_train":
        event_data["user"] = data.get("user", "TestUser")
        event_data["level"] = data.get("level", 1)
        event_data["total"] = data.get("total", 1000)
    elif event_type == "goal":
        event_data["user"] = data.get("user", "TestUser")
        event_data["goal_name"] = data.get("goal_name", "100 подписчиков")
        event_data["goal_type"] = data.get("goal_type", "follower")
        event_data["current_amount"] = data.get("current_amount", 50)
        event_data["target_amount"] = data.get("target_amount", 100)
    elif event_type == "watch_streak":
        event_data["user"] = data.get("user", "TestUser")
        event_data["streak"] = data.get("streak", 5)
        event_data["input_raw"] = data.get("input_raw", "Test message for streak")
    else:
        return jsonify({"error": f"Unknown event type: {event_type}"}), 400

    logger.info(f"🧪 ТЕСТОВОЕ СОБЫТИЕ: {event_data}")
    process_event(event_data)
    return jsonify({"status": f"Event {event_type} processed"})

@app.route("/api/debug/config", methods=["GET"])
def debug_config():
    """Отладка: показать текущую конфигурацию события reward"""
    reward_cfg = config.get("events", {}).get("reward", {})
    return jsonify({
        "reward_enabled": reward_cfg.get("enabled"),
        "format_no_msg": reward_cfg.get("format_no_msg"),
        "format_with_msg": reward_cfg.get("format_with_msg"),
        "voice": reward_cfg.get("voice"),
        "reward_voice_map": reward_cfg.get("reward_voice_map", {})
    })

@app.route("/api/twitch/start", methods=["POST"])
def twitch_start():
    return jsonify({"error": "Manual start disabled, bot starts automatically"}), 400

@app.route("/api/twitch/stop", methods=["POST"])
def twitch_stop():
    global twitch_bot, twitch_running
    if not twitch_running:
        return jsonify({"status": "not_running"})
    try:
        if twitch_bot:
            twitch_bot.stop()
        stop_event_sub()
        twitch_running = False
        broadcast_sse({"event": "twitch_status", "running": False})
        logger.info("🔌 Bot stopped")
        log_to_queue("system", "Бот отключён")
        return jsonify({"status": "stopped"})
    except Exception as e:
        logger.error(f"❌ Stop error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/twitch/test", methods=["POST"])
def twitch_test():
    if not twitch_running or not twitch_bot:
        return jsonify({"error": "Bot not running"}), 400
    data = request.json or {}
    message = data.get("message", "🔊 Test message")
    if twitch_bot.send_message(message):
        return jsonify({"status": "sent"})
    else:
        return jsonify({"error": "Failed to send"}), 500

@app.route("/api/twitch/channel", methods=["POST"])
def twitch_set_channel():
    global twitch_bot, twitch_running, config
    data = request.json or {}
    channel = data.get("channel", "").strip().lstrip("#").lower()
    if not channel:
        return jsonify({"error": "Channel name required"}), 400
    channel = f"#{channel}"
    if twitch_running:
        if twitch_bot:
            twitch_bot.stop()
        stop_event_sub()
        twitch_bot = None
        twitch_running = False
    with config_lock:
        config["twitch_channel"] = channel
        if not config.get("twitch_token", "").strip():
            config["twitch_login"] = channel.lstrip("#")
        save_config(config)
    threading.Thread(target=auto_start_twitch, daemon=True).start()
    logger.info(f"📺 Переключение на канал {channel}")
    return jsonify({"status": "connecting", "channel": channel})

@app.route("/api/twitch/logout", methods=["POST"])
def twitch_logout():
    global twitch_bot, twitch_running
    if twitch_running:
        if twitch_bot:
            twitch_bot.stop()
        stop_event_sub()
        twitch_bot = None
        twitch_running = False
    with config_lock:
        config["twitch_token"] = ""
        config["twitch_refresh_token"] = ""
        config["twitch_user_id"] = ""
        config["twitch_login"] = config.get("twitch_channel", "").lstrip("#")
        save_config(config)
    logger.info("🔑 Токены Twitch удалены")
    log_to_queue("system", "🔑 Токены Twitch удалены, канал сохранён")
    return jsonify({"status": "logged_out"})

@app.route("/api/twitch/auth", methods=["POST"])
def twitch_manual_auth():
    """Ручной запуск OAuth из интерфейса"""
    global twitch_bot, twitch_running, config
    logger.info("🔐 Ручной запуск OAuth...")
    token, user_id, login, refresh_token = perform_full_oauth()
    if not token:
        return jsonify({"error": "Ошибка авторизации"}), 500
    channel = f"#{login}"
    with config_lock:
        config["twitch_token"] = token
        config["twitch_refresh_token"] = refresh_token
        config["twitch_channel"] = channel
        config["twitch_user_id"] = user_id
        config["twitch_login"] = login
        save_config(config)
    if twitch_running:
        if twitch_bot:
            twitch_bot.stop()
        stop_event_sub()
        twitch_bot = None
        twitch_running = False
    threading.Thread(target=auto_start_twitch, daemon=True).start()
    logger.info(f"✅ OAuth успешен: {login}")
    return jsonify({"status": "authorized", "login": login})

@app.route("/api/twitch/reset", methods=["POST"])
def twitch_reset():
    global twitch_bot, twitch_running
    if twitch_running:
        if twitch_bot:
            twitch_bot.stop()
        stop_event_sub()
        twitch_bot = None
        twitch_running = False
    with config_lock:
        config["twitch_token"] = ""
        config["twitch_refresh_token"] = ""
        config["twitch_user_id"] = ""
        config["twitch_login"] = ""
        config["twitch_channel"] = ""
        save_config(config)
    logger.info("🧹 Полный сброс Twitch-авторизации и канала")
    return jsonify({"status": "reset"})

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Server error"}), 500

def print_banner():
    cfg = _get_config_snapshot()
    eng = cfg.get("tts_engine", "edge-tts")
    if eng == "qwen3":
        engine_name = f"Qwen3-TTS ({cfg.get('qwen3_voice', 'Vivian')})"
    elif eng == "xtts":
        engine_name = f"XTTSv2 ({cfg.get('xtts_voice', 'ref.wav')})"
    elif eng == "cosyvoice":
        engine_name = f"CosyVoice3 ({cfg.get('cosyvoice_voice', 'default')})"
    elif eng == "piper":
         engine_name = f"Piper-TTS ({cfg.get('piper_voice', cfg.get('piper_default_voice', 'ru_RU-mari-medium_epoch6399'))})"
    elif eng == "sapi5":
        engine_name = f"SAPI5 ({cfg.get('sapi5_voice', 'Microsoft Zira')})"
    else:
        engine_name = f"edge-tts ({cfg.get('voice', 'ru-RU-SvetlanaNeural')})"
    banner = f"""
╔══════════════════════════════════════════════════════════╗
║  🎙️  Twitch TTS Server v7.9.3 (встроенный Piper)       ║
║  🌐 Web GUI:  http://{cfg.get('host','127.0.0.1')}:{cfg.get('port',5000)}                    ║
║  🎙️  TTS:      {engine_name}           ║
║  💾 Save mode: {'ON' if cfg.get('save_audio', False) else 'OFF (streaming)'}   ║
╚══════════════════════════════════════════════════════════╝
"""
    print(banner)
    logger.info("🚀 Server starting...")

def _configure_workers():
    voices_dir = str(VOICES_DIR.resolve())
    outputs_dir = str(OUTPUTS_DIR.resolve())
    if qwen3_worker.health():
        qwen3_worker.configure(voices_dir=voices_dir, outputs_dir=outputs_dir)
        logger.info(f"Qwen3 worker configured: voices={voices_dir}, outputs={outputs_dir}")
    if cosyvoice_worker.health():
        cosyvoice_worker.configure(voices_dir=voices_dir, outputs_dir=outputs_dir)
        logger.info(f"CosyVoice worker configured: voices={voices_dir}, outputs={outputs_dir}")
    if xtts_worker.health():
        latents_dir = str(VOICES_DIR.parent / "latents")
        xtts_worker.configure(voices_dir=voices_dir, outputs_dir=outputs_dir, latents_dir=latents_dir)
        logger.info(f"XTTS worker configured: voices={voices_dir}, outputs={outputs_dir}")

def run_server():
    global tts_runner, twitch_bot, twitch_running
    pid_dir = Path("data")
    pid_dir.mkdir(exist_ok=True)
    pid_file = pid_dir / "server.pid"
    pid_file.write_text(str(os.getpid()))
    print_banner()
    _configure_workers()
    tts_runner = TTSRunner(
        engine=tts_engine,
        get_config=lambda: config,
        log_callback=log_to_queue,
        event_callback=broadcast_sse,
        xtts_worker=xtts_worker,
        qwen3_worker=qwen3_worker,
        cosyvoice_worker=cosyvoice_worker,
        piper_engine=piper_engine,
        sapi5_worker=sapi5_engine,
    )
    tts_runner.start()
    auto_start_twitch()
    try:
        app.run(
            host=config["host"],
            port=config["port"],
            debug=False,
            threaded=True
        )
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
    finally:
        if tts_runner:
            tts_runner.stop()
        if twitch_running and twitch_bot:
            twitch_bot.stop()
        stop_event_sub()
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
        logger.info("✅ Server stopped")


if __name__ == "__main__":
    run_server()