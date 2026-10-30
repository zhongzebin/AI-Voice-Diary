"""AI语音日记 启动入口: 启动本地服务 → 打开浏览器 → 系统托盘常驻"""
import os
import sys
import socket
import logging
import threading
import webbrowser
import urllib.request
from logging.handlers import RotatingFileHandler

from backend import config as cfg
from backend import platform_utils as plat

PORTS = [18765, 18766, 18767, 18768, 18769]
HOST = "127.0.0.1"


def setup_logging() -> None:
    # 无控制台打包时 stdout/stderr 为 None, uvicorn 写日志会崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    handler = RotatingFileHandler(cfg.get_config_dir() / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def existing_instance(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=1) as r:
            return b"ai-voice-diary" in r.read()
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def make_icon(size=64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 调整各元素尺寸以适应不同 size
    s = size / 64
    d.ellipse((2*s, 2*s, 62*s, 62*s), fill=(79, 110, 247, 255))
    d.rounded_rectangle((24*s, 12*s, 40*s, 38*s), radius=8*s, fill="white")
    d.arc((17*s, 22*s, 47*s, 46*s), 0, 180, fill="white", width=int(4*s))
    d.line((32*s, 46*s, 32*s, 53*s), fill="white", width=int(4*s))
    d.line((24*s, 53*s, 40*s, 53*s), fill="white", width=int(4*s))
    return img


def main() -> None:
    setup_logging()
    log = logging.getLogger("launcher")

    for port in PORTS:
        if existing_instance(port):
            webbrowser.open(f"http://{HOST}:{port}/")
            return
    port = next((p for p in PORTS if port_free(p)), None)
    if port is None:
        log.error("端口 %s 均被占用", PORTS)
        return
    url = f"http://{HOST}:{port}/"

    import uvicorn
    from backend.server import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(), host=HOST, port=port, log_config=None, access_log=False))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    log.info("服务启动: %s", url)

    # 等服务就绪再开浏览器
    for _ in range(100):
        if server.started:
            break
        t.join(0.1)
    webbrowser.open(url)

    try:
        import pystray

        def open_folder(icon, item):
            p = cfg.get_data_path()
            if p and os.path.isdir(p):
                try:
                    plat.open_folder(p)
                except Exception as e:
                    log.error("打开文件夹失败: %s", e)

        def quit_app(icon, item):
            server.should_exit = True
            icon.stop()

        icon = pystray.Icon("ai_voice_diary", make_icon(), "AI语音日记", menu=pystray.Menu(
            pystray.MenuItem("打开日记", lambda i, it: webbrowser.open(url), default=True),
            pystray.MenuItem("打开数据文件夹", open_folder),
            pystray.MenuItem("退出", quit_app),
        ))
        icon.run()
    except Exception as e:  # 托盘不可用时退化为前台常驻
        log.warning("托盘不可用(%s), 前台运行, Ctrl+C 退出", e)
        try:
            t.join()
        except KeyboardInterrupt:
            server.should_exit = True
    t.join(5)


if __name__ == "__main__":
    main()
