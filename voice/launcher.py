"""Desktop entry point: start the local server, wait for readiness, open browser."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import msvcrt
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
URL = "http://127.0.0.1:8765/"
AGENT_ROOT = ROOT.parent / "shengbo-ops-agent"
AGENT_URL = "http://127.0.0.1:8791/health"


def log(message):
    with (RUNTIME / "launcher.log").open("a", encoding="utf-8") as output:
        output.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def healthy():
    try:
        # A direct local connection must not use a system HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "api/library", timeout=2) as response:
            value = json.load(response)
        return isinstance(value.get("products"), list) and isinstance(value.get("scripts"), list) and value.get("service", {}).get("tts") == "本机普通话 / MiniMax 可选"
    except Exception:
        return False


def agent_healthy():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(AGENT_URL, timeout=3) as response:
            status = json.load(response)
        return status.get("service") == "shengbo-prelive-agent" and status.get("started") is True
    except Exception:
        return False


def ensure_agent():
    if agent_healthy():
        log("Reused existing prelive Agent")
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8791)) == 0:
            raise RuntimeError("播前助手端口8791已被占用，请先检查该服务。声播控制台可独立使用。")
    python = AGENT_ROOT / ".venv/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("找不到播前助手运行环境，请按Agent项目README恢复。")
    agent_runtime = AGENT_ROOT / ".runtime"
    agent_runtime.mkdir(exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with (agent_runtime / "service.log").open("ab") as output:
        proc = subprocess.Popen([str(python), "-X", "utf8", "-m", "shengbo_prelive"], cwd=AGENT_ROOT,
                                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                creationflags=flags, close_fds=True)
    (agent_runtime / "service.pid").write_text(str(proc.pid), encoding="ascii")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if agent_healthy():
            log("Prelive Agent ready")
            return
        if proc.poll() is not None:
            raise RuntimeError("播前助手启动失败，请查看Agent项目.runtime/service.log。")
        time.sleep(.3)
    raise RuntimeError("播前助手启动超过60秒，请检查Agent项目.runtime/service.log。")


@contextmanager
def startup_lock():
    with (RUNTIME / "launcher.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 65
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("另一个启动任务仍在进行，请稍后重试。")
                time.sleep(.2)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_server():
    if healthy():
        log("Reused existing server")
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            raise RuntimeError("本机端口8765被其他服务占用，声播未改动该服务。请检查后再启动。")
    python = ROOT / ".venv/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("找不到应用运行环境，请按项目README恢复虚拟环境。")
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with (RUNTIME / "server.desktop.log").open("ab") as output:
        proc = subprocess.Popen([str(python), "-m", "uvicorn", "app.server:app", "--host", "127.0.0.1", "--port", "8765", "--no-access-log"],
                                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output, creationflags=flags, close_fds=True)
    (RUNTIME / "server.pid").write_text(str(proc.pid), encoding="ascii")
    log(f"Started server pid={proc.pid}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if healthy():
            log("Server ready")
            return
        if proc.poll() is not None:
            raise RuntimeError("服务启动失败，请查看项目runtime/server.desktop.log。")
        time.sleep(.3)
    raise RuntimeError("服务启动超过60秒，请稍后重新点击快捷方式，或查看runtime/server.desktop.log。")


def main():
    RUNTIME.mkdir(exist_ok=True)
    try:
        with startup_lock():
            ensure_server()
            ensure_agent()
        if "--no-browser" not in sys.argv:
            if not webbrowser.open(URL, new=2):
                raise RuntimeError("服务已启动，但默认浏览器未能打开。请在浏览器输入：" + URL)
            log("Opened default browser")
        return 0
    except Exception as error:
        log(f"Startup failed: {type(error).__name__}")
        if "--no-browser" in sys.argv:
            print(str(error))
        else:
            import tkinter
            from tkinter import messagebox
            window = tkinter.Tk()
            window.withdraw()
            messagebox.showerror("声播 · 启动提示", str(error), parent=window)
            window.destroy()
        return 1


if __name__ == "__main__":
    sys.exit(main())
