#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显存监控：本地实时查看 NVIDIA GPU 显存占用。

启动后在默认浏览器打开 http://127.0.0.1:<port>/，关闭页面约 1 秒后自动退出。
只依赖 Python 标准库和 NVIDIA 驱动自带的 nvidia-smi。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "显存监控"
VERSION = "1.0.3"

# 源码运行时资源/日志都在项目目录；PyInstaller 时 web 在 _MEIPASS，
# 日志放在 exe 同目录，便于便携包保留日志。
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR
WEB_ROOT = RESOURCE_DIR / "web"
LOG_DIR = APP_DIR / "logs"

HISTORY_MAX = 240        # 内部最多保留 120 秒（0.5 秒一个点）
CHART_POINTS = 130       # 页面首次打开时回放的点数
IDLE_EXIT_SECONDS = 20   # 从未连上页面时，多久后自动退出
DISCONNECT_EXIT_SECONDS = 1.0  # 页面关闭后，留给刷新重连的宽限

log = logging.getLogger("vram")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def show_fatal(message: str) -> None:
    """pythonw 启动时没有控制台，用一个系统消息框把启动失败原因告诉用户。"""
    log.error("启动失败：%s", message)
    if getattr(sys, "stderr", None) is not None:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    except Exception:
        pass


def open_page(url: str) -> None:
    """打开浏览器；失败时至少把地址用消息框告诉用户。"""
    try:
        if webbrowser.open(url):
            return
    except Exception:
        log.exception("打开浏览器失败")
    log.warning("无法自动打开浏览器：%s", url)
    if getattr(sys, "stderr", None) is None:
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "无法自动打开浏览器。\n请手动访问：\n%s" % url,
                APP_NAME,
                0x40,
            )
        except Exception:
            pass


def read_system_style() -> tuple[str, bool]:
    """读取 Windows 强调色与深浅色设置；失败时用中性默认值。"""
    accent = "#2f6fed"
    dark = False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            dark = not bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            raw = int(winreg.QueryValueEx(key, "AccentColor")[0])
        # DWM 存的是 ABGR
        accent = "#%02x%02x%02x" % (raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF)
    except Exception:
        log.warning("读取系统主题失败，使用默认配色", exc_info=True)
    return accent, dark


def parse_number(text: str) -> float | None:
    text = text.strip()
    if not text or text.startswith("["):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---- Windows 进程显存（PDH）--------------------------------------
# nvidia-smi 在 WDDM 下拿不到进程显存，改用 VidMm 暴露给性能计数器的
# “GPU Process Memory\Local Usage”。它对应进程实际落在本地显存里的量；
# 进程合计通常小于顶部 nvidia-smi 总量，差额是驱动/系统保留。
_PDH_FMT_DOUBLE = 0x00000200
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_pdh = ctypes.WinDLL("pdh")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _PdhCounterValue(ctypes.Structure):
    _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]


class _PdhCounterItem(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("value", _PdhCounterValue)]


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_pdh.PdhOpenQueryW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_void_p),
]
_pdh.PdhOpenQueryW.restype = wintypes.LONG
_pdh.PdhAddEnglishCounterW.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_void_p),
]
_pdh.PdhAddEnglishCounterW.restype = wintypes.LONG
_pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
_pdh.PdhCollectQueryData.restype = wintypes.LONG
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_pdh.PdhGetFormattedCounterArrayW.restype = wintypes.LONG
_pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
_pdh.PdhCloseQuery.restype = wintypes.LONG

