import sqlite3
import logging
import threading
import time
import json
from pathlib import Path

logger = logging.getLogger(__name__)

DB_DIR = Path("data")
DB_PATH = DB_DIR / "voice_map.db"


class VoiceMapDB:
    def __init__(self, db_path: str = None):
        self._db_path = str(db_path or DB_PATH)
        DB_DIR.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_voices (
                        username TEXT PRIMARY KEY,
                        voice TEXT,
                        engine TEXT DEFAULT 'edge-tts',
                        rate TEXT DEFAULT '+0%',
                        volume TEXT DEFAULT '+0%',
                        pitch TEXT DEFAULT '+0Hz',
                        last_seen REAL DEFAULT 0,
                        created_at REAL DEFAULT 0
                    )
                """)
                # Add engine column if missing on existing DB
                try:
                    conn.execute("ALTER TABLE user_voices ADD COLUMN engine TEXT DEFAULT 'edge-tts'")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE user_voices ADD COLUMN xtts_language TEXT DEFAULT 'ru'")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE user_voices ADD COLUMN xtts_temperature REAL DEFAULT 0.5")
                except sqlite3.OperationalError:
                    pass
                conn.commit()
            finally:
                conn.close()

    def get(self, username: str) -> dict | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM user_voices WHERE username = ?", (username,)).fetchone()
                if row is None:
                    return None
                return dict(row)
            finally:
                conn.close()

    def get_all(self) -> dict[str, dict]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT * FROM user_voices ORDER BY last_seen DESC").fetchall()
                result = {}
                for row in rows:
                    d = dict(row)
                    username = d.pop("username")
                    result[username] = d
                return result
            finally:
                conn.close()

    def set(self, username: str, voice: str = None, engine: str = None, rate: str = "+0%", volume: str = "+0%", pitch: str = "+0Hz",
            xtts_language: str = "ru", xtts_temperature: float = 0.5):
        with self._lock:
            conn = self._conn()
            try:
                now = time.time()
                conn.execute("""
                    INSERT INTO user_voices (username, voice, engine, rate, volume, pitch, xtts_language, xtts_temperature, last_seen, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        voice = excluded.voice,
                        engine = excluded.engine,
                        rate = excluded.rate,
                        volume = excluded.volume,
                        pitch = excluded.pitch,
                        xtts_language = excluded.xtts_language,
                        xtts_temperature = excluded.xtts_temperature,
                        last_seen = excluded.last_seen
                """, (username, voice, engine, rate, volume, pitch, xtts_language, xtts_temperature, now, now))
                conn.commit()
            finally:
                conn.close()

    def set_voice(self, username: str, voice: str = None, engine: str = None,
                  xtts_language: str = "ru", xtts_temperature: float = 0.5):
        """Update only the voice/engine fields and touch last_seen."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO user_voices (username, voice, engine, xtts_language, xtts_temperature, last_seen, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        voice = excluded.voice,
                        engine = excluded.engine,
                        xtts_language = excluded.xtts_language,
                        xtts_temperature = excluded.xtts_temperature,
                        last_seen = excluded.last_seen
                """, (username, voice, engine, xtts_language, xtts_temperature, time.time(), time.time()))
                conn.commit()
            finally:
                conn.close()

    def touch(self, username: str):
        """Update last_seen without changing voice."""
        with self._lock:
            conn = self._conn()
            try:
                now = time.time()
                conn.execute("""
                    INSERT INTO user_voices (username, last_seen, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET last_seen = excluded.last_seen
                """, (username, now, now))
                conn.commit()
            finally:
                conn.close()

    def delete(self, username: str):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM user_voices WHERE username = ?", (username,))
                conn.commit()
            finally:
                conn.close()

    def to_config_map(self) -> dict:
        """Return dict matching config's user_voice_map format with engine info."""
        all_rows = self.get_all()
        cfg_map = {}
        for user, data in all_rows.items():
            voice = data.get("voice")
            engine = data.get("engine", "edge-tts")
            rate = data.get("rate", "+0%")
            volume = data.get("volume", "+0%")
            pitch = data.get("pitch", "+0Hz")
            if not voice:
                cfg_map[user] = {}
            else:
                entry = {"voice": voice, "engine": engine}
                if rate != "+0%" or volume != "+0%" or pitch != "+0Hz":
                    entry["rate"] = rate
                    entry["volume"] = volume
                    entry["pitch"] = pitch
                if engine == "xtts":
                    entry["xtts_language"] = data.get("xtts_language", "ru")
                    entry["xtts_temperature"] = data.get("xtts_temperature", 0.5)
                cfg_map[user] = entry
        return cfg_map

    @staticmethod
    def _detect_engine(voice: str) -> str:
        """Detect engine from voice name: .wav files are XTTS, otherwise edge-tts."""
        if not voice:
            return "edge-tts"
        return "xtts" if voice.endswith(".wav") else "edge-tts"

    def import_from_config(self, cfg_map: dict):
        """Import existing user_voice_map from config on first run."""
        if not cfg_map:
            return
        with self._lock:
            conn = self._conn()
            try:
                existing = conn.execute("SELECT COUNT(*) as c FROM user_voices").fetchone()["c"]
                if existing > 0:
                    return
                now = time.time()
                for user, val in cfg_map.items():
                    if isinstance(val, dict):
                        voice = val.get("voice")
                        engine = val.get("engine") or self._detect_engine(voice)
                        rate = val.get("rate", "+0%")
                        volume = val.get("volume", "+0%")
                        pitch = val.get("pitch", "+0Hz")
                        xtts_language = val.get("xtts_language", "ru")
                        xtts_temperature = val.get("xtts_temperature", 0.5)
                    else:
                        voice = val if isinstance(val, str) else None
                        engine = self._detect_engine(voice)
                        rate = "+0%"
                        volume = "+0%"
                        pitch = "+0Hz"
                        xtts_language = "ru"
                        xtts_temperature = 0.5
                    conn.execute("""
                        INSERT OR IGNORE INTO user_voices (username, voice, engine, rate, volume, pitch, xtts_language, xtts_temperature, last_seen, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (user, voice, engine, rate, volume, pitch, xtts_language, xtts_temperature, now, now))
                conn.commit()
            finally:
                conn.close()


class EventConfigDB:
    """Store event configurations in DB instead of JSON config file."""

    def __init__(self, db_path: str = None):
        self._db_path = str(db_path or DB_PATH)
        DB_DIR.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS event_configs (
                        event_type TEXT PRIMARY KEY,
                        config TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS event_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        min_range REAL,
                        max_range REAL,
                        FOREIGN KEY (event_type) REFERENCES event_configs(event_type) ON DELETE CASCADE
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS reward_voice_map (
                        reward_name TEXT NOT NULL,
                        voice TEXT,
                        engine TEXT,
                        rate TEXT DEFAULT '+0%',
                        volume TEXT DEFAULT '+0%',
                        pitch TEXT DEFAULT '+0Hz',
                        language TEXT DEFAULT 'ru',
                        PRIMARY KEY (reward_name)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS reward_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        reward_name TEXT NOT NULL,
                        message TEXT NOT NULL,
                        FOREIGN KEY (reward_name) REFERENCES reward_voice_map(reward_name) ON DELETE CASCADE
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    def get_all_configs(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT * FROM event_configs").fetchall()
                return {r["event_type"]: json.loads(r["config"]) for r in rows}
            finally:
                conn.close()

    def get_config(self, event_type: str) -> dict | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM event_configs WHERE event_type = ?", (event_type,)).fetchone()
                return json.loads(row["config"]) if row else None
            finally:
                conn.close()

    def set_config(self, event_type: str, config: dict):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO event_configs (event_type, config) VALUES (?, ?)
                    ON CONFLICT(event_type) DO UPDATE SET config = excluded.config
                """, (event_type, json.dumps(config, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()

    def delete_config(self, event_type: str):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM event_messages WHERE event_type = ?", (event_type,))
                conn.execute("DELETE FROM event_configs WHERE event_type = ?", (event_type,))
                conn.commit()
            finally:
                conn.close()

    def get_messages(self, event_type: str) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM event_messages WHERE event_type = ? ORDER BY id", (event_type,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def set_messages(self, event_type: str, messages: list[dict]):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM event_messages WHERE event_type = ?", (event_type,))
                for msg in messages:
                    conn.execute(
                        "INSERT INTO event_messages (event_type, message, min_range, max_range) VALUES (?, ?, ?, ?)",
                        (event_type, msg["message"], msg.get("min_range"), msg.get("max_range"))
                    )
                conn.commit()
            finally:
                conn.close()

    def import_from_config(self, events_cfg: dict):
        if not events_cfg:
            return
        with self._lock:
            conn = self._conn()
            try:
                existing = conn.execute("SELECT COUNT(*) as c FROM event_configs").fetchone()["c"]
                if existing > 0:
                    return
                for event_type, cfg in events_cfg.items():
                    if not isinstance(cfg, dict):
                        continue
                    reward_map = cfg.pop("reward_voice_map", {})
                    conn.execute(
                        "INSERT OR IGNORE INTO event_configs (event_type, config) VALUES (?, ?)",
                        (event_type, json.dumps(cfg, ensure_ascii=False))
                    )
                    for reward_name, rcfg in reward_map.items():
                        if isinstance(rcfg, str):
                            conn.execute(
                                "INSERT OR IGNORE INTO reward_voice_map (reward_name, voice) VALUES (?, ?)",
                                (reward_name, rcfg)
                            )
                        elif isinstance(rcfg, dict):
                            conn.execute(
                                "INSERT OR IGNORE INTO reward_voice_map (reward_name, voice, engine, rate, volume, pitch, language) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (reward_name, rcfg.get("voice"), rcfg.get("engine"),
                                 rcfg.get("rate", "+0%"), rcfg.get("volume", "+0%"),
                                 rcfg.get("pitch", "+0Hz"), rcfg.get("language", "ru"))
                            )
                conn.commit()
            finally:
                conn.close()

    def get_all_reward_mappings(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT * FROM reward_voice_map").fetchall()
                result = {}
                for row in rows:
                    d = dict(row)
                    name = d.pop("reward_name")
                    msgs = conn.execute(
                        "SELECT message FROM reward_messages WHERE reward_name = ?", (name,)
                    ).fetchall()
                    if msgs:
                        d["messages"] = [m["message"] for m in msgs]
                    result[name] = d
                return result
            finally:
                conn.close()

    def set_reward_mapping(self, reward_name: str, cfg: dict):
        with self._lock:
            conn = self._conn()
            try:
                messages = cfg.pop("messages", None)
                conn.execute("""
                    INSERT INTO reward_voice_map (reward_name, voice, engine, rate, volume, pitch, language)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(reward_name) DO UPDATE SET
                        voice=excluded.voice, engine=excluded.engine,
                        rate=excluded.rate, volume=excluded.volume,
                        pitch=excluded.pitch, language=excluded.language
                """, (reward_name, cfg.get("voice"), cfg.get("engine"),
                      cfg.get("rate", "+0%"), cfg.get("volume", "+0%"),
                      cfg.get("pitch", "+0Hz"), cfg.get("language", "ru")))
                if messages:
                    conn.execute("DELETE FROM reward_messages WHERE reward_name = ?", (reward_name,))
                    for msg in messages:
                        conn.execute("INSERT INTO reward_messages (reward_name, message) VALUES (?, ?)", (reward_name, msg))
                conn.commit()
            finally:
                conn.close()

    def delete_reward_mapping(self, reward_name: str):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM reward_messages WHERE reward_name = ?", (reward_name,))
                conn.execute("DELETE FROM reward_voice_map WHERE reward_name = ?", (reward_name,))
                conn.commit()
            finally:
                conn.close()

    def to_config_dict(self) -> dict:
        all_cfgs = self.get_all_configs()
        result = dict(all_cfgs)
        reward_cfg = result.get("reward", {})
        if isinstance(reward_cfg, dict):
            reward_cfg["reward_voice_map"] = self.get_all_reward_mappings()
        return result
