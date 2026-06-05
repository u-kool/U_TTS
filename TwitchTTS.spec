# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('G:\\1\\backup_333\\core\\templates', 'core/templates'),
         ('G:\\1\\backup_333\\icons\\icon.ico', 'icons')]
binaries = []
hiddenimports = ['edge_tts', 'edge_tts.communicate', 'edge_tts.voices', 'edge_tts.constants', 'edge_tts.exceptions', 'edge_tts.typing', 'pyttsx3', 'pyttsx3.drivers', 'pyttsx3.drivers.sapi5', 'pythoncom', 'pywintypes', 'flask', 'werkzeug', 'requests', 'requests.packages', 'requests.packages.urllib3', 'urllib3', 'urllib3.packages', 'urllib3.packages.six', 'certifi', 'charset_normalizer', 'idna', 'aiohttp', 'aiohttp.web', 'websockets', 'asyncio', 'jinja2', 'markupsafe', 'itsdangerous', 'click', 'multidict', 'yarl', 'frozenlist', 'aiosignal', 'attr', 'blinker', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'sounddevice', 'soundfile', 'numpy', 'regex', 'core.piper_engine', 'core.piper_preprocessor', 'eng_to_ipa', 'num2words', 'docopt', 'comtypes']
tmp_ret = collect_all('sounddevice')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('soundfile')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('edge_tts')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('aiohttp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('flask')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('werkzeug')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('jinja2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('markupsafe')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('requests')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('urllib3')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('charset_normalizer')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('idna')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('comtypes')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['G:\\1\\backup_333\\tray_app.py'],
    pathex=['G:\\1\\backup_333\\myenv\\Lib\\site-packages'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='U_TTS',
    icon='G:\\1\\backup_333\\icons\\icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
