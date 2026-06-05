#!/usr/bin/env python3
import os
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

def _find_python():
    # Приоритет: явный путь к venv
    venv_python = ROOT / "myenv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    # Если скрипт уже запущен из venv
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        return sys.executable
    # Старые методы поиска
    local = os.environ.get("LOCALAPPDATA", "")
    roots = [
        Path(local) / "Python" / "pythoncore-3.14-64",
        Path(local) / "Programs" / "Python" / "Python314",
    ]
    for r in roots:
        p = r / "python.exe"
        if p.exists():
            return str(p)
    for p in (shutil.which("python"), sys.executable):
        if p and "WindowsApps" not in p:
            return p
    for base in (Path(local) / "Programs" / "Python", Path(local) / "Python"):
        if base.exists():
            candidates = sorted(base.rglob("python.exe"), reverse=True)
            if candidates:
                return str(candidates[0])
    return sys.executable

python = _find_python()
print(f"Using Python: {python}")

# Добавляем путь к site-packages venv
venv_site_packages = ROOT / "myenv" / "Lib" / "site-packages"
extra_args = []
if venv_site_packages.exists():
    extra_args.append(f"--paths={venv_site_packages}")
    print(f"Added site-packages path: {venv_site_packages}")
else:
    print("Warning: venv site-packages not found")

args = [
    str(python),
    "-m", "PyInstaller",
    "--onefile",
    "--name=U_TTS",
    "--clean",
    "--noupx",
    f"--icon={ROOT / 'icons' / 'icon.ico'}",
    f"--add-data={ROOT / 'core' / 'templates'};core/templates",
    f"--add-data={ROOT / 'icons' / 'icon.ico'};icons",
    # Основные скрытые импорты
    "--hidden-import=edge_tts",
    "--hidden-import=edge_tts.communicate",
    "--hidden-import=edge_tts.voices",
    "--hidden-import=edge_tts.constants",
    "--hidden-import=edge_tts.exceptions",
    "--hidden-import=edge_tts.typing",
    "--hidden-import=pyttsx3",
    "--hidden-import=pyttsx3.drivers",
    "--hidden-import=pyttsx3.drivers.sapi5",
    "--hidden-import=pythoncom",
    "--hidden-import=pywintypes",
    "--hidden-import=flask",
    "--hidden-import=werkzeug",
    "--hidden-import=requests",
    "--hidden-import=requests.packages",
    "--hidden-import=requests.packages.urllib3",
    "--hidden-import=urllib3",
    "--hidden-import=urllib3.packages",
    "--hidden-import=urllib3.packages.six",
    "--hidden-import=certifi",
    "--hidden-import=charset_normalizer",
    "--hidden-import=idna",
    "--hidden-import=aiohttp",
    "--hidden-import=aiohttp.web",
    "--hidden-import=websockets",
    "--hidden-import=asyncio",
    "--hidden-import=jinja2",
    "--hidden-import=markupsafe",
    "--hidden-import=itsdangerous",
    "--hidden-import=click",
    "--hidden-import=multidict",
    "--hidden-import=yarl",
    "--hidden-import=frozenlist",
    "--hidden-import=aiosignal",
    "--hidden-import=attr",
    "--hidden-import=blinker",
    "--hidden-import=pystray",
    "--hidden-import=PIL",
    "--hidden-import=PIL.Image",
    "--hidden-import=PIL.ImageDraw",
    "--hidden-import=sounddevice",
    "--hidden-import=soundfile",
    "--hidden-import=numpy",
    "--hidden-import=regex",
    "--hidden-import=core.piper_engine",
    "--hidden-import=core.piper_preprocessor",
    "--hidden-import=eng_to_ipa",
    "--hidden-import=num2words",
    "--hidden-import=docopt",
    "--hidden-import=comtypes",
    # Опционально silero-stress (закомментировано)
    # "--hidden-import=silero_stress",
    # "--collect-all=torch",
    # Сборка целых пакетов
    "--collect-all=sounddevice",
    "--collect-all=soundfile",
    "--collect-all=edge_tts",
    "--collect-all=aiohttp",
    "--collect-all=flask",
    "--collect-all=werkzeug",
    "--collect-all=jinja2",
    "--collect-all=markupsafe",
    "--collect-all=requests",
    "--collect-all=urllib3",
    "--collect-all=certifi",
    "--collect-all=charset_normalizer",
    "--collect-all=idna",
    "--collect-all=comtypes",
] + extra_args + [str(ROOT / "tray_app.py")]

print("Running PyInstaller with:")
print(" ".join(str(a) for a in args))
print()

result = subprocess.run(args)
sys.exit(result.returncode)