#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tray application for Twitch TTS Server + Audio Player
Запускает сервер и аудиоплеер автоматически.
Движки TTS запускаются вручную через меню трея.
"""

import sys
import os
import signal
import atexit
import subprocess
import threading
import time
import webbrowser
import ctypes
import shutil
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("Установите зависимости: pip install pystray pillow")
    sys.exit(1)

FROZEN = getattr(sys, 'frozen', False)

if FROZEN:
    BASE_DIR = Path(sys.executable).parent.resolve()  # dist/
    SOURCE_DIR = BASE_DIR.parent.resolve()             # корень проекта (с исходниками)
else:
    BASE_DIR = Path(__file__).parent
    SOURCE_DIR = BASE_DIR

# В frozen-mode сервер и плеер запущены in-process, но скрипты воркеров берём из SOURCE_DIR
SERVER_SCRIPT = SOURCE_DIR / "server.py"
PLAYER_SCRIPT = SOURCE_DIR / "audio_player" / "player.py"
COSYVOICE_WORKER_SCRIPT = SOURCE_DIR / "workers" / "cosyvoice" / "cosyvoice_worker.py"
QWEN3_WORKER_SCRIPT = SOURCE_DIR / "workers" / "qwen3_tts" / "qwen3_worker.py"
XTTS_WORKER_SCRIPT = SOURCE_DIR / "workers" / "xtts" / "xtts_worker.py"
WEB_URL = "http://127.0.0.1:5000"

server_process = None
player_process = None
cosyvoice_process = None
qwen3_process = None
xtts_process = None
running = True

_log_threads = []


@atexit.register
def _atexit_cleanup():
    stop_processes()


def find_worker_python(worker_dir_name: str, venv_name: str = None):
    if venv_name is None:
        venv_name = f"venv_{worker_dir_name}"
    # В frozen-mode venv ищем в SOURCE_DIR, иначе в BASE_DIR
    _venv_base = SOURCE_DIR if FROZEN else BASE_DIR
    local_venv = _venv_base / "workers" / worker_dir_name / venv_name / "Scripts" / "python.exe"
    if local_venv.exists():
        return str(local_venv)
    root_venv = _venv_base / venv_name / "Scripts" / "python.exe"
    if root_venv.exists():
        return str(root_venv)
    if FROZEN:
        system_python = shutil.which("python.exe") or shutil.which("python")
        if system_python:
            print(f"WARNING: venv for {worker_dir_name} not found, using system python: {system_python}")
            return system_python
        print(f"WARNING: Cannot find python interpreter for {worker_dir_name} worker. Install its venv first.")
        return None
    return sys.executable


def read_output(process, prefix, color_code=None):
    for line in iter(process.stdout.readline, b''):
        if not running:
            break
        line = line.decode('utf-8', errors='replace').rstrip()
        if color_code:
            print(f"\033[{color_code}m{prefix} {line}\033[0m")
        else:
            print(f"{prefix} {line}")
    process.stdout.close()


def read_error(process, prefix, color_code=None):
    for line in iter(process.stderr.readline, b''):
        if not running:
            break
        line = line.decode('utf-8', errors='replace').rstrip()
        if color_code:
            print(f"\033[{color_code}m{prefix} {line}\033[0m")
        else:
            print(f"{prefix} {line}")
    process.stderr.close()


def _start_process(script_path, cwd, label, color, venv_name=None, env=None):
    proc = None
    try:
        worker_dir = script_path.parent.name
        python = find_worker_python(worker_dir, venv_name) if venv_name else (sys.executable if not FROZEN else None)
        if python is None:
            print(f"WARNING: No python interpreter for {label}. Skipping.")
            return proc
        if FROZEN and python == sys.executable:
            print(f"WARNING: Refusing to spawn frozen exe as {label}. Skipping.")
            return proc
        proc_env = os.environ.copy()
        proc_env["TTS_DATA_ROOT"] = str(BASE_DIR)
        if env:
            proc_env.update(env)
        proc = subprocess.Popen(
            [python, str(script_path)],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proc_env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        print(f"{label} started (PID: {proc.pid}, Python: {python})")
        t_out = threading.Thread(target=read_output, args=(proc, f"[{label}]", color), daemon=True)
        t_err = threading.Thread(target=read_error, args=(proc, f"[{label} ERR]", "31"), daemon=True)
        t_out.start()
        t_err.start()
        _log_threads.extend([t_out, t_err])
    except Exception as e:
        print(f"Failed to start {label}: {e}")
    return proc


def _docker_compose(args, label):
    import re
    compose_file = BASE_DIR / "docker-compose.yml"
    if not compose_file.exists():
        return False
    for attempt in range(2):
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file)] + args,
                cwd=str(BASE_DIR),
                capture_output=True, text=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                out = result.stdout.strip()
                if out:
                    print(f"{label}: {out}")
                return True
            err = result.stderr.strip()
            # Если контейнер с таким именем уже существует — удаляем и пробуем снова
            match = re.search(r'container name "/([^"]+)"', err)
            if match:
                old_name = match.group(1)
                print(f"{label}: removing stale container '{old_name}'...")
                subprocess.run(
                    ["docker", "rm", "-f", old_name],
                    capture_output=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                continue
            print(f"{label}: {err}")
            return False
        except Exception as e:
            print(f"{label} Docker error: {e}")
            return False
    return False


def start_server():
    global server_process
    if FROZEN:
        print("Server runs in-process (frozen mode)")
        return
    if server_process and server_process.poll() is None:
        print("Server already running")
        return
    server_process = _start_process(SERVER_SCRIPT, SERVER_SCRIPT.parent, "Server", "36")


def start_player():
    global player_process
    if FROZEN:
        print("Audio player runs in-process (frozen mode)")
        return
    if player_process and player_process.poll() is None:
        print("Player already running")
        return
    player_process = _start_process(PLAYER_SCRIPT, PLAYER_SCRIPT.parent, "Player", "32")


def start_cosyvoice():
    global cosyvoice_process
    if cosyvoice_process and cosyvoice_process.poll() is None:
        print("CosyVoice already running")
        return
    cosyvoice_process = _start_process(COSYVOICE_WORKER_SCRIPT, BASE_DIR, "CosyVoice", "35", venv_name="venv_cosyvoice")


def start_qwen3():
    global qwen3_process
    if qwen3_process and qwen3_process.poll() is None:
        print("Qwen3 already running")
        return
    if _docker_compose(["up", "-d", "qwen3-worker"], "Qwen3"):
        return
    qwen3_process = _start_process(QWEN3_WORKER_SCRIPT, BASE_DIR, "Qwen3", "33", venv_name="venv_qwen3")


def start_xtts():
    global xtts_process
    if xtts_process and xtts_process.poll() is None:
        print("XTTS already running")
        return
    if _docker_compose(["up", "-d", "xtts-worker"], "XTTS"):
        return
    xtts_process = _start_process(XTTS_WORKER_SCRIPT, BASE_DIR, "XTTS", "34", venv_name="venv_xtts")


def stop_cosyvoice():
    global cosyvoice_process
    cosyvoice_process = stop_process(cosyvoice_process, "CosyVoice Worker")


def stop_qwen3():
    global qwen3_process
    if _docker_compose(["down", "qwen3-worker"], "Qwen3"):
        qwen3_process = None
        return
    qwen3_process = stop_process(qwen3_process, "Qwen3 Worker")


def stop_xtts():
    global xtts_process
    if _docker_compose(["down", "xtts-worker"], "XTTS"):
        xtts_process = None
        return
    xtts_process = stop_process(xtts_process, "XTTS Worker")


def stop_all_engines():
    stop_cosyvoice()
    stop_qwen3()
    stop_xtts()


def stop_processes():
    global server_process, player_process, cosyvoice_process, qwen3_process, xtts_process, running
    running = False
    stop_all_engines()
    stop_process(server_process, "Server")
    stop_process(player_process, "Audio Player")
    print("All processes stopped.")


# ========== ИКОНКА ТРЕЯ ==========
def on_left_click(icon, item):
    webbrowser.open(WEB_URL)


def on_quit(icon, item):
    icon.stop()
    stop_processes()
    os._exit(0)


def create_menu():
    items = [
        pystray.MenuItem("Open Web UI", on_left_click, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start CosyVoice", start_cosyvoice),
        pystray.MenuItem("Stop CosyVoice", stop_cosyvoice),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start Qwen3", start_qwen3),
        pystray.MenuItem("Stop Qwen3", stop_qwen3),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start XTTS", start_xtts),
        pystray.MenuItem("Stop XTTS", stop_xtts),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop All Engines", stop_all_engines),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_quit),
    ]
    return pystray.Menu(*items)


def create_image():
    ico_path = Path(sys._MEIPASS) / "icons" / "icon.ico" if FROZEN else BASE_DIR / "icons" / "icon.ico"
    if ico_path.exists():
        return Image.open(ico_path)
    width = 64
    height = 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, width-4, height-4), fill='#9146FF')
    draw.text((16, 16), "TTS", fill='white')
    return image


def setup_tray():
    icon = pystray.Icon("TwitchTTS", create_image(), "Twitch TTS", create_menu())
    icon.run()


# ========== ТОЧКА ВХОДА ==========
def signal_handler(sig, frame):
    print(f"\nReceived signal {sig}, shutting down...")
    stop_processes()
    os._exit(0)


def ensure_single_instance():
    """Named mutex to prevent duplicate instances. On conflict, the new instance exits."""
    if sys.platform != 'win32':
        return True
    try:
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\TwitchTTS")
        if ctypes.windll.kernel32.GetLastError() == 183:
            print("TwitchTTS is already running. Exiting duplicate instance.")
            return False
        return True
    except Exception as e:
        print(f"Single-instance check skipped (non-critical): {e}")
        return True


def main():
    if not ensure_single_instance():
        sys.exit(0)
    if sys.platform == 'win32':
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    else:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    print("Starting Twitch TTS Tray Application...")
    print("Server and Audio Player will start automatically.")
    print("Engines (CosyVoice, Qwen3, XTTS) can be started from the tray menu.")
    print("Piper TTS is built into the server and starts automatically.")

    # In frozen mode, run server and player in-process
    if FROZEN:
        import server
        import audio_player.player as player_mod
        threading.Thread(target=server.run_server, daemon=True).start()
        time.sleep(2)
        threading.Thread(target=player_mod.run_player, daemon=True).start()
    else:
        start_server()
        start_player()
    time.sleep(1)

    try:
        setup_tray()
    except KeyboardInterrupt:
        signal_handler(None, None)


def cleanup_orphaned_processes():
    """Kill any orphaned worker processes from previous runs."""
    import subprocess as _sp
    print("Cleaning up orphaned Twitch TTS processes...")
    try:
        result = _sp.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        killed = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and "LISTENING" in parts[3]:
                for port_str in ("5000", "5003", "5004", "5005", "5006", "5007", "3000"):
                    if f":{port_str}" in parts[1]:
                        pid = parts[4]
                        if pid not in killed:
                            print(f"Killing orphaned PID {pid} (port {port_str})")
                            _sp.run(["taskkill", "/F", "/PID", pid],
                                    capture_output=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                            killed.add(pid)
    except Exception as e:
        print(f"Cleanup error: {e}")
    print("Cleanup done.")


if __name__ == "__main__":
    if "--cleanup" in sys.argv:
        cleanup_orphaned_processes()
        sys.exit(0)
    main()