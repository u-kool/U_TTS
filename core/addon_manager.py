import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AddonInfo:
    name: str
    display: str
    version: str
    description: str
    port: int
    addon_type: str      # "native" (exe), "python", or "docker"
    addon_dir: Path
    manifest: dict = field(default_factory=dict)

    # Для native / python
    exe: str = ""
    work_dir: str = "."

    # Для docker
    compose_file: str = ""
    service_name: str = ""

    @property
    def exe_path(self) -> Path:
        return self.addon_dir / self.exe

    @property
    def work_path(self) -> Path:
        return self.addon_dir / self.work_dir

    @property
    def worker_script(self) -> Optional[Path]:
        w = self.manifest.get("worker", "")
        return self.addon_dir / w if w else None

    @property
    def compose_path(self) -> Path:
        if self.compose_file:
            return self.addon_dir / self.compose_file
        return self.addon_dir / "docker-compose.yml"

    @property
    def is_native(self) -> bool:
        return self.addon_type == "native"

    @property
    def is_python(self) -> bool:
        return self.addon_type == "python"

    @property
    def is_docker(self) -> bool:
        return self.addon_type == "docker"


class AddonRegistry:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.addons: dict[str, AddonInfo] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self._log_threads: list[threading.Thread] = []
        self._running = True

    # ── Discovery ──────────────────────────────────────────

    def discover(self):
        addons_dir = self.base_dir / "addons"
        if not addons_dir.exists():
            logger.info(f"Addon directory not found: {addons_dir}")
            return
        for entry in sorted(addons_dir.iterdir()):
            if entry.is_dir():
                manifest_path = entry / "addon.json"
                if manifest_path.exists():
                    try:
                        info = self._load_manifest(manifest_path, entry)
                        if info:
                            self.addons[info.name] = info
                            logger.info(
                                f"Discovered addon: {info.display} "
                                f"(v{info.version}, {info.addon_type}, port {info.port})"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to load addon from {entry.name}: {e}")

    def _load_manifest(self, path: Path, addon_dir: Path) -> Optional[AddonInfo]:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        addon_type = data.get("type", "native")
        return AddonInfo(
            name=data["name"],
            display=data.get("display", data["name"]),
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            port=data["port"],
            addon_type=addon_type,
            addon_dir=addon_dir,
            manifest=data,
            exe=data.get("exe", ""),
            work_dir=data.get("work_dir", "."),
            compose_file=data.get("compose_file", ""),
            service_name=data.get("service_name", ""),
        )

    # ── Query ──────────────────────────────────────────────

    def get(self, name: str) -> Optional[AddonInfo]:
        return self.addons.get(name)

    def get_all(self) -> list[AddonInfo]:
        return list(self.addons.values())

    def get_by_type(self, addon_type: str) -> list[AddonInfo]:
        return [a for a in self.addons.values() if a.addon_type == addon_type]

    def get_port(self, name: str) -> Optional[int]:
        info = self.addons.get(name)
        return info.port if info else None

    # ── Start / Stop ───────────────────────────────────────

    def start(self, name: str, env: dict = None) -> bool:
        info = self.addons.get(name)
        if not info:
            logger.error(f"Addon '{name}' not found")
            return False
        if info.is_docker:
            return self._start_docker(info)
        if info.is_python:
            return self._start_python(info, env)
        return self._start_native(info, env)

    def stop(self, name: str, timeout: int = 5) -> bool:
        info = self.addons.get(name)
        if info and info.is_docker:
            return self._stop_docker(info)
        return self._stop_native(name, timeout)

    def restart(self, name: str) -> bool:
        self.stop(name)
        time.sleep(0.5)
        return self.start(name)

    def is_running(self, name: str) -> bool:
        info = self.addons.get(name)
        if not info:
            return False
        if info.is_docker:
            return self._is_docker_running(info)
        return self._is_native_running(name)

    def stop_all(self):
        for name in list(self.addons.keys()):
            self.stop(name)

    def shutdown(self):
        self._running = False
        self.stop_all()

    # ── Python addon (script via sys.executable) ───────────

    def _start_python(self, info: AddonInfo, env: dict = None) -> bool:
        with self._lock:
            if self._is_native_running(info.name):
                logger.info(f"Addon '{info.name}' already running")
                return True

            script = info.worker_script
            if not script or not script.exists():
                logger.error(f"Worker script not found: {script}")
                return False

            python = self._find_python()
            if not python:
                logger.error("No Python interpreter found for python addon")
                return False

            try:
                proc_env = os.environ.copy()
                proc_env["TTS_DATA_ROOT"] = str(self.base_dir)
                if env:
                    proc_env.update(env)

                proc = subprocess.Popen(
                    [python, str(script)],
                    cwd=str(info.addon_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=proc_env,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                self._processes[info.name] = proc
                logger.info(f"Python addon '{info.display}' started (PID: {proc.pid}, script: {script})")

                t_out = threading.Thread(
                    target=self._pipe_reader,
                    args=(proc.stdout, f"[{info.display}]"),
                    daemon=True,
                )
                t_err = threading.Thread(
                    target=self._pipe_reader,
                    args=(proc.stderr, f"[{info.display} ERR]"),
                    daemon=True,
                )
                t_out.start()
                t_err.start()
                self._log_threads.extend([t_out, t_err])
                return True
            except Exception as e:
                logger.error(f"Failed to start python addon '{info.name}': {e}")
                return False

    def _find_python(self) -> Optional[str]:
        if not getattr(sys, "frozen", False):
            return sys.executable
        # Prefer Python 3.14 (has all packages)
        local = os.environ.get("LOCALAPPDATA", "")
        for path in [
            Path(local) / "Python" / "pythoncore-3.14-64" / "python.exe",
            Path(local) / "Programs" / "Python" / "Python314" / "python.exe",
        ]:
            if path.exists():
                return str(path)
        python = shutil.which("python.exe") or shutil.which("python")
        if python and "WindowsApps" not in python:
            return python
        for p in [
            r"C:\Python314\python.exe",
            r"C:\Python313\python.exe",
            r"C:\Python312\python.exe",
            r"%LOCALAPPDATA%\Programs\Python\Python314\python.exe",
            r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe",
            r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe",
        ]:
            expanded = os.path.expandvars(p)
            if os.path.isfile(expanded):
                return expanded
        return None

    # ── Native (exe / subprocess) ──────────────────────────

    def _start_native(self, info: AddonInfo, env: dict = None) -> bool:
        with self._lock:
            if self._is_native_running(info.name):
                logger.info(f"Addon '{info.name}' already running")
                return True

            exe = str(info.exe_path)
            if not os.path.isfile(exe):
                logger.error(f"Addon exe not found: {exe}")
                return False

            try:
                proc_env = os.environ.copy()
                if env:
                    proc_env.update(env)

                proc = subprocess.Popen(
                    [exe],
                    cwd=str(info.work_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=proc_env,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                self._processes[info.name] = proc
                logger.info(f"Native addon '{info.display}' started (PID: {proc.pid}, exe: {exe})")

                t_out = threading.Thread(
                    target=self._pipe_reader,
                    args=(proc.stdout, f"[{info.display}]"),
                    daemon=True,
                )
                t_err = threading.Thread(
                    target=self._pipe_reader,
                    args=(proc.stderr, f"[{info.display} ERR]"),
                    daemon=True,
                )
                t_out.start()
                t_err.start()
                self._log_threads.extend([t_out, t_err])
                return True
            except Exception as e:
                logger.error(f"Failed to start native addon '{info.name}': {e}")
                return False

    def _stop_native(self, name: str, timeout: int = 5) -> bool:
        with self._lock:
            proc = self._processes.get(name)
            if not proc or proc.poll() is not None:
                self._processes.pop(name, None)
                return True

            info = self.addons.get(name)
            display = info.display if info else name
            try:
                logger.info(f"Stopping addon '{display}' (PID: {proc.pid})...")
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Force killing addon '{display}'...")
                    proc.kill()
                    proc.wait()
                self._processes.pop(name, None)
                return True
            except Exception as e:
                logger.error(f"Error stopping addon '{name}': {e}")
                return False

    def _is_native_running(self, name: str) -> bool:
        proc = self._processes.get(name)
        return proc is not None and proc.poll() is None

    # ── Docker ─────────────────────────────────────────────

    def _start_docker(self, info: AddonInfo) -> bool:
        compose = info.compose_path
        if not compose.exists():
            logger.error(f"Docker compose file not found: {compose}")
            return False

        logger.info(f"Starting Docker addon '{info.display}' (compose: {compose})...")
        try:
            cmd = ["docker", "compose", "-f", str(compose), "up", "-d"]
            if info.service_name:
                cmd.append(info.service_name)

            result = subprocess.run(
                cmd,
                cwd=str(compose.parent),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info(f"Docker addon '{info.display}' started")
                return True
            else:
                logger.error(f"Docker start failed for '{info.display}': {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"Docker start timed out for '{info.display}'")
            return False
        except FileNotFoundError:
            logger.error("Docker not found. Install Docker Desktop.")
            return False
        except Exception as e:
            logger.error(f"Failed to start Docker addon '{info.name}': {e}")
            return False

    def _stop_docker(self, info: AddonInfo) -> bool:
        compose = info.compose_path
        if not compose.exists():
            logger.warning(f"Docker compose file not found: {compose}")
            return False

        logger.info(f"Stopping Docker addon '{info.display}'...")
        try:
            cmd = ["docker", "compose", "-f", str(compose), "down"]
            result = subprocess.run(
                cmd,
                cwd=str(compose.parent),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info(f"Docker addon '{info.display}' stopped")
                return True
            else:
                logger.warning(f"Docker stop warning for '{info.display}': {result.stderr}")
                return True
        except Exception as e:
            logger.error(f"Failed to stop Docker addon '{info.name}': {e}")
            return False

    def _is_docker_running(self, info: AddonInfo) -> bool:
        try:
            name_filter = info.service_name or info.name
            cmd = ["docker", "ps", "--filter", f"name={name_filter}", "--format", "{{.Names}}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                running = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                return len(running) > 0
            return False
        except Exception:
            return False

    # ── Helpers ────────────────────────────────────────────

    def _pipe_reader(self, pipe, prefix):
        try:
            for line in iter(pipe.readline, b""):
                if not self._running:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"{prefix} {text}")
        except ValueError:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass


_registry: Optional[AddonRegistry] = None
_registry_lock = threading.Lock()


def get_registry(base_dir: Optional[Path] = None) -> AddonRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                if base_dir is None:
                    if getattr(sys, "frozen", False):
                        base_dir = Path(sys.executable).parent.resolve()
                    else:
                        base_dir = Path(__file__).parent.parent.resolve()
                _registry = AddonRegistry(base_dir)
                _registry.discover()
    return _registry


def discover_addons(base_dir: Optional[Path] = None) -> dict[str, AddonInfo]:
    return get_registry(base_dir).addons
