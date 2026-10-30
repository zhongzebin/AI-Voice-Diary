"""平台差异集中在此模块: 其余代码只调用这里的函数, Windows/macOS 共用一套业务代码"""
import base64
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
APP_NAME = "AI语音日记"
KEYRING_SERVICE = "ai-voice-diary"


def config_dir() -> Path:
    if IS_WIN:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME


def resource_dir() -> Path:
    """static 等只读资源所在目录(源码运行 / PyInstaller 打包两种情况)"""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    if IS_MAC:
        # .app 内资源在 Contents/Resources; Frameworks 下只是符号链接, StaticFiles 会拒绝越界的链接
        res = Path(sys.executable).resolve().parent.parent / "Resources"
        if (res / "static").is_dir():
            return res
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


def open_folder(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if IS_WIN:
        os.startfile(p)  # noqa
    else:
        subprocess.Popen(["open" if IS_MAC else "xdg-open", str(p)])


def pick_folder(title: str) -> Optional[str]:
    """弹出系统文件夹选择框; 用户取消返回 None"""
    if IS_MAC:
        # tkinter 在 macOS 只能跑在主线程, 改用系统自带的 AppleScript 对话框
        prompt = title.replace("\\", "\\\\").replace('"', '\\"')
        # 分两步执行: 先 activate 确保对话框在前, 再弹出选择框
        r = subprocess.run([
            "osascript",
            "-e", "activate",
            "-e", f'POSIX path of (choose folder with prompt "{prompt}")'
        ], capture_output=True, text=True)
        if r.returncode != 0:
            if "-128" in r.stderr:  # 用户点了取消
                return None
            raise RuntimeError(r.stderr.strip() or "osascript 失败")
        return r.stdout.strip().rstrip("/") or None

    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title=title, mustexist=False)
        return str(Path(path)) if path else None
    finally:
        root.destroy()


# ---------------- 密钥存储 ----------------
# Windows: DPAPI 加密后存 config.json;  macOS/Linux: 存系统钥匙串, config.json 只留引用

def store_secret(name: str, secret: str) -> str:
    """保存密钥, 返回写入 config.json 的字符串"""
    if not secret:
        return ""
    if IS_WIN:
        enc = _dpapi(secret.encode("utf-8"), protect=True)
        if enc is not None:
            return "dpapi:" + enc.hex()
    else:
        try:
            import keyring
            keyring.set_password(KEYRING_SERVICE, name, secret)
            return f"keyring:{name}"
        except Exception as e:
            log.warning("系统钥匙串不可用, 密钥以弱混淆方式保存: %s", e)
    return "b64:" + base64.b64encode(secret.encode("utf-8")).decode("ascii")


def load_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        if stored.startswith("dpapi:"):
            if not IS_WIN:
                return ""
            dec = _dpapi(bytes.fromhex(stored[6:]), protect=False)
            return dec.decode("utf-8") if dec is not None else ""
        if stored.startswith("keyring:"):
            import keyring
            return keyring.get_password(KEYRING_SERVICE, stored[8:]) or ""
        if stored.startswith("b64:"):
            return base64.b64decode(stored[4:]).decode("utf-8")
    except Exception as e:
        log.warning("读取密钥失败: %s", e)
    return ""


def _dpapi(data: bytes, protect: bool) -> Optional[bytes]:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
