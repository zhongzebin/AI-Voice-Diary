# -*- mode: python ; coding: utf-8 -*-
"""
跨平台打包配置 - 同一个 spec 文件, Windows 上产出 .exe, macOS 上产出 .app

打包命令:
  python -m PyInstaller build.spec --noconfirm

产物:
  Windows: dist/AI语音日记/AI语音日记.exe
  macOS:   dist/AI语音日记.app
"""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))  # 确保能导入 run.py

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
APP_NAME = "AI语音日记"

# 生成图标
from run import make_icon
ICON_DIR = ROOT / "build" / "icons"
ICON_DIR.mkdir(parents=True, exist_ok=True)

if IS_WIN:
    ICON_PATH = ICON_DIR / "app.ico"
    make_icon(256).save(ICON_PATH, sizes=[(16,16), (32,32), (48,48), (64,64), (256,256)])
else:  # macOS
    ICON_PATH = ICON_DIR / "app.icns"
    # iconutil 要求的标准尺寸和命名
    iconset = ICON_DIR / "app.iconset"
    iconset.mkdir(exist_ok=True)
    for size in [16, 32, 128, 256, 512]:
        make_icon(size).save(iconset / f"icon_{size}x{size}.png")
        make_icon(size*2).save(iconset / f"icon_{size}x{size}@2x.png")
    import subprocess
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_PATH)], check=True)

# 平台相关的隐藏导入
hidden = collect_submodules("uvicorn")
if IS_WIN:
    hidden += ["pystray._win32", "tkinter", "tkinter.filedialog"]
elif IS_MAC:
    hidden += ["pystray._darwin"]
else:
    hidden += ["pystray._xorg"]

a = Analysis(
    ["run.py"],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "static"), "static")],
    hiddenimports=hidden,
    excludes=["matplotlib", "pandas", "scipy", "torch", "tensorflow", "PyQt5", "PyQt6",
              "PySide2", "PySide6", "IPython", "jupyter", "pytest", "playwright", "cv2"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=str(ICON_PATH),
    upx=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    name=APP_NAME,
    upx=False,
)

if IS_MAC:
    # macOS: 打包成 .app bundle
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ICON_PATH),
        bundle_identifier="com.ai-voice-diary.app",
        version="1.0.0",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "LSMinimumSystemVersion": "10.15",
            "LSUIElement": True,  # 隐藏 Dock 图标,只显示托盘图标
            "NSMicrophoneUsageDescription": "需要麦克风权限来录制语音日记",
            "NSDocumentsFolderUsageDescription": "存储日记文件到您的文档文件夹",
        },
    )