_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_names() -> dict[int, str]:
    """一次快照拿全部 PID 的可执行名，保护进程（dwm/csrss）也能拿到。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return {}
    names: dict[int, str] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pid = int(entry.th32ProcessID)
            name = str(entry.szExeFile).strip() or "PID %d" % pid
            names[pid] = name
            more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return names


def process_path(pid: int) -> str:
    """读取进程完整路径；保护进程可能拒绝访问，返回空串。"""
    if pid <= 0:
        return ""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not _kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(length)
        ):
            return ""
        return buffer.value
    finally:
        _kernel32.CloseHandle(handle)


PROCESS_DESCRIPTIONS = {
    "dwm.exe": "Windows 桌面窗口管理器（图形合成）",
    "explorer.exe": "Windows 资源管理器",
    "csrss.exe": "Windows 客户端/服务端运行时",
    "winlogon.exe": "Windows 登录",
    "services.exe": "Windows 服务控制管理器",
    "lsass.exe": "Windows 本地安全认证",
    "smss.exe": "Windows 会话管理器",
    "wininit.exe": "Windows 启动初始化",
    "svchost.exe": "Windows 服务宿主",
    "dllhost.exe": "Windows COM+ 代理",
    "rundll32.exe": "Windows 宿主进程",
    "taskhostw.exe": "Windows 任务宿主",
    "sihost.exe": "Windows Shell 基础设施",
    "ctfmon.exe": "Windows 文本输入",
    "runtimebroker.exe": "Windows 应用运行时",
    "searchhost.exe": "Windows 搜索",
    "searchapp.exe": "Windows 搜索",
    "searchindexer.exe": "Windows 搜索索引",
    "startmenuexperiencehost.exe": "Windows 开始菜单",
    "shellexperiencehost.exe": "Windows Shell 体验",
    "textinputhost.exe": "Windows 输入法/文本服务",
    "applicationframehost.exe": "UWP 应用宿主",
    "systemsettings.exe": "Windows 设置",
    "lockapp.exe": "Windows 锁屏",
    "shellhost.exe": "Windows Shell",
    "conhost.exe": "控制台宿主",
    "taskmgr.exe": "任务管理器",
    "phoneexperiencehost.exe": "手机连接",
    "crossdeviceresume.exe": "跨设备续传",
    "time.exe": "Windows 时钟",
    "microsoft.photos.exe": "Windows 照片",
    "msmpeng.exe": "Microsoft Defender",
    "securityhealthservice.exe": "Windows 安全中心",
    "securityhealthsystray.exe": "Windows 安全中心托盘",
    "spoolsv.exe": "Windows 打印后台",
    "audiodg.exe": "Windows 音频设备图",
    "werfault.exe": "Windows 错误报告",
    "wudfhost.exe": "Windows 用户模式驱动框架",
    "fontdrvhost.exe": "Windows 字体驱动宿主",
    "wmiprvse.exe": "Windows WMI 提供程序",
    "msedge.exe": "Microsoft Edge 浏览器",
    "msedgewebview2.exe": "Microsoft Edge WebView2（应用内嵌网页）",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Mozilla Firefox",
    "brave.exe": "Brave 浏览器",
    "opera.exe": "Opera 浏览器",
    "python.exe": "Python 脚本/模型进程",
    "pythonw.exe": "Python 脚本/模型进程",
    "node.exe": "Node.js 运行时",
    "code.exe": "Visual Studio Code",
    "devenv.exe": "Visual Studio",
    "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ IDEA",
    "qq.exe": "腾讯 QQ",
    "wechat.exe": "微信",
    "weixin.exe": "微信",
    "wechatappex.exe": "微信小程序",
    "wxwork.exe": "企业微信",
    "wetype.exe": "微信输入法",
    "wetype_update.exe": "微信输入法更新",
    "dingtalk.exe": "钉钉",
    "feishu.exe": "飞书",
    "telegram.exe": "Telegram",
    "discord.exe": "Discord",
    "spotify.exe": "Spotify",
    "steam.exe": "Steam",
    "steamwebhelper.exe": "Steam 内嵌浏览器",
    "epicgameslauncher.exe": "Epic Games 启动器",
    "nvidia overlay.exe": "NVIDIA 游戏内覆盖",
    "nvidia share.exe": "NVIDIA 分享",
    "nvcontainer.exe": "NVIDIA 容器服务",
    "nvsphelper64.exe": "NVIDIA 服务",
    "switchhosts.exe": "SwitchHosts",
    "o+connect.exe": "OPPO 互联",
    "windowsterminal.exe": "Windows Terminal",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "cmd.exe": "命令提示符",
    "winword.exe": "Microsoft Word",
    "excel.exe": "Microsoft Excel",
    "powerpnt.exe": "Microsoft PowerPoint",
    "outlook.exe": "Microsoft Outlook",
    "msaccess.exe": "Microsoft Access",
    "onenote.exe": "Microsoft OneNote",
    "illustrator.exe": "Adobe Illustrator",
    "photoshop.exe": "Adobe Photoshop",
    "premiere pro.exe": "Adobe Premiere Pro",
    "aftereffects.exe": "Adobe After Effects",
    "afterfx.exe": "Adobe After Effects",
    "acrobat.exe": "Adobe Acrobat",
    "indesign.exe": "Adobe InDesign",
    "lightroom.exe": "Adobe Lightroom",
    "mspaint.exe": "Windows 画图",
    "notepad.exe": "Windows 记事本",
    "calc.exe": "Windows 计算器",
    "snippingtool.exe": "Windows 截图工具",
    "magnify.exe": "Windows 放大镜",
    "osk.exe": "Windows 屏幕键盘",
    "ms-teams.exe": "Microsoft Teams",
    "teams.exe": "Microsoft Teams",
    "onedrive.exe": "OneDrive",
    "deepseek harness.exe": "DeepSeek Harness",
    "system": "Windows 内核",
    "registry": "Windows 注册表",
    "memory compression": "Windows 内存压缩",
    "secure system": "Windows 安全系统",
}


def describe_process(name: str) -> str:
    """把可执行名推断成用户能认出的程序/服务说明。"""
    key = name.strip().lower()
    if key in PROCESS_DESCRIPTIONS:
        return PROCESS_DESCRIPTIONS[key]
    if "nvidia" in key:
        return "NVIDIA 组件"
    if "tencent" in key or "qq" in key:
        return "腾讯组件"
    if "wechat" in key or "weixin" in key or "wetype" in key:
        return "微信组件"
    if "microsoft" in key or "windows" in key:
        return "Microsoft/Windows 组件"
    if "edge" in key:
        return "Microsoft Edge 组件"
    return "来源未识别"


class GpuSampler(threading.Thread):
    """常驻一个 nvidia-smi 进程，按间隔流式读取指标。"""

    FIELDS = (
        "index",
        "name",
        "memory.total",
        "memory.used",
        "memory.reserved",
        "utilization.gpu",
        "temperature.gpu",
        "power.draw",
        "clocks.current.graphics",
    )

    def __init__(self, interval_ms: int = 500, executable: str = "nvidia-smi") -> None:
        super().__init__(name="gpu-sampler", daemon=True)
        self.interval_ms = max(200, int(interval_ms))
        self.executable = executable
        self.lock = threading.Lock()
        self.samples: deque[dict] = deque(maxlen=HISTORY_MAX)
        self.latest: dict | None = None
        self.gpu = {"index": None, "name": "NVIDIA GPU", "total": None}
        self.error: str | None = None
        self.seq = 0
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    # ---- 生命周期 -------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._stream()
            except FileNotFoundError:
                self._set_error("未找到 nvidia-smi，请先安装 NVIDIA 驱动。")
            except Exception as exc:  # noqa: BLE001
                log.exception("采集异常")
                self._set_error("读取 GPU 数据失败：%s" % exc)
            if not self._stop.is_set():
                self._stop.wait(2.0)

    def _stream(self) -> None:
        args = [
            self.executable,
            "--query-gpu=" + ",".join(self.FIELDS),
            "--format=csv,noheader,nounits",
            "-lms",
            str(self.interval_ms),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        self._proc = proc
        bad_output = None
        code = None
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if self._stop.is_set():
                    break
                raw = raw.strip()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                parsed = self._parse(line)
                if parsed is None:
                    # 报错信息走 Windows OEM 代码页（中文系统是 CP936），
                    # 直接用 UTF-8 解会变成乱码。
                    try:
                        line = raw.decode("oem")
                    except (LookupError, UnicodeDecodeError):
                        pass
                    bad_output = line[:300]
                    self._set_error("nvidia-smi 返回异常：%s" % bad_output)
                    continue
                sample, gpu = parsed
                self._clear_error()
                self._publish(sample, gpu)
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            self._proc = None

        if not self._stop.is_set() and bad_output is None:
            self._set_error("nvidia-smi 已退出（代码 %s）。" % code)

    # ---- 解析与发布 -----------------------------------------------
    def _parse(self, line: str) -> tuple[dict, dict] | None:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != len(self.FIELDS):
            return None
        try:
            index = int(float(parts[0]))
        except ValueError:
            return None
        total = parse_number(parts[2])
        used = parse_number(parts[3])
        if total is None or used is None:
            return None
        reserved = parse_number(parts[4])
        util = parse_number(parts[5])
        temp = parse_number(parts[6])
        power = parse_number(parts[7])
        clock = parse_number(parts[8])
        sample = {
            "t": round(time.time(), 3),
            "mem": int(round(used)),
            "total": int(round(total)),
            "reserved": int(round(reserved)) if reserved is not None else None,
            "util": int(round(util)) if util is not None else None,
            "temp": round(temp, 1) if temp is not None else None,
            "power": round(power, 1) if power is not None else None,
            "clock": int(round(clock)) if clock is not None else None,
        }
        gpu = {
            "index": index,
            "name": parts[1] or "NVIDIA GPU",
            "total": int(round(total)),
        }
        return sample, gpu

    def _publish(self, sample: dict, gpu: dict) -> None:
        with self.lock:
            self.seq += 1
            sample["seq"] = self.seq
            self.samples.append(sample)
            self.latest = sample
            self.gpu = gpu

    def _set_error(self, message: str) -> None:
        with self.lock:
            if self.error != message:
                log.warning(message)
            self.error = message

    def _clear_error(self) -> None:
        with self.lock:
            if self.error is not None:
                log.info("GPU 数据已恢复")
            self.error = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "seq": self.seq,
                "gpu": dict(self.gpu),
                "error": self.error,
                "samples": list(self.samples),
                "latest": dict(self.latest) if self.latest else None,
            }


class ProcessSampler(threading.Thread):
    """每 2 秒读一次每个进程的本地显存，按进程名汇总前 6 名。"""

    PATHS = (
        ("local", r"\GPU Process Memory(*)\Local Usage"),
        ("dedicated", r"\GPU Process Memory(*)\Dedicated Usage"),
    )

    def __init__(self, interval: float = 2.0) -> None:
        super().__init__(name="process-sampler", daemon=True)
        self.interval = interval
        self.lock = threading.Lock()
        self.items: list[dict] = []
        self.total = 0
        self.metric: str | None = None
        self.error: str | None = None
        self.seq = 0
        self._stop = threading.Event()
        self._query: ctypes.c_void_p | None = None
        self._counter: ctypes.c_void_p | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    if self._query is None:
                        self._open()
                    if self._query is not None:
                        self._sample()
                except Exception as exc:  # noqa: BLE001
                    self._fail("进程显存读取失败：%s" % exc)
                self._stop.wait(self.interval)
        finally:
            self._close_query()

    def _open(self) -> None:
        for metric, path in self.PATHS:
            query = ctypes.c_void_p()
            counter = ctypes.c_void_p()
            if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                continue
            if _pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter)) == 0:
                self._query = query
                self._counter = counter
                self.metric = metric
                return
            _pdh.PdhCloseQuery(query)
        self.metric = None
        self._fail("系统不支持进程显存统计。")

    def _sample(self) -> None:
        assert self._query is not None and self._counter is not None
        if _pdh.PdhCollectQueryData(self._query) != 0:
            raise OSError("PdhCollectQueryData 失败")

        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        _pdh.PdhGetFormattedCounterArrayW(
            self._counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
        )
        rows: list[tuple[str, float]] = []
        if size.value:
            buffer = ctypes.create_string_buffer(size.value)
            status = _pdh.PdhGetFormattedCounterArrayW(
                self._counter,
                _PDH_FMT_DOUBLE,
                ctypes.byref(size),
                ctypes.byref(count),
                buffer,
            )
            if status != 0:
                raise OSError("PdhGetFormattedCounterArrayW 失败（%s）" % status)
            array = ctypes.cast(buffer, ctypes.POINTER(_PdhCounterItem))
            rows = [
                (array[index].name, array[index].value.value)
                for index in range(count.value)
            ]

        by_pid: dict[int, float] = {}
        for instance, value in rows:
            match = re.match(r"pid_(\d+)_", instance)
            if not match or value <= 0:
                continue
            pid = int(match.group(1))
            by_pid[pid] = by_pid.get(pid, 0.0) + value

        names = process_names()
        paths = {pid: process_path(pid) for pid in by_pid}
        groups: dict[str, dict] = {}
        for pid, value in by_pid.items():
            label = names.get(pid, "PID %d" % pid)
            group = groups.setdefault(
                label, {"bytes": 0.0, "count": 0, "by_path": {}}
            )
            group["bytes"] += value
            group["count"] += 1
            path = paths.get(pid) or ""
            if path:
                entry = group["by_path"].setdefault(path, {"bytes": 0.0, "pids": []})
                entry["bytes"] += value
                entry["pids"].append(pid)

        items = []
        for name, values in groups.items():
            ordered = sorted(
                values["by_path"].items(),
                key=lambda pair: pair[1]["bytes"],
                reverse=True,
            )
            primary_path = ordered[0][0] if ordered else ""
            primary_pid = None
            if ordered:
                primary_pid = max(ordered[0][1]["pids"], key=lambda pid: by_pid[pid])
            items.append(
                {
                    "name": name,
                    "bytes": int(values["bytes"]),
                    "count": values["count"],
                    "desc": describe_process(name),
                    "path": primary_path,
                    "pid": primary_pid,
                    "paths": [path for path, _ in ordered[:4]],
                }
            )
        items.sort(key=lambda item: item["bytes"], reverse=True)
        total = int(sum(by_pid.values()))

        with self.lock:
            self.items = items[:6]
            self.total = total
            self.error = None
            self.seq += 1

    def _fail(self, message: str) -> None:
        self._close_query()
        with self.lock:
            if self.error != message:
                log.warning(message)
                self.seq += 1
            self.error = message

    def _close_query(self) -> None:
        query = self._query
        self._query = None
        self._counter = None
        if query is not None:
            _pdh.PdhCloseQuery(query)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "seq": self.seq,
                "items": [dict(item) for item in self.items],
                "total": self.total,
                "metric": self.metric,
                "error": self.error,
            }


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sampler = GpuSampler(args.interval, args.nvidia_smi)
        self.processes = ProcessSampler()
        self.server: Server | None = None
        self.lock = threading.Lock()
        self.clients = 0
        self.started = time.time()
        self.last_activity = time.time()
        self.stopping = False
        self._disconnect_timer: threading.Timer | None = None
        self.accent, self.system_dark = read_system_style()

    def touch(self) -> None:
        self.last_activity = time.time()

    def client_connected(self) -> None:
        """页面连上时取消“页面已关闭”的退出计时。"""
        timer = None
        with self.lock:
            self.clients += 1
            timer = self._disconnect_timer
            self._disconnect_timer = None
        if timer is not None:
            timer.cancel()

    def client_disconnected(self) -> None:
        """最后一个页面断开后，短暂等待刷新重连，否则立刻退出。"""
        timer = None
        with self.lock:
            self.clients = max(0, self.clients - 1)
            if self.clients > 0 or self.stopping or self.args.keep_alive:
                return
            if self._disconnect_timer is not None:
                self._disconnect_timer.cancel()
            timer = threading.Timer(DISCONNECT_EXIT_SECONDS, self._disconnect_shutdown)
            timer.daemon = True
            self._disconnect_timer = timer
        timer.start()

    def _disconnect_shutdown(self) -> None:
        with self.lock:
            if self.clients > 0 or self.stopping or self.args.keep_alive:
                return
        self.begin_shutdown("page closed")

    def begin_shutdown(self, reason: str) -> None:
        timer = None
        with self.lock:
            if self.stopping:
                return
            self.stopping = True
            timer = self._disconnect_timer
            self._disconnect_timer = None
        if timer is not None:
            timer.cancel()
        log.info("开始退出：%s", reason)
        self.sampler.stop()
        self.processes.stop()
        server = self.server
        if server is not None:
            threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # 必须关掉：Windows 上 SO_REUSEADDR 允许两个进程绑同一端口，
    # 第二个实例不会报错，而是在两个服务之间随机分流请求。
    allow_reuse_address = False

    def __init__(self, address, handler, app: App) -> None:
        super().__init__(address, handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VRAMMonitor/" + VERSION

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- GET ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        app = self.server.app
        app.touch()

        if path == "/api/stream":
            self._stream()
            return
        if path == "/api/health":
            snap = app.sampler.snapshot()
            proc = app.processes.snapshot()
            self._json(
                {
                    "ok": True,
                    "version": VERSION,
                    "gpu": snap["gpu"],
                    "error": snap["error"],
                    "seq": snap["seq"],
                    "clients": app.clients,
                    "processes": {
                        "seq": proc["seq"],
                        "total": proc["total"],
                        "error": proc["error"],
                    },
                }
            )
            return
        if path in ("/", "/index.html"):
            self._file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json({"ok": False, "error": "not found"}, 404)

    # ---- POST -----------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        app = self.server.app
        app.touch()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        path = urlparse(self.path).path
        if path == "/api/shutdown":
            if not self._origin_ok():
                self._json({"ok": False, "error": "forbidden"}, 403)
                return
            self._json({"ok": True})
            threading.Thread(target=self._shutdown_later, args=(app,), daemon=True).start()
            return
        if path == "/api/reveal":
            self._reveal(body)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def _reveal(self, body: bytes) -> None:
        if not self._origin_ok():
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            pid = int(payload.get("pid") or 0)
        except (ValueError, TypeError, UnicodeDecodeError, AttributeError):
            self._json({"ok": False, "error": "无效的进程 ID"}, 400)
            return
        if pid <= 0:
            self._json({"ok": False, "error": "无效的进程 ID"}, 400)
            return
        path = process_path(pid)
        if not path or not os.path.isfile(path):
            self._json(
                {"ok": False, "error": "进程路径不可用（可能已退出或受保护）"}, 404
            )
            return
        try:
            subprocess.Popen(
                ["explorer.exe", "/select,", path],
                close_fds=True,
            )
        except OSError as exc:
            self._json({"ok": False, "error": "无法打开资源管理器：%s" % exc}, 500)
            return
        self._json({"ok": True})

    def _shutdown_later(self, app: App) -> None:
        time.sleep(0.3)
        app.begin_shutdown("web")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin.rstrip("/") in {
            "http://127.0.0.1:%d" % port,
            "http://localhost:%d" % port,
        }

    # ---- SSE ------------------------------------------------------
    def _stream(self) -> None:
        app = self.server.app
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True

        app.client_connected()
        last_seq = 0
        last_error = None
        last_proc_seq = -1
        last_ping = time.time()

        try:
            snap = app.sampler.snapshot()
            proc = app.processes.snapshot()
            self._event(
                "init",
                {
                    "version": VERSION,
                    "interval": app.sampler.interval_ms,
                    "gpu": snap["gpu"],
                    "error": snap["error"],
                    "theme": {"accent": app.accent, "dark": app.system_dark},
                    "history": snap["samples"][-CHART_POINTS:],
                    "seq": snap["seq"],
                    "processes": proc,
                },
            )
            last_seq = snap["seq"]
            last_error = snap["error"]
            last_proc_seq = proc["seq"]

            while not app.stopping:
                time.sleep(0.1)
                snap = app.sampler.snapshot()
                if snap["seq"] > last_seq:
                    for sample in snap["samples"]:
                        if sample["seq"] > last_seq:
                            self._event("sample", sample)
                    last_seq = snap["seq"]
                if snap["error"] != last_error:
                    self._event("state", {"error": snap["error"]})
                    last_error = snap["error"]
                proc = app.processes.snapshot()
                if proc["seq"] != last_proc_seq:
                    self._event("processes", proc)
                    last_proc_seq = proc["seq"]
                now = time.time()
                if now - last_ping >= 15:
                    self._chunk(": ping\n\n")
                    last_ping = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            app.client_disconnected()
            app.touch()
            try:
                self._chunk("")
            except OSError:
                pass

    def _event(self, name: str, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._chunk("event: %s\ndata: %s\n\n" % (name, data))

    def _chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        self.wfile.write(b"%X\r\n" % len(data))
        if data:
            self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    # ---- 响应工具 -------------------------------------------------
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"ok": False, "error": "missing"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def watchdog(app: App) -> None:
    if app.args.keep_alive:
        return
    while not app.stopping:
        time.sleep(1.0)
        with app.lock:
            clients = app.clients
        idle = time.time() - app.last_activity
        if clients == 0 and idle > IDLE_EXIT_SECONDS and time.time() - app.started > 15:
            app.begin_shutdown("idle")
            return


def existing_instance(port: int) -> bool:
    """默认端口上已经跑着本工具时，直接打开它，不再起第二个服务。"""
    if not port:
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=0.6) as resp:
            if not resp.headers.get("Server", "").startswith("VRAMMonitor/"):
                return False
            data = json.loads(resp.read(4096).decode("utf-8"))
        return bool(data.get("ok"))
    except Exception:
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地实时 NVIDIA 显存监控")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765；被占用时自动换一个")
    parser.add_argument("--interval", type=int, default=500, help="采样间隔毫秒，默认 500")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--keep-alive", action="store_true", help="没有页面连接时也不自动退出")
    parser.add_argument("--nvidia-smi", default="nvidia-smi", help="nvidia-smi 可执行文件路径，默认从 PATH 查找")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    setup_logging()
    log.info("启动 %s v%s，请求端口 %s", APP_NAME, VERSION, args.port)

    if existing_instance(args.port):
        url = "http://127.0.0.1:%d/" % args.port
        log.info("已有实例在运行：%s", url)
        if getattr(sys, "stdout", None):
            print("%s 已在运行：%s" % (APP_NAME, url), flush=True)
        if not args.no_browser:
            open_page(url)
        return

    app = App(args)
    try:
        server = Server(("127.0.0.1", args.port), Handler, app)
    except OSError:
        log.warning("端口 %s 不可用，改用随机端口", args.port)
        server = Server(("127.0.0.1", 0), Handler, app)
    app.server = server
    port = server.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    log.info("监听 %s", url)

    app.sampler.start()
    app.processes.start()
    threading.Thread(target=watchdog, args=(app,), daemon=True).start()

    if getattr(sys, "stdout", None):
        print("%s 已启动：%s  （关闭页面后自动退出）" % (APP_NAME, url), flush=True)
    if not args.no_browser:
        threading.Timer(0.7, open_page, args=(url,)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping = True
        app.sampler.stop()
        app.processes.stop()
        server.server_close()
    log.info("已退出")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log.exception("主程序异常")
        show_fatal("%s启动失败：\n%s\n\n详细信息见 logs/app.log" % (APP_NAME, exc))
        sys.exit(1)
