#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live Clip Studio · 桌面 App 入口

双击后：
  1. 后台线程起本地只读服务（127.0.0.1，端口被占用则顺延）
  2. 用 WebView2 开一个原生窗口承载页面 —— 无地址栏、无标签页、独立任务栏图标
  3. 关掉窗口，服务和进程一起退出

依赖：pywebview + WebView2 Runtime（Win10/11 自带）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import server as S  # noqa: E402

BASE, RES = S.BASE, S.RES          # 打包后：BASE=exe 所在目录，RES=解包临时目录
APP_NAME = S.CFG.get("app_name") or "Live Clip Studio"


def _setup_log():
    """无控制台运行时把 stdout/stderr 和未捕获异常落到 cache/app.log。"""
    if sys.stdout is not None:
        return
    try:
        S.CACHE.mkdir(parents=True, exist_ok=True)
        f = open(S.CACHE / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        import traceback

        def hook(t, v, tb):
            traceback.print_exception(t, v, tb, file=f)
            f.flush()

        sys.excepthook = hook
        import threading

        def thook(a):
            traceback.print_exception(a.exc_type, a.exc_value, a.exc_traceback, file=f)
            f.flush()

        threading.excepthook = thook
    except Exception:
        pass


_setup_log()


def existing_instance() -> int | None:
    """已有实例在跑就复用它的服务，避免开出第二个端口。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{S.PORT}/api/state", timeout=1.2) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("app") == APP_NAME:
            return S.PORT
    except Exception:
        pass
    return None


def load_geo() -> dict:
    p = S.CACHE / "window.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        w = max(1120, int(d.get("width", 1460)))
        h = max(680, int(d.get("height", 930)))
        x, y = d.get("x"), d.get("y")
        # 换过显示器或分辨率，旧坐标可能落在屏幕外，直接放弃
        if not isinstance(x, int) or not (0 <= x <= 4000):
            x = None
        if not isinstance(y, int) or not (0 <= y <= 3000):
            y = None
        return {"width": w, "height": h, "x": x, "y": y}
    except Exception:
        return {"width": 1460, "height": 930, "x": None, "y": None}


def _own_hwnd() -> int | None:
    """按标题 + 进程号找到本进程自己的窗口句柄。"""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    me = ctypes.windll.kernel32.GetCurrentProcessId()
    hit: list[int] = []
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if not n:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        if b.value == APP_NAME:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == me:
                hit.append(h)
        return True

    u.EnumWindows(PROC(cb), 0)
    return hit[0] if hit else None


def save_geo(win) -> None:
    d = None
    try:
        import ctypes
        from ctypes import wintypes

        h = _own_hwnd()
        if h:
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(h, ctypes.byref(r))
            d = {"width": r.right - r.left, "height": r.bottom - r.top,
                 "x": r.left, "y": r.top}
    except Exception:
        d = None
    if not d:                                  # 拿不到真实矩形就退回接口值
        try:
            d = {"width": win.width, "height": win.height, "x": win.x, "y": win.y}
        except Exception:
            return
    try:
        (S.CACHE / "window.json").write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    port = existing_instance()
    httpd = None
    if port is None:
        httpd, port = S.serve()
    url = f"http://127.0.0.1:{port}/"

    import webview

    icon = RES / "icon.ico"
    geo = load_geo()
    win = webview.create_window(
        APP_NAME,
        url,
        width=geo["width"],
        height=geo["height"],
        x=geo["x"],
        y=geo["y"],
        min_size=(1120, 680),
        background_color="#F4F1EA",
        text_select=True,
        confirm_close=False,
        zoomable=False,
    )
    win.events.closing += lambda: save_geo(win)

    try:
        webview.start(
            gui="edgechromium",
            debug="--debug" in sys.argv,
            icon=str(icon) if icon.is_file() else None,
            private_mode=False,
            storage_path=str(S.CACHE / "webview"),
        )
    finally:
        if httpd is not None:
            httpd.shutdown()


if __name__ == "__main__":
    main()
