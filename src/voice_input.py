# -*- coding: utf-8 -*-
"""
Windows 语音输入法 (讯飞开放平台 iat 引擎)
=========================================
GitHub: https://github.com/arthurqwang/ArthurVoiceInput

- 屏幕常驻一个 28x28 的小浮窗，按住它说话，松开即识别并输出到当前光标
- 额外支持全局热键 Ctrl+Alt+Space 按住切换录音
- 识别结果三种去向 (VI_MODE):
    clipboard  默认，复制后 Ctrl+V 填入光标
    type       模拟逐字输入 (对中文更通用)
    kb         语音速记：写入当日速记文件，同时仍填入光标

依赖:
    pip install -r requirements.txt

运行:
    python voice_input.py
环境变量(可选):
    VI_MODE   =clipboard|type|kb    注入方式 (默认 clipboard)
    VI_KB_DIR =路径                 kb 模式落盘目录 (默认脚本下 voice_notes/)
讯飞凭证(必填，或用 run_xfyun.bat 一键启动):
    XFYUN_APPID / XFYUN_APIKEY / XFYUN_APISECRET
"""
import os
import re
import sys
import time

# ---- 冻结包 Tcl/Tk 路径兜底（必须在任何 import tkinter 之前执行）----
# PyInstaller 6.x 的 pyi_rth__tkinter 钩子把 TCL_LIBRARY 指向
# `sys._MEIPASS/_tcl_data`，但实际数据在 onedir/onefile 中均位于
# `_internal/_tcl_data/` 子目录，导致钩子找不到、Tcl 退回到系统 Tcl；
# 系统 Tcl 与打包 Tk 版本不一致时即报 "Can't find a usable init.tcl"。
# 这里的兜底遍历 _tcl_data 可能位置，确保 TCL_LIBRARY/TK_LIBRARY 指向真实数据。
if getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    for _sub in ("_internal/_tcl_data", "_tcl_data"):
        _p = os.path.join(_base, _sub)
        if os.path.isfile(os.path.join(_p, "init.tcl")):
            os.environ["TCL_LIBRARY"] = _p
            break
    for _sub in ("_internal/_tk_data", "_tk_data"):
        _p = os.path.join(_base, _sub)
        if os.path.isfile(os.path.join(_p, "tk.tcl")):
            os.environ["TK_LIBRARY"] = _p
            break
    del _sub, _p, _base
# --------------------------------------------------------------------
import math
import wave
import tempfile
import threading
import logging

import tkinter as tk
import tkinter.messagebox  # tk.messagebox 需显式导入

logger = logging.getLogger("voice_input")


LOCK_FILE = os.path.join(tempfile.gettempdir(), "voice_input.lock")


def app_dir():
    """程序根目录：打包后为 exe 所在目录（sys.frozen），开发时为脚本所在目录。
    日志/配置/语音速记等落盘文件都应以它为基准，避免写入 PyInstaller 临时解压目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """只读资源文件路径：打包后从 PyInstaller 临时解压目录（sys._MEIPASS）读取，
    开发时从脚本所在目录读取。用于浮窗 logo / 窗口图标等随 exe 打包的静态资源。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def setup_logging():
    logging.basicConfig(
        filename=os.path.join(app_dir(), "voice_input.log"),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


_AUTOSTART_NAME = "阿色语音快捷输入法"   # HKCU Run 键名


def _autostart_enabled():
    """读取 HKCU\\...\\Run 键：是否已配置开机自动启动。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, _AUTOSTART_NAME)
            return True
    except OSError:
        return False


def _set_autostart(enabled):
    """写/删 HKCU Run 键：enabled=True 写入开机自启动命令（pythonw 无控制台窗口）。"""
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if not enabled:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, _AUTOSTART_NAME)
        except OSError:
            pass
        return
    if getattr(sys, "frozen", False):
        # 打包版：直接用 exe 自身（无控制台窗口），无需 pythonw
        cmd = '"%s"' % sys.executable
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        cmd = '"%s" -u "%s"' % (pythonw, os.path.abspath(__file__))
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)


def _sync_autostart_path():
    """打包版（exe）启动自检：若开机自启动已启用但指向的不是当前 exe，
    自动更新为当前 exe 路径——exe 改名/移动后不会再次静默失效。
    仅 frozen 生效；开发版（python 运行）不干预。"""
    if not getattr(sys, "frozen", False):
        return
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    cur = '"%s"' % sys.executable
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            old = winreg.QueryValueEx(k, _AUTOSTART_NAME)[0]
    except OSError:
        return  # 未启用自启动，不主动创建
    if old != cur:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, cur)
            logger.info("自启动路径已自动更新：%s", cur)
        except Exception as e:
            logger.warning("自启动路径自动更新失败: %s", e)


def acquire_single_instance():
    """确保只有一个实例在跑：发现旧实例存活则终止它。
    Windows 下用 OpenProcess/TerminateProcess 探测与强杀——
    os.kill(pid, 0) 在 Windows 上不受支持会抛 OSError，
    导致旧实例杀不掉、双实例并存（两个浮窗重叠）。"""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old = int(f.read().strip())
            if _win_pid_alive(old):
                logger.info("发现旧实例 PID=%s，终止", old)
                _win_terminate(old)
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.warning("单实例锁处理失败: %s", e)

# ---------- Windows API ----------
import ctypes
import ctypes.wintypes as wt

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001


def _win_pid_alive(pid):
    """Windows 下检测进程是否存活（替代不可靠的 os.kill(pid, 0)）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _win_terminate(pid):
    """Windows 下强杀进程（替代 os.kill(pid, 9)）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        kernel32.TerminateProcess.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def set_dpi_aware():
    """高 DPI 下坐标才准。优先 PerMonitorV2，失败回退到 System 或 System_DPI。"""
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            # Windows Vista/7/8
            user32.SetProcessDPIAware()
        except Exception:
            pass


EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

last_foreground = {"hwnd": 0}   # 记录最近一次"非本程序"的前台窗口 (即目标输入窗口)
our_hwnd = 0


def _is_our_window(hwnd):
    """判断窗口是否属于本进程（按 PID 反查，最可靠）。

    背景：`app.root.winfo_id()` 在 overrideredirect / Tk 顶层包装下可能与
    浮窗的真实 HWND 不一致，导致浮窗被误判为"外部窗口"、on_press 把目标
    快照成浮窗自己、文字注入回自身而消失（十轮"不落字"的真正根因）。
    按 PID 判断与本进程同属，绝不误判。"""
    if not hwnd:
        return False
    try:
        user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user32.GetWindowThreadProcessId.restype = wt.DWORD
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return False


def _find_own_visible_hwnd():
    """枚举本进程所有可见顶层窗口（用于启动时校准 our_hwnd）。"""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        if _is_our_window(hwnd) and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    cb = WNDENUMPROC(_cb)
    user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
    user32.EnumWindows.restype = wt.BOOL
    user32.EnumWindows(cb, 0)
    return found

# 前台锁定超时（让 SetForegroundWindow 在跨进程时可靠生效）
_g_foreground_lock_old = None
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001


def _disable_foreground_lock():
    """临时把前台锁定超时设为 0，使本进程能可靠地把焦点还给目标窗口。退出时恢复。"""
    global _g_foreground_lock_old
    try:
        user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
        user32.SystemParametersInfoW.restype = wt.BOOL
        old = wt.DWORD(0)
        got = user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old), 0)
        # 仅当 GET 成功且数值合理(<=1小时)时才记录，避免把可疑值写回系统
        if got and old.value <= 3600000:
            _g_foreground_lock_old = old.value
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(wt.DWORD(0)), 0)
        logger.info("前台锁定超时已设为0(原值=%s)，焦点归还将可靠", _g_foreground_lock_old)
    except Exception as e:
        logger.warning("设置前台锁定超时失败: %s", e)


def _restore_foreground_lock():
    global _g_foreground_lock_old
    if _g_foreground_lock_old is not None:
        try:
            user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                         ctypes.byref(wt.DWORD(_g_foreground_lock_old)), 0)
            logger.info("前台锁定超时已恢复为 %s", _g_foreground_lock_old)
        except Exception:
            pass



def _win_event_proc(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
    # 只记录"其它窗口"成为前台的情况，本程序浮窗自身获得焦点时忽略
    if event == EVENT_SYSTEM_FOREGROUND and not _is_our_window(hwnd):
        last_foreground["hwnd"] = hwnd
    return 0


_WinEventProc = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
    ctypes.c_long, ctypes.c_long, ctypes.c_ulong, ctypes.c_ulong
)
_win_event_proc_cb = _WinEventProc(_win_event_proc)


def install_foreground_hook():
    return user32.SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
        0, _win_event_proc_cb, 0, 0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
    )


# ---------- 焦点恢复 + 文本注入 ----------
def restore_focus(hwnd=None):
    """将焦点还给目标输入窗口。返回 True 表示前台已成功切到目标。
    hwnd 为 None 时使用 last_foreground 快照。"""
    if hwnd is None:
        hwnd = last_foreground["hwnd"]
    logger.info("restore_focus: target hwnd=%s", hwnd)
    if not hwnd:
        logger.warning("restore_focus: 无目标窗口句柄，文字可能无法送达光标")
        return False
    hwnd = int(hwnd)
    ok = False
    try:
        user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user32.GetWindowThreadProcessId.restype = wt.DWORD
        user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
        user32.AttachThreadInput.restype = wt.BOOL
        pid = wt.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cur = kernel32.GetCurrentThreadId()
        attached = False
        if tid and tid != cur:
            attached = user32.AttachThreadInput(cur, tid, True)
        try:
            # ⚠️ 不能无条件 ShowWindow(hwnd, 9)=SW_RESTORE：它会把「最大化」的窗口
            # 还原成正常大小（用户实测：点图标后 WorkBuddy 窗口变小了！）。
            # 仅当窗口处于最小化状态时才恢复显示；否则只提到前台。
            user32.IsIconic.argtypes = [wt.HWND]
            user32.IsIconic.restype = wt.BOOL
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)        # SW_RESTORE（仅最小化时）
            user32.BringWindowToTop(hwnd)
            # 仅把窗口提到前台；不要对顶层窗口强行 SetFocus，否则会把
            # Electron/Chrome 的可编辑子控件（hwndFocus）的焦点抢走，导致随后
            # 的 Ctrl+V / WM_PASTE 落不到输入框。SetForegroundWindow 会自动把
            # 键盘焦点恢复到该窗口此前拥有焦点的子控件。
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur, tid, False)
        # 验证前台是否真的切换过去
        fg = user32.GetForegroundWindow()
        if int(fg) == hwnd:
            logger.info("restore_focus: 焦点已成功还给 hwnd=%s (PID=%s)",
                        hwnd, pid.value)
            ok = True
        else:
            logger.warning("restore_focus: 前台仍为 %s（期望 %s），文字可能送错窗口",
                           fg, hwnd)
    except Exception as e:
        logger.error("restore_focus 异常: %s", e)
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    time.sleep(0.12)
    return ok


def send_ctrl_v(target_hwnd=None):
    """使用 SendInput 模拟 Ctrl+V（比 keybd_event 更可靠，不会被 UIPI 过滤）。
    target_hwnd 非空时，会先把本线程绑定到目标线程的输入队列，
    否则后台线程调用 SendInput 可能被 Windows 静默丢弃（返回 0）。"""
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002

    inputs = []
    def key_down(vk):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = vk
        inp.ki.wScan = 0
        inp.ki.dwFlags = 0
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0
        inputs.append(inp)

    def key_up(vk):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = vk
        inp.ki.wScan = 0
        inp.ki.dwFlags = KEYEVENTF_KEYUP
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0
        inputs.append(inp)

    key_down(VK_CONTROL)
    key_down(VK_V)
    key_up(VK_V)
    key_up(VK_CONTROL)

    sent, err = _send_inputs(inputs, target_hwnd)
    logger.info("send_ctrl_v: SendInput 发送 %d 个事件，成功 %d 个，GetLastError=%s",
                len(inputs), sent, err)
    return sent == len(inputs)


# ---------- 文本光标目标检测 (让图标吸附到当前插入点正下方 5px) ----------
class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


# ---------- 分层窗口 (per-pixel alpha 真半透明) ----------
class SIZE(ctypes.Structure):
    _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", wt.BYTE), ("BlendFlags", wt.BYTE),
                ("SourceConstantAlpha", wt.BYTE), ("AlphaFormat", wt.BYTE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]


AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
ULW_ALPHA = 0x02
BI_RGB = 0
WS_EX_LAYERED = 0x00080000
GWL_EXSTYLE = -20
_ICON_SIZE = 28
_ICON_SIZE_HOVER = 34        # 鼠标悬停/按住时放大尺寸（提示可互动）
# 圆饼不透明度（0-255，越大越不透明）：平时 160（63%，清晰可辨），
# 录音中 200（78%，更明显）；hover 时再 +50 提高清晰度。
_ALPHA_IDLE = 160
_ALPHA_REC = 200
_ALPHA_HOVER_BOOST = 50
# 话筒颜色：平时暖白米色（#f5efe0），hover 纯白；录音闪烁用 MIC_DIM 作暗帧
MIC_GRAY = (245, 239, 224)
MIC_WHITE = (255, 255, 255)
MIC_DIM = (140, 47, 42)          # 深酒红（录音闪烁暗帧，与白帧交替）
_ANIM_MS = 120                  # 录音动画帧间隔（波形跳动/话筒闪烁）
_PRESS_MS = 350                 # 长按判定阈值：鼠标左键按住超过此时长（ms）才跟随定位
_PRESS_MOVE_PX = 15             # 长按判定允许的鼠标位移（px）；超过视为拖选文本/拖动，不跟位
APP_VERSION = "v0.0.1"  # 产品版本号（配置/帮助对话框显示）


def _hex2rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _render_icon_bitmap(color, alpha, size=_ICON_SIZE, mic_rgb=MIC_GRAY, wave=None):
    """渲染 size×size BGRA 位图：高透明红色圆饼（有填充）+ 麦克风图标。
    返回 (hdc_mem, hbmp, buf_addr)，供 UpdateLayeredWindow 使用；用完需释放。
    size 支持放大（hover 态），所有形状坐标按 size/28 比例缩放。
    mic_rgb 为话筒 RGB：平时暖白米色，hover 时纯白。
    wave 为录音波形（3 元组，各条高度 px，0 不画）：左右对称各 3 条频谱条，
    位于话筒两侧、全部限制在圆饼内（超出红饼的像素不画）。"""
    gdi32 = ctypes.windll.gdi32
    r, g, b = _hex2rgb(color)
    w = h = size
    s = size / float(_ICON_SIZE)
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.ReleaseDC.restype = wt.INT
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteObject.restype = wt.BOOL
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.DeleteDC.restype = wt.BOOL

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 自顶向下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    buf = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0,
                                  ctypes.byref(buf), None, 0)
    if not hbmp:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        return None, None, None
    gdi32.SelectObject(hdc_mem, hbmp)
    px = (wt.BYTE * (w * h * 4)).from_address(buf.value)
    # 形状基准（28 尺寸下的坐标），按 s 比例缩放
    cx = cy = 14 * s                       # 圆饼圆心
    r_disc2 = (11 * s) ** 2                # 圆饼半径 11
    # 麦克风（深灰 #5f6368）：标准 mic 造型参数（28 基准，按 s 缩放）
    mic_cx = 14 * s                      # 话筒中轴 x
    mic_r = 3 * s                        # 主体胶囊半径（宽 7）
    mic_y0, mic_y1 = 8 * s, 14 * s       # 主体线段上下端（胶囊）
    mic_r2 = mic_r * mic_r
    arc_cy = 14 * s                      # 罩弧圆心 y（与主体下端同心）
    arc_r1_2 = (4.5 * s) ** 2            # 罩弧环带 4.5~6.5
    arc_r2_2 = (6.5 * s) ** 2
    base_x0, base_x1 = 9 * s, 19 * s     # 底座横线范围
    base_y0, base_y1 = 21 * s, 22 * s
    dot1_x, dot2_x = 9.5 * s, 18.5 * s   # 底座两端圆点圆心
    dot_y = 21.5 * s
    dot_r2 = (1.5 * s) ** 2
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 4
            a = cr = cg = cb = 0
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= r_disc2:      # 圆饼（有填充）：高透明红
                a, cr, cg, cb = alpha, r, g, b
            # 麦克风图标（深灰 #5f6368）：圆头胶囊主体 + 下凹罩弧 + 圆点底座。
            # 主体优先（含两端圆头），其次底座，最后罩弧只补两侧可见像素。
            if (abs(x - mic_cx) <= mic_r and mic_y0 <= y <= mic_y1) or \
               ((x - mic_cx) ** 2 + (y - mic_y0) ** 2 <= mic_r2) or \
               ((x - mic_cx) ** 2 + (y - mic_y1) ** 2 <= mic_r2) or \
               ((x - dot1_x) ** 2 + (y - dot_y) ** 2 <= dot_r2) or \
               ((x - dot2_x) ** 2 + (y - dot_y) ** 2 <= dot_r2):
                a, cr, cg, cb = 210, mic_rgb[0], mic_rgb[1], mic_rgb[2]
            elif base_x0 <= x <= base_x1 and base_y0 <= y <= base_y1:
                a, cr, cg, cb = 210, mic_rgb[0], mic_rgb[1], mic_rgb[2]
            else:
                # 罩弧（下凹 ︶，圆心与主体下端同心，只画下半环）
                ax, ay = x - mic_cx, y - arc_cy
                d2 = ax * ax + ay * ay
                if arc_r1_2 <= d2 <= arc_r2_2 and y >= arc_cy:
                    a, cr, cg, cb = 210, mic_rgb[0], mic_rgb[1], mic_rgb[2]
            # 录音波形：左右对称各 3 条频谱条（白色），仅限圆饼内
            if wave and dx * dx + dy * dy <= r_disc2:
                wbase = 15 * s                      # 条底部 y
                for wi, wh_ in enumerate(wave):
                    if wh_ <= 0:
                        continue
                    htop = wbase - wh_ * s          # 条顶 y（向上跳动）
                    for side in (-1, 1):
                        wx = (14 + side * (5 + 2 * wi)) * s   # 距中心 5/7/9
                        if wx - 0.5 <= x < wx + 0.5 and htop <= y <= wbase:
                            a, cr, cg, cb = 210, MIC_WHITE[0], MIC_WHITE[1], MIC_WHITE[2]
            px[i] = cb
            px[i + 1] = cg
            px[i + 2] = cr
            px[i + 3] = a
    return hdc_mem, hbmp, hdc_screen


def _apply_layered(hwnd, color, alpha, size=_ICON_SIZE, mic_rgb=MIC_GRAY, wave=None):
    """把高透明圆饼位图通过 UpdateLayeredWindow 画到浮窗 HWND 上。
    这是 per-pixel alpha 真半透明——文字从半透明红饼中清晰透出，
    且绝不出现 stipple 实心化/遮挡问题。"""
    if not hwnd:
        return
    try:
        user32.UpdateLayeredWindow.argtypes = [
            wt.HWND, wt.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
            wt.HDC, ctypes.POINTER(POINT), wt.COLORREF,
            ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
        user32.UpdateLayeredWindow.restype = wt.BOOL
        user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
        user32.GetWindowRect.restype = wt.BOOL
        hdc_mem, hbmp, hdc_screen = _render_icon_bitmap(color, alpha, size, mic_rgb, wave)
        if not hdc_mem:
            return
        try:
            # ULW 的 pptDst 是「新屏幕位置」：必须传当前窗口位置，
            # 否则窗口会被挪到 (0,0)（hover 重绘频繁时会更明显）。
            cur_x = cur_y = 0
            rect = RECT()
            if user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
                cur_x, cur_y = rect.left, rect.top
            dst = POINT(cur_x, cur_y)
            sz = SIZE(size, size)
            pt = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(int(hwnd), hdc_screen,
                                       ctypes.byref(dst), ctypes.byref(sz),
                                       hdc_mem, ctypes.byref(pt), 0,
                                       ctypes.byref(blend), ULW_ALPHA)
        finally:
            gdi32 = ctypes.windll.gdi32
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
    except Exception as e:
        logger.error("_apply_layered 异常: %s", e)


def _apply_logo_layered(hwnd, size, alpha, fallback_color):
    """用 AVI_logo28.png 渲染浮窗（idle 态 logo）。

    与 _apply_layered 走同一条 UpdateLayeredWindow 管道，但像素来自 PNG
    （RGBA → BGRA 字节序），整体不透明度由 SourceConstantAlpha=alpha 控制
    （平时 160 / hover 210，与旧红饼透明度语义一致）。
    PNG 缺失或加载失败时回退到 _apply_layered 原绘制，绝不让浮窗空白。
    """
    try:
        from PIL import Image
        p = resource_path("AVI_logo28.png")
        if not os.path.exists(p):
            logger.warning("_apply_logo_layered: 缺少 %s，回退原绘制", p)
            _apply_layered(hwnd, fallback_color, alpha, size, MIC_GRAY, None)
            return
        im = Image.open(p).convert("RGBA")
        if im.size != (size, size):
            im = im.resize((size, size), Image.LANCZOS)
        arr = np.array(im)
        h, w = arr.shape[:2]
        # 预乘 alpha（关键！）：UpdateLayeredWindow 的混合公式为
        #   输出 = 源RGB + (1-α)*背景色
        # 若透明像素 RGB 残留非零色（本 logo 透明区 RGB=255,0,0 纯红），
        # 黑色背景上会整片透出红色；预乘后 α=0 像素 RGB 归零，任何背景下都正确透明。
        a16 = arr[..., 3].astype(np.uint16)
        r_pm = (arr[..., 0].astype(np.uint16) * a16) >> 8
        g_pm = (arr[..., 1].astype(np.uint16) * a16) >> 8
        b_pm = (arr[..., 2].astype(np.uint16) * a16) >> 8
        # RGBA(预乘) -> BGRA（UpdateLayeredWindow 要求 B,G,R,A 字节序）
        px_src = np.empty((h, w, 4), dtype=np.uint8)
        px_src[..., 0] = b_pm
        px_src[..., 1] = g_pm
        px_src[..., 2] = r_pm
        px_src[..., 3] = arr[..., 3]

        gdi32 = ctypes.windll.gdi32
        user32.GetDC.argtypes = [wt.HWND]
        user32.GetDC.restype = wt.HDC
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        user32.ReleaseDC.restype = wt.INT
        gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
        gdi32.CreateCompatibleDC.restype = wt.HDC
        gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                           ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
        gdi32.CreateDIBSection.restype = wt.HBITMAP
        gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        gdi32.SelectObject.restype = wt.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
        gdi32.DeleteObject.restype = wt.BOOL
        gdi32.DeleteDC.argtypes = [wt.HDC]
        gdi32.DeleteDC.restype = wt.BOOL

        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 自顶向下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0,
                                      ctypes.byref(buf), None, 0)
        if not hbmp:
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
            _apply_layered(hwnd, fallback_color, alpha, size, MIC_GRAY, None)
            return
        gdi32.SelectObject(hdc_mem, hbmp)
        # 整块拷贝 BGRA 像素（w*h*4 字节）
        ctypes.memmove(buf.value, px_src.ctypes.data, w * h * 4)
        try:
            user32.UpdateLayeredWindow.argtypes = [
                wt.HWND, wt.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
                wt.HDC, ctypes.POINTER(POINT), wt.COLORREF,
                ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
            user32.UpdateLayeredWindow.restype = wt.BOOL
            user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
            user32.GetWindowRect.restype = wt.BOOL
            cur_x = cur_y = 0
            rect = RECT()
            if user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
                cur_x, cur_y = rect.left, rect.top
            dst = POINT(cur_x, cur_y)
            sz = SIZE(size, size)
            pt = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, alpha, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(int(hwnd), hdc_screen,
                                       ctypes.byref(dst), ctypes.byref(sz),
                                       hdc_mem, ctypes.byref(pt), 0,
                                       ctypes.byref(blend), ULW_ALPHA)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
    except Exception as e:
        logger.error("_apply_logo_layered 异常: %s", e)
        try:
            _apply_layered(hwnd, fallback_color, alpha, size, MIC_GRAY, None)
        except Exception:
            pass


def _enable_layered(hwnd):
    """给浮窗加 WS_EX_LAYERED 扩展样式（64 位安全）。"""
    try:
        user32.GetWindowLongPtrW.argtypes = [wt.HWND, wt.INT]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wt.HWND, wt.INT, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        style = user32.GetWindowLongPtrW(int(hwnd), GWL_EXSTYLE)
        user32.SetWindowLongPtrW(int(hwnd), GWL_EXSTYLE, style | WS_EX_LAYERED)
    except Exception as e:
        logger.error("_enable_layered 异常: %s", e)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", RECT),
    ]


def _screen_bounds():
    user32.GetSystemMetrics.argtypes = [wt.INT]
    user32.GetSystemMetrics.restype = wt.INT
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _work_area():
    """返回屏幕工作区 (left, top, right, bottom)——不含任务栏等系统区域。
    弹窗定位用它，避免浮窗在屏幕底端时弹窗底部落到任务栏之下。"""
    rect = RECT()
    try:
        user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
        user32.SystemParametersInfoW.restype = wt.BOOL
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    w, h = _screen_bounds()
    return 0, 0, w, h


def _fallback_position(hwnd):
    """拿不到文本光标时，把图标放到目标窗口右下角（留 12px 边距），
    保证在 WorkBuddy/Electron/浏览器等非标准 caret 应用中仍可见。"""
    if not hwnd:
        return None
    try:
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        sw, sh = _screen_bounds()
        # 右下角，留边距
        x = rect.right - 28 - 12
        y = rect.bottom - 28 - 12
        # 若窗口太窄，则改放到左上角内侧
        if x < rect.left + 12:
            x = rect.left + 12
        if y < rect.top + 12:
            y = rect.top + 12
        # 屏幕边界保护
        if x + 28 > sw:
            x = sw - 28
        if y + 28 > sh:
            y = sh - 28
        if x < 0:
            x = 0
        if y < 0:
            y = 0
        return x, y
    except Exception:
        return None


def _place_below_rect(left, top, right, bottom):
    """给定 caret 屏幕矩形，返回悬浮窗坐标：水平居中于光标、光标正下方 5px。
    含屏幕边界保护。供标准 caret 与 hwndCaret/GetCaretPos 路径共用。"""
    ICON, GAP = 28, 1
    try:
        sw, sh = _screen_bounds()
        cx = (left + right) // 2        # 光标水平中心
        x = cx - ICON // 2              # 悬浮窗水平居中于光标
        y = bottom + GAP                # 光标正下方 5px
        # 屏幕边界保护
        if x < 0:
            x = 0
        if x + ICON > sw:
            x = max(0, sw - ICON)
        if y + ICON > sh:
            y = max(0, sh - ICON - GAP)  # 底部空间不足：上移到光标上方
        if y < 0:
            y = 0
        return x, y
    except Exception:
        return None


def _place_by_caret(hwnd, rc):
    """标准 caret：rcCaret 是 hwnd 客户区坐标，先转屏幕坐标，
    再把悬浮窗定位到光标正下方 5px（水平居中于光标），屏幕边缘自动回退。"""
    try:
        pt1 = POINT(rc.left, rc.top)
        pt2 = POINT(rc.right, rc.bottom)
        user32.ClientToScreen(hwnd, ctypes.byref(pt1))
        user32.ClientToScreen(hwnd, ctypes.byref(pt2))
        if pt2.x <= pt1.x or pt2.y <= pt1.y:
            return _fallback_position(hwnd)
        pos = _place_below_rect(pt1.x, pt1.y, pt2.x, pt2.y)
        return pos if pos else _fallback_position(hwnd)
    except Exception:
        return _fallback_position(hwnd)


def _caret_via_hwndcaret(fg, tid, info):
    """部分应用（含部分 Electron/Chromium）不填充 rcCaret 但设置了 hwndCaret，
    用 GetCaretPos + ClientToScreen 拿真实光标位置。拿不到返回 None。"""
    try:
        ch = int(info.hwndCaret or 0)
        if not ch or _is_our_window(ch):
            return None
        cur = kernel32.GetCurrentThreadId()
        attached = False
        if tid != cur:
            attached = user32.AttachThreadInput(cur, tid, True)
        try:
            pt = POINT()
            if user32.GetCaretPos(ctypes.byref(pt)):
                if pt.x > 2 or pt.y > 2:  # 排除退化 (0,0)
                    user32.ClientToScreen(ch, ctypes.byref(pt))
                    pos = _place_below_rect(pt.x, pt.y, pt.x + 2, pt.y + 20)
                    if pos:
                        logger.debug("caret_target: hwndCaret 路径命中 hwnd=%s -> %s", ch, pos)
                        return pos
        finally:
            if attached:
                user32.AttachThreadInput(cur, tid, False)
    except Exception as e:
        logger.debug("_caret_via_hwndcaret 异常: %s", e)
    return None


def get_foreground_hwnd():
    """返回当前前台窗口句柄（排除本程序浮窗自身）；无则返回 None。"""
    try:
        user32.GetForegroundWindow.restype = wt.HWND
        fg = user32.GetForegroundWindow()
        if not fg or _is_our_window(fg):
            return None
        return fg
    except Exception:
        return None


def _cursor_pos_once():
    """读取一次鼠标屏幕坐标 (x, y)；失败返回 None。"""
    try:
        pt = POINT()
        user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        user32.GetCursorPos.restype = wt.BOOL
        if user32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    except Exception:
        pass
    return None


_last_caret_state = None   # 仅在前台窗口或「是否有真实 caret」切换时输出调试，避免轮询刷屏
_caret_err_logged = set()    # 同一 tid 的 AttachThreadInfo/GetGUIThreadInfo 失败只记一次，避免刷屏


def get_caret_target():
    """返回图标应放置的 (x, y)，或 None。
    优先：当前文本光标（闪烁插入点）正下方 5px（水平居中于光标）。
    无标准 caret（Electron/WorkBuddy/浏览器等）时返回 None，由上层锚定处理。"""
    try:
        user32.GetForegroundWindow.restype = wt.HWND
        fg = user32.GetForegroundWindow()
        if not fg:
            logger.debug("caret_target: no foreground window")
            return None
        # 前台是本程序浮窗 → 不跟（避免图标跟自己跑）
        if _is_our_window(fg):
            return None

        user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user32.GetWindowThreadProcessId.restype = wt.DWORD
        pid = wt.DWORD()
        tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if not tid:
            logger.debug("caret_target: GetWindowThreadProcessId failed for fg=%s", fg)
            return None

        user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
        user32.GetGUIThreadInfo.restype = wt.BOOL
        user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
        user32.AttachThreadInput.restype = wt.BOOL

        cur = kernel32.GetCurrentThreadId()
        attached = False
        if tid != cur:
            attached = user32.AttachThreadInput(cur, tid, True)
            if not attached:
                key = ("attach", int(tid))
                if key not in _caret_err_logged:
                    _caret_err_logged.add(key)
                    logger.debug("caret_target: AttachThreadInput(%s,%s) failed, err=%s",
                                 cur, tid, kernel32.GetLastError())
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        ok = user32.GetGUIThreadInfo(tid, ctypes.byref(info))
        if attached:
            user32.AttachThreadInput(cur, tid, False)
        if not ok:
            key = ("gui", int(tid))
            if key not in _caret_err_logged:
                _caret_err_logged.add(key)
                logger.debug("caret_target: GetGUIThreadInfo failed for tid=%s, err=%s",
                             tid, kernel32.GetLastError())
            return None
    except Exception as e:
        logger.debug("caret_target exception: %s", e)
        return None

    rc = info.rcCaret
    has_rect = rc.right > rc.left and rc.bottom > rc.top
    # 排除位于原点的退化矩形 (0,0,1,1)：表示该线程并无真实 caret，
    # 常见于 Electron / WorkBuddy / 浏览器等自绘输入框。
    has_real_caret = has_rect and rc.left > 2

    # 仅在「是否有真实 caret」或前台窗口发生切换时输出调试，
    # 否则每 300ms 轮询会把这个 DEBUG 刷成几 MB 日志，淹没真实问题。
    state = (int(fg), has_real_caret)
    global _last_caret_state
    if state != _last_caret_state:
        _last_caret_state = state
        if has_real_caret:
            carethwnd = info.hwndCaret if (info.hwndCaret and not _is_our_window(info.hwndCaret)) else fg
            logger.debug("caret_target: 真实 caret hwnd=%s rc=(%s,%s,%s,%s)",
                         carethwnd, rc.left, rc.top, rc.right, rc.bottom)
        else:
            logger.debug("caret_target: 无真实 caret (rc=%s,%s,%s,%s) fg=%s",
                         rc.left, rc.top, rc.right, rc.bottom, fg)

    # 标准文本光标存在 → 精确跟随（悬浮窗置于光标正下方 5px）
    if has_real_caret:
        carethwnd = info.hwndCaret if (info.hwndCaret and not _is_our_window(info.hwndCaret)) else fg
        return _place_by_caret(carethwnd, rc)

    # 无标准 rcCaret：部分 Chromium/Electron 设置了 hwndCaret，可经 GetCaretPos 命中
    hc_pos = _caret_via_hwndcaret(fg, tid, info)
    if hc_pos:
        return hc_pos

    # 无标准 caret：返回 None，由上层做"跟随鼠标"锚定处理
    return None


# SendInput 逐字符输入 (对中文等非 ASCII 最稳)
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        # 必须包含所有成员，使 union 尺寸与 Windows 一致：64 位下 MOUSEINPUT=28 字节，
        # 否则 sizeof(INPUT)=24 ≠ 真实 32，SendInput 会返回 ERROR_INVALID_PARAMETER(87)，
        # 表现为「SendInput 成功 0 个」——这是此前所有注入失败的根因。
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
    _fields_ = [("type", wt.DWORD), ("_u", _U)]
    _anonymous_ = ("_u",)


def _process_integrity_level():
    """返回当前进程完整性级别字符串（Medium/High/Low/...），用于诊断 UIPI 拦截。"""
    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        TOKEN_QUERY = 0x0008
        TokenIntegrityLevel = 25
        tok = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                         ctypes.byref(tok)):
            return "?"
        try:
            size = ctypes.c_ulong(0)
            advapi32.GetTokenInformation(tok, TokenIntegrityLevel, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(max(1, size.value))
            if not advapi32.GetTokenInformation(tok, TokenIntegrityLevel, buf, size.value,
                                                ctypes.byref(size)):
                return "?"
            # TOKEN_MANDATORY_LABEL: { SID_AND_ATTRIBUTES Label; } → Label.Sid 指针在结构开头
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_ptr:
                return "?"
            sid = ctypes.cast(sid_ptr, ctypes.POINTER(ctypes.c_ubyte))
            count = sid[1]  # SubAuthorityCount
            if count < 1:
                return "?"
            # SID 布局: Revision(1) Count(1) Authority(6) → SubAuthorities 从偏移 8 起
            sub = ctypes.cast(ctypes.byref(sid, 8), ctypes.POINTER(ctypes.c_ulong))
            level = sub[count - 1]
            names = {16: "Untrusted", 32: "Low", 12288: "Medium", 16384: "High", 20480: "System"}
            return names.get(level, "L%d" % level)
        finally:
            kernel32.CloseHandle(tok)
    except Exception:
        return "?"


def _log_inject_diag(target_hwnd=None):
    """SendInput 全失败时的环境诊断：目标/前台窗口归属 + 本进程完整性级别，
    用于判断 UIPI/权限拦截。"""
    try:
        cur = int(user32.GetForegroundWindow() or 0)
        tgt = int(target_hwnd) if target_hwnd else 0
        for label, hwnd in (("target", tgt), ("foreground", cur)):
            if not hwnd:
                logger.info("inject_diag: %s hwnd=0", label)
                continue
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            logger.info("inject_diag: %s hwnd=%s class=%r pid=%s",
                        label, hwnd, cls.value, pid.value)
        logger.info("inject_diag: 本进程完整性=%s (SendInput 0 事件)",
                    _process_integrity_level())
    except Exception as e:
        logger.debug("inject_diag 异常: %s", e)


def _char_input(ch, keyup=False):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wScan = ord(ch)
    inp.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
    return inp


def _send_inputs(inputs, target_hwnd=None):
    """发送一组 INPUT 键盘事件。

    ⚠️ 重要：**不要**在发送前做 AttachThreadInput 输入队列绑定——实测证明
    绑定后跨进程 SendInput 的事件会被路由进错误的队列，目标窗口收不到
    （表现为只落一个字面 'v' 或什么都不落）；而仅「SetForegroundWindow +
    SendInput」（AutoHotkey 同款）跨进程 Ctrl+V 粘贴完整命中（三策略实测全 PASS）。
    """
    try:
        # SendInput 只会把输入投递到「前台窗口」。在发送前再确保一次目标在前台，
        # 消除 restore_focus 与本次发送之间可能发生的焦点漂移（AutoHotkey 也这么做）。
        if target_hwnd:
            try:
                user32.SetForegroundWindow(int(target_hwnd))
            except Exception:
                pass
            # 前台切换需要时间生效（实测 0.02s 太短、事件会落到旧前台窗口；
            # 0.15s 可稳定命中，策略对比实验三策略全 PASS 用的就是 0.15s）。
            time.sleep(0.15)
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wt.UINT
        sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        # 必须在 SendInput 之后、任何其它 API 调用之前取 LastError，
        # 否则后面的调用会把错误码覆盖掉。
        err = kernel32.GetLastError()
        return sent, err
    except Exception as e:
        logger.error("_send_inputs 异常: %s", e)
        return 0, -1


def type_unicode(text, target_hwnd=None):
    inputs = []
    for ch in text:
        inputs.append(_char_input(ch, False))
        inputs.append(_char_input(ch, True))
    sent, err = _send_inputs(inputs, target_hwnd)
    time.sleep(0.01)
    ok = sent == len(inputs)
    logger.info("type_unicode: SendInput 发送 %d 个事件，成功 %d 个，GetLastError=%s",
                len(inputs), sent, err)
    if not ok:
        # 诊断：SendInput 全失败时的环境信息（前台窗口归属 + 完整性级别）
        _log_inject_diag(target_hwnd)
    return ok


def type_unicode_keybd(text, target_hwnd=None):
    """keybd_event 逐字符 Unicode 注入（SendInput 被系统拦截时的备选路径）。

    keybd_event 是旧版键盘 API，部分受限环境（SendInput 返回 0 且 LastError=0，
    如安全软件/输入法钩子拦截）下 SendInput 无效而 keybd_event 仍然有效。"""
    if not text:
        return False
    try:
        if target_hwnd:
            try:
                user32.SetForegroundWindow(int(target_hwnd))
            except Exception:
                pass
            time.sleep(0.15)
        user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                                       ctypes.c_ulong, ctypes.c_ulong]
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        for ch in text:
            sc = ord(ch) & 0xFFFF
            user32.keybd_event(0, sc, KEYEVENTF_UNICODE, 0)
            user32.keybd_event(0, sc, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(0.012)
        logger.info("type_unicode_keybd: keybd_event 注入 %d 字符", len(text))
        return True
    except Exception as e:
        logger.error("type_unicode_keybd 异常: %s", e)
        return False


WM_CHAR = 0x0102
WM_PASTE = 0x0302
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E


def _get_window_text(hwnd):
    """通过 WM_GETTEXT 读回窗口/控件当前文本（仅对经典控件有效，如记事本 EDIT、
    单行输入框等）。现代控件（Word 文档区 / 浏览器地址栏等）不支持时返回 None。
    ⚠️ 空文本必须返回 ""（而不是 None）：否则 post_text 读回验证会把「空框 +
    WM_CHAR 已注入成功」误判为「无法读回」而继续走 WM_PASTE，造成重复注入。"""
    try:
        hwnd = int(hwnd)
        if not hwnd:
            return None
        length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
        if length < 0:
            return None
        buf = ctypes.create_unicode_buffer(int(length) + 2)
        user32.SendMessageW(hwnd, WM_GETTEXT, len(buf), buf)
        return buf.value
    except Exception:
        return None


def post_text(hwnd, text):
    """【已退役，不再被注入链调用】通过 PostMessage(WM_CHAR) 投递文本。

    退役原因：WM_CHAR 对「有光标控件但读回不可靠」的现代控件（Excel 单元格 /
    Electron 输入框）会「已生效却误判失败」，随后 WM_PASTE 再注入一次 →
    同一文本出现两份（用户实测重复落字）。经典控件由 SendInput / WM_PASTE 覆盖，
    此函数保留仅供调试/诊断参考。失败返回 False。"""
    if not hwnd:
        return False
    try:
        hwnd = int(hwnd)
        pid = wt.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        tgt = hwnd
        has_caret = False
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndCaret:
            tgt = int(info.hwndCaret)
            has_caret = True
        if not has_caret:
            # ⚠️ 无光标控件(hwndCaret=0)：WM_CHAR 只能投到顶层窗口。现代控件
            # （Electron/Excel 等）可能把顶层 WM_CHAR 当作输入处理（落字但无法
            # 读回验证），也可能丢弃——两者都不可验证。若「已生效却继续走
            # WM_PASTE」，同一文本会被粘贴两次（用户实测：电子表格/输入框
            # 重复落字）。因此无可靠光标控件时直接放弃 WM_CHAR 路径，
            # 交由剪贴板/WM_PASTE 单次注入。
            logger.info("post_text: 无光标控件(hwndCaret=0)，跳过 WM_CHAR 避免重复落字")
            return False
        # 让目标窗口处于活动态，Electron 渲染进程才会处理投递的字符
        # ⚠️ 不要无条件 ShowWindow(SW_RESTORE)：会把最大化窗口还原变小，仅最小化时恢复
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        before = _get_window_text(tgt)
        ok_all = True
        for ch in text:
            r = user32.PostMessageW(tgt, WM_CHAR, ord(ch), 0)
            if not r:
                ok_all = False
            time.sleep(0.008)
        time.sleep(0.05)
        after = _get_window_text(tgt)
        if before is not None and after is not None:
            # 经典控件可读回：必须确认文字真的落入（内容变化且包含注入文本）
            landed = (after != before) and (text in after)
            logger.info("post_text: 已投递 %d 字符 读回验证 before=%r after=%r landed=%s",
                        len(text), before[-40:], after[-40:], landed)
            return landed
        # 现代控件读不回文本：不轻易判定成功，交回调用方走剪贴板/其他兜底
        logger.info("post_text: 目标控件无法读回文本（现代控件），保守返回 False 交兜底")
        return False
    except Exception as e:
        logger.error("post_text 异常: %s", e)
        return False


def _inject_paste(text, target_hwnd):
    """剪贴板注入（已退役，保留仅作后备参考；现由 _fallback_inject 统一接管）。
    原实现：复制文本 → 目标窗口置前台 →
    优先 SendInput(Ctrl+V)（Chromium/Electron 信任的键盘输入路径），
    失败再 SendMessage(WM_PASTE) 到焦点控件，最后 PostMessage 兜底。
    每一步都记录 GetLastError，便于定位「返回 0 但文本不入框」的根因。

    注意：SendInput 跨进程注入若返回 0 且 GetLastError=5(ACCESS_DENIED)，
    说明被 UIPI 拦截——此时需以「管理员身份」运行本程序，使其与目标进程
    处于同一（或更高）完整性级别。"""
    try:
        # 注入前先把浮窗藏起来，避免它抢占焦点/拦截键盘消息
        if app and getattr(app, "_visible", False):
            app._show_at(None)
            logger.info("_inject_paste: 浮窗已隐藏")

        import pyperclip
        old_clip = pyperclip.paste()
        pyperclip.copy(text)
        logger.info("_inject_paste: 剪贴板已写入 %d 字符", len(text))

        # 把目标窗口置为前台（恢复其可编辑子控件焦点）
        restore_focus(target_hwnd)
        time.sleep(0.1)

        # 解析真正接收输入的控件：焦点子窗口优先，其次插入符，最后顶层
        focus_tgt = int(target_hwnd)
        try:
            pid = wt.DWORD()
            tid = user32.GetWindowThreadProcessId(int(target_hwnd), ctypes.byref(pid))
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                if int(info.hwndFocus or 0):
                    focus_tgt = int(info.hwndFocus)
                elif int(info.hwndCaret or 0):
                    focus_tgt = int(info.hwndCaret)
        except Exception as e:
            logger.warning("_inject_paste: 解析焦点控件失败: %s", e)

        # 注入链（顺序由 VI_INJECT_MODE 控制）：
        #   unicode（默认）：KEYEVENTF_UNICODE 字符级注入——不经剪贴板、不经 Ctrl 组合键、
        #     不经过 IME 快捷键拦截，从机制上避免「只落一个 v」；
        #   paste：SendInput Ctrl+V（Chromium/Electron 最信任的键盘路径）。
        # 两类任一成功即返回；都不行再走 WM_PASTE / PostMessage 兜底。
        mode = os.environ.get("VI_INJECT_MODE", "unicode")
        order = [("unicode", lambda: type_unicode(text, target_hwnd)),
                 ("paste", lambda: send_ctrl_v(target_hwnd))]
        if mode == "paste":
            order.reverse()
        logger.info("_inject_paste: VI_INJECT_MODE=%s 注入链=%s",
                    mode, [n for n, _ in order])
        for name, fn in order:
            ok = fn()
            time.sleep(0.15)
            if ok:
                try:
                    pyperclip.copy(old_clip)
                except Exception:
                    pass
                logger.info("_inject_paste: 注入完成 ok=True (%s)", name)
                return True

        # 尝试 3：SendMessage WM_PASTE 到焦点控件（同步）
        r = user32.SendMessageW(focus_tgt, WM_PASTE, 0, 0)
        err = kernel32.GetLastError()
        logger.info("_inject_paste: WM_PASTE focus_tgt=%s 返回=%s GetLastError=%s",
                    focus_tgt, r, err)
        if int(r) != 0 and err != 5:
            time.sleep(0.15)
            try:
                pyperclip.copy(old_clip)
            except Exception:
                pass
            logger.info("_inject_paste: 注入完成 ok=True (WM_PASTE)")
            return True

        # 尝试 4：PostMessage WM_PASTE（异步兜底）
        pr = user32.PostMessageW(focus_tgt, WM_PASTE, 0, 0)
        err2 = kernel32.GetLastError()
        logger.info("_inject_paste: PostMessage WM_PASTE focus_tgt=%s 返回=%s GetLastError=%s",
                    focus_tgt, pr, err2)
        time.sleep(0.2)
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass
        ok = bool(pr) and err2 != 5
        logger.info("_inject_paste: 注入完成 ok=%s (PostMessage)", ok)
        return ok
    except Exception as e:
        logger.exception("_inject_paste 异常: %s", e)
        return False


def _paste_via_ctrlv(text, target_hwnd):
    """剪贴板注入：复制文本 → 目标窗口置前台 → SendInput(Ctrl+V)。
    SendInput 是系统级键盘输入，与手动 Ctrl+V 等效，对 Word/浏览器地址栏/
    Electron 等现代控件最可靠。成功（事件全部发送）即返回 True。
    注入后尽力恢复用户原剪贴板内容。"""
    if not text:
        return False
    import pyperclip
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    try:
        pyperclip.copy(text)
    except Exception as e:
        logger.error("_paste_via_ctrlv: 剪贴板写入失败: %s", e)
        return False
    logger.info("_paste_via_ctrlv: 剪贴板已写入 %d 字符", len(text))
    try:
        if target_hwnd:
            restore_focus(target_hwnd)
            time.sleep(0.1)
        if send_ctrl_v(target_hwnd):
            time.sleep(0.15)
            logger.info("_paste_via_ctrlv: Ctrl+V 注入成功")
            return True
        logger.warning("_paste_via_ctrlv: SendInput Ctrl+V 未全部发出，继续兜底")
        return False
    finally:
        try:
            if old is not None:
                pyperclip.copy(old)
        except Exception:
            pass


def _paste_via_message(text, target_hwnd):
    """WM_PASTE 直接投递到目标窗口的焦点控件（同步 SendMessage 优先，
    异步 PostMessage 兜底）。仅对响应 WM_PASTE 消息的控件有效。
    返回 True 表示事件已送达（尽力而为，不作读回强验证）。"""
    if not text:
        return False
    import pyperclip
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    try:
        pyperclip.copy(text)
    except Exception as e:
        logger.error("_paste_via_message: 剪贴板写入失败: %s", e)
        return False
    try:
        focus_tgt = int(target_hwnd)
        try:
            pid = wt.DWORD()
            tid = user32.GetWindowThreadProcessId(int(target_hwnd), ctypes.byref(pid))
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                if int(info.hwndFocus or 0):
                    focus_tgt = int(info.hwndFocus)
                elif int(info.hwndCaret or 0):
                    focus_tgt = int(info.hwndCaret)
        except Exception as e:
            logger.warning("_paste_via_message: 解析焦点控件失败: %s", e)
        r = user32.SendMessageW(focus_tgt, WM_PASTE, 0, 0)
        err = kernel32.GetLastError()
        time.sleep(0.15)
        if int(r) != 0 and err != 5:
            logger.info("_paste_via_message: WM_PASTE SendMessage 成功 focus_tgt=%s", focus_tgt)
            return True
        pr = user32.PostMessageW(focus_tgt, WM_PASTE, 0, 0)
        err2 = kernel32.GetLastError()
        time.sleep(0.2)
        ok = bool(pr) and err2 != 5
        logger.info("_paste_via_message: WM_PASTE SendMessage 返回=%s err=%s PostMessage=%s err=%s ok=%s",
                    r, err, pr, err2, ok)
        return ok
    finally:
        try:
            if old is not None:
                pyperclip.copy(old)
        except Exception:
            pass


# ---------- UI Automation 文本注入（根治方案） ----------
# 通过 UI Automation 的 TextPattern.InsertText 把文本插入到「任意支持 UIA 的编辑控件」
# （Word / WPS / Electron / Chrome / Edge / 浏览器地址栏 / 各类聊天输入框 / 富文本编辑器等），
# 不再依赖 SendInput / 剪贴板 / WM_PASTE —— 这些方式在「目标进程未提权(UIPI 拦截)」或
# 「现代文本控件不响应原始键盘/字符消息」时会被静默丢弃，导致「只在记事本落字」的 bug。
#
# 设计要点：
#   * 优先走 UIA；不支持 TextPattern 的控件（如经典 EDIT=记事本）本函数返回 False，
#     由调用方自动回退到原有 SendInput / 剪贴板 / WM_PASTE 链路（记事本等仍可用）。
#   * 通过 ElementFromHandle(target_hwnd) 取元素，再在自身/后代/祖先中查找 TextPattern
#     控件，完全不依赖「前台焦点」，因此对后台/最小化窗口也稳。
#   * InsertText 的 vtable 槽位在运行时从 typelib 推导（标准 Windows 自带），不写死；
#     主路径用 comtypes 高层调用，失败时退回推导槽位做原生 vtable 调用。
#   * COM 在每个调用线程内自行 STA 初始化，避免跨线程 COM 套间问题。

_UIMOD = None          # comtypes 生成的 UIAutomationCore 模块（缓存）
_UITEXT_IDX = None     # InsertText 的 vtable 槽位（运行时推导，缓存：int 可用 / False 不可用）


def _uia_get_module():
    global _UIMOD
    if _UIMOD is None:
        import comtypes.client
        # 与 uiautomation 库一致的可用实例化方式（本机/标准机均可用）
        _UIMOD = comtypes.client.GetModule("UIAutomationCore.dll")
    return _UIMOD


def _uia_inserttext_vtable_index():
    """从 typelib 推导 IUIAutomationTextRange.InsertText 的真实 vtable 槽位。

    标准 Windows 的 typelib 含 InsertText；本机精简版可能缺失，此时回退到
    微软标准固定布局。注意：IUIAutomationTextRange 的标准 vtable 布局为
    IUnknown(0-2) → Clone(3) → Compare(4) → CompareEndpoints(5) →
    ExpandToEnclosingUnit(6) → FindAttribute(7) → FindText(8) →
    GetAttributeValue(9) → GetBoundingRectangles(10) → GetChildren(11) →
    GetEnclosingElement(12) → GetText(13) → Move(14) → MoveEndpointByUnit(15) →
    MoveEndpointByRange(16) → Select(17) → AddToSelection(18) →
    RemoveFromSelection(19) → ScrollIntoView(20) → GetCaretRange(21) →
    GetSelection(22) → InsertText(23)。
    ⚠️ 历史坑：旧代码把兜底槽位写成 21（oVft=168），实际指向 GetCaretRange，
    在精简 typelib 机器上调用 GetCaretRange 返回 S_OK，造成 InsertText「假成功」
    ——文本未插入却被判定成功，正是「Word/浏览器不落字、只在记事本落字」的根因之一。"""
    global _UITEXT_IDX
    if _UITEXT_IDX is not None:
        return _UITEXT_IDX
    try:
        import comtypes.typeinfo as ti
        tlb = ti.LoadTypeLib(r"C:\Windows\System32\UIAutomationCore.dll")
        for i in range(tlb.GetTypeInfoCount()):
            info = tlb.GetTypeInfo(i)
            if info.GetDocumentation(-1)[0] != "IUIAutomationTextRange":
                continue
            tdesc = info.GetTypeAttr()
            for j in range(tdesc.cFuncs):
                fd = info.GetFuncDesc(j)
                if info.GetDocumentation(fd.memid)[0] == "InsertText":
                    _UITEXT_IDX = fd.oVft // 8
                    logger.debug("UIA InsertText vtable 槽位=%s", _UITEXT_IDX)
                    return _UITEXT_IDX
    except Exception as e:
        logger.debug("推导 InsertText vtable 槽位失败（本机 typelib 可能精简）: %s", e)
    # 精简 typelib 兜底：标准 Windows 8+ 的 IUIAutomationTextRange.InsertText
    # 固定位于槽位 23（oVft=184），紧随 GetSelection(槽位22) 之后。
    _UITEXT_IDX = 23
    logger.debug("UIA InsertText 使用标准固定槽位=23 (oVft=184, 精简 typelib 兜底)")
    return _UITEXT_IDX


def _uia_pattern_of(element):
    """若 element 支持 TextPattern(10014) 则返回 IUIAutomationTextPattern COM 指针，否则 None。"""
    mod = _uia_get_module()
    try:
        p = element.GetCurrentPattern(10014)
        if p:
            return p.QueryInterface(mod.IUIAutomationTextPattern)
    except Exception:
        pass
    return None


def _uia_find_text_pattern(automation, element):
    """在 element 自身 / 后代 / 祖先中查找支持 TextPattern 的控件，返回
    (IUIAutomationTextPattern COM 指针, 控件 element)；找不到返回 (None, None)。
    返回控件 element 是为了让调用方在 TextPattern 拿不到 Range 时，能从
    同一个控件上转取 ValuePattern 做后备注入。"""
    pat = _uia_pattern_of(element)
    if pat:
        return pat, element
    # 后代（RawViewWalker 深度优先，带访问去重与上限，防环）
    try:
        import ctypes
        walker = automation.RawViewWalker
        visited = set()
        stack = [element]
        guard = 0
        while stack and guard < 4000:
            guard += 1
            cur = stack.pop()
            try:
                key = ctypes.cast(cur, ctypes.c_void_p).value
                if key in visited:
                    continue
                visited.add(key)
            except Exception:
                pass
            pat = _uia_pattern_of(cur)
            if pat:
                return pat, cur
            try:
                child = walker.GetFirstChildElement(cur)
                sib = child
                while sib:
                    stack.append(sib)
                    sib = walker.GetNextSiblingElement(sib)
            except Exception:
                pass
    except Exception as e:
        logger.debug("UIA 后代遍历异常: %s", e)
    # 祖先
    try:
        parent = element.GetParentElement()
        depth = 0
        while parent and depth < 16:
            pat = _uia_pattern_of(parent)
            if pat:
                return pat, parent
            parent = parent.GetParentElement()
            depth += 1
    except Exception:
        pass
    return None, None


def _uia_insert_range(text_pattern):
    """返回用于插入的 TextRange：仅光标处（GetCaretRange / GetSelection 退化 range）。

    ⚠️ 不再退回 DocumentRange：它代表「整个文档」，InsertText 会插到文档开头，
    不是用户期望的光标位置（用户实测：本想插入文字中间，结果落到别处）。
    拿不到光标 Range 时返回 None，由调用方转 ValuePattern 或键盘兜底。
    所有返回值均校验 COM 指针有效性，防精简 typelib provider 返回坏指针。"""
    # 1) GetCaretRange（较新 typelib 位于 IUIAutomationTextPattern）
    try:
        res = text_pattern.GetCaretRange()
        rng = res[1] if isinstance(res, tuple) else res
        if _uia_ptr_valid(rng):
            return rng
    except Exception:
        pass
    # 2) GetSelection：光标（无选区时退化为光标处的退化 range）即插入点
    try:
        arr = text_pattern.GetSelection()
        if arr and getattr(arr, "Length", 0) and arr.Length > 0:
            rng = arr.GetElement(0)
            if _uia_ptr_valid(rng):
                return rng
    except Exception:
        pass
    return None


def _uia_ptr_valid(ptr):
    """校验 COM 接口指针有效性：非空、vtable 指针可读、不是无效值。

    精简 typelib 的 provider 可能返回坏指针（如 GetCaretRange 返回 BOOL 而非
    range），直接解引用会 access violation **导致整个进程崩溃**（ctypes 不捕获
    SEH）。因此用 IsBadReadPtr 先探测可读性，绝不在未经验证的地址上解引用。"""
    try:
        if not ptr:
            return False
        p = ctypes.cast(ptr, ctypes.c_void_p).value
        if not p:
            return False
        kernel32 = ctypes.windll.kernel32
        kernel32.IsBadReadPtr.restype = ctypes.c_int
        kernel32.IsBadReadPtr.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        # 前 16 字节可读（含 vtable 指针）
        if kernel32.IsBadReadPtr(p, 16):
            return False
        vt = ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0]
        if not vt or int(vt) == -1:
            return False
        # vtable 前 64 字节可读（接口方法区，含 InsertText 所在槽位）
        if kernel32.IsBadReadPtr(vt, 64):
            return False
        return True
    except Exception:
        return False


def _uia_do_insert(range_ptr, text):
    """调用 IUIAutomationTextRange.InsertText(text)。

    主路径：comtypes 高层（标准 Windows typelib 可用）。
    后备：本机精简 typelib 缺失 InsertText 方法时，走 vtable 槽位调用——
    但必须在 _uia_ptr_valid 校验（IsBadReadPtr）通过后才碰 vtable，
    并逐一检查槽位函数指针有效性，杜绝 access violation 崩溃
    （此前崩溃的根因是无校验地解引用 provider 返回的坏指针）。"""
    if not _uia_ptr_valid(range_ptr):
        logger.debug("UIA InsertText 跳过：Range 指针无效（精简 typelib provider）")
        return False
    try:
        range_ptr.InsertText(text)
        return True
    except AttributeError:
        pass  # 本机 typelib 未生成 InsertText 方法（精简）→ 走 vtable（已校验）
    except Exception as e:
        logger.debug("UIA comtypes InsertText 调用失败: %s", e)
        return False
    # vtable 槽位调用（指针已通过 IsBadReadPtr 校验）
    import ctypes
    try:
        idx = _uia_inserttext_vtable_index()
        if not idx:
            return False
        VTBL = ctypes.POINTER(ctypes.c_void_p)
        pp = ctypes.cast(range_ptr, VTBL)
        vt = pp[0]
        fn_ptr = ctypes.cast(vt, VTBL)[idx]
        if not fn_ptr or int(fn_ptr) == -1:
            logger.debug("UIA vtable 槽位 %s 无效（provider 未实现 InsertText），放弃", idx)
            return False
        fn = ctypes.cast(fn_ptr, ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p))
        this = ctypes.cast(range_ptr, ctypes.c_void_p).value
        oleaut32 = ctypes.windll.oleaut32
        oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
        oleaut32.SysAllocString.restype = ctypes.c_void_p
        bstr = oleaut32.SysAllocString(text)
        hr = fn(this, ctypes.c_void_p(bstr))
        logger.debug("UIA vtable InsertText hr=0x%08X", hr & 0xFFFFFFFF)
        return (hr & 0xFFFFFFFF) == 0
    except Exception as e:
        logger.debug("UIA vtable InsertText 失败: %s", e)
        return False


def _uia_set_value(element, text):
    """ValuePattern(10002) 后备注入：对输入框类控件直接设值。

    仅对可写控件（CurrentIsReadOnly=False）注入；**已有内容时追加到末尾
    （绝不清空原文）**——用户实测替换会删掉原有文字（记事本/文件/WorkBuddy
    全中招），不可接受；ValuePattern 无法定位光标，追加是保留原文的折中。"""
    if not text:
        return False
    mod = _uia_get_module()
    try:
        p = element.GetCurrentPattern(10002)
        if not p:
            logger.info("_uia_set_value: 目标控件不支持 ValuePattern")
            return False
        vp = p.QueryInterface(mod.IUIAutomationValuePattern)
        try:
            if vp.CurrentIsReadOnly:
                logger.info("_uia_set_value: 控件只读，拒绝注入")
                return False
        except Exception:
            pass
        try:
            old = vp.CurrentValue or ""
        except Exception:
            old = ""
        if old:
            logger.warning("_uia_set_value: 输入框已有内容(%d字)，追加到末尾（不清空原文）",
                           len(old))
            vp.SetValue(old + text)
            logger.info("_uia_set_value: ValuePattern 追加成功（%d 字）", len(text))
            return True
        vp.SetValue(text)
        logger.info("_uia_set_value: ValuePattern 注入成功（%d 字）", len(text))
        return True
    except Exception as e:
        logger.debug("_uia_set_value: ValuePattern 注入失败: %s", e)
        return False


def _inject_wmchar_electron(text, target_hwnd):
    """Electron/CEF 目标专用：PostMessage(WM_CHAR) 逐字符投递。

    背景：用户环境装有 360 安全卫士，其键盘防护拦截 SendInput/keybd_event
    （所有目标 LastError=0），但 **不拦截 WM_CHAR 消息投递**——v1 时代实测
    WorkBuddy（Electron）WM_CHAR 落字成功（当时的"重复落字"正是 WM_CHAR
    生效 + WM_PASTE 叠加造成的）。

    规则（吸取 v1 教训）：
    * 仅对窗口类名 Chrome_Widget_Win_1（Electron/CEF）的目标启用；
    * 投递到光标控件（hwndCaret）或顶层窗口（Electron 会路由到聚焦输入框）；
    * 投递即视为成功，**不再读回验证、不再叠加 WM_PASTE**（杜绝重复）。
    """
    if not text or not target_hwnd:
        return False
    try:
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(int(target_hwnd), cls, 64)
        # Electron/CEF 窗口类名：Chrome_WidgetWin_1（注意不是 Widget_Win）
        if cls.value not in ("Chrome_WidgetWin_1", "CefBrowserWindow"):
            return False
        pid = wt.DWORD()
        tid = user32.GetWindowThreadProcessId(int(target_hwnd), ctypes.byref(pid))
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        tgt = int(target_hwnd)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndCaret:
            tgt = int(info.hwndCaret)
        for ch in text:
            user32.PostMessageW(tgt, WM_CHAR, ord(ch), 0)
            time.sleep(0.01)
        logger.info("wmchar_electron: 已投递 %d 字符到 Electron 目标 (tgt=%s)",
                    len(text), tgt)
        return True
    except Exception as e:
        logger.debug("wmchar_electron 异常: %s", e)
        return False


def _inject_uia(text, target_hwnd):
    """UI Automation 文本注入（根治方案）。
    返回 True 表示已成功注入；返回 False 表示目标不支持 TextPattern / UIA 不可用，
    调用方应回退到原有 SendInput / 剪贴板 / WM_PASTE 链路。"""
    if not text:
        return False
    try:
        import comtypes.client
        mod = _uia_get_module()
        automation = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=mod.IUIAutomation,
        )

        # 注入前先把浮窗藏起来，避免抢占焦点（与 _inject_paste 一致）
        try:
            try:
                _app = app
            except NameError:
                _app = None
            if _app and getattr(_app, "_visible", False):
                _app.root.after(0, _app.root.withdraw)
                time.sleep(0.05)
        except Exception:
            pass

        # 取目标元素。策略：GetFocusedElement 优先——焦点通常就在目标输入框上
        # （on_press 已把焦点还给目标窗口），直接命中输入框元素，免去在巨大 UIA
        # 树里遍历（浏览器/Word 树可达数万节点，4000 上限经常找不到输入框）。
        # ElementFromHandle(顶层窗口) 仅作后备。
        element = None
        element_from_focus = False
        try:
            element = automation.GetFocusedElement()
            if element:
                element_from_focus = True
                logger.debug("UIA 取到焦点元素")
        except Exception as e:
            logger.debug("GetFocusedElement 失败: %s", e)
        if not element and target_hwnd:
            try:
                element = automation.ElementFromHandle(int(target_hwnd))
                logger.debug("UIA 取到目标窗口元素 (ElementFromHandle)")
            except Exception as e:
                logger.debug("ElementFromHandle 失败: %s", e)
        if not element:
            logger.info("_inject_uia: 未取得目标元素，回退原有链路")
            return False
        # ⚠️ 焦点元素若是不可编辑控件（静态文本/只读/禁用），拒绝注入——
        # 防止语音文本被插进系统提示等不可修改文本（用户实测反馈）。
        # 仅对焦点元素做类型过滤；ElementFromHandle 顶层元素仍继续遍历后代。
        if element_from_focus:
            try:
                if not element.CurrentIsEnabled:
                    logger.info("_inject_uia: 焦点元素禁用，拒绝注入")
                    return False
                ct = int(element.CurrentControlType)
                if ct not in (50004, 50030, 50003, 50034):  # Edit/Document/ComboBox/Spinner
                    logger.info("_inject_uia: 焦点元素类型 %s 不可编辑，拒绝注入", ct)
                    return False
            except Exception:
                pass

        text_pattern, ctrl_element = _uia_find_text_pattern(automation, element)
        rng = _uia_insert_range(text_pattern) if text_pattern else None
        if rng:
            ok = _uia_do_insert(rng, text)
            if ok:
                logger.info("_inject_uia: UIA TextPattern 注入成功（%d 字）", len(text))
            else:
                # InsertText 失败（精简 typelib 无此方法 / 目标不支持）→
                # 从同一控件转 ValuePattern 后备，再失败才回退键盘链
                logger.info("_inject_uia: TextPattern 注入失败，尝试 ValuePattern 后备")
                ok = _uia_set_value(ctrl_element, text) if ctrl_element is not None else False
                if not ok:
                    logger.warning("_inject_uia: ValuePattern 后备也失败，回退原有链路")
        elif ctrl_element is not None:
            # TextPattern 存在但拿不到有效 Range（Electron/浏览器输入框常见），
            # 或控件只有 ValuePattern —— 用 ValuePattern 后备注入
            logger.info("_inject_uia: TextPattern 无可用 Range，尝试 ValuePattern 后备")
            ok = _uia_set_value(ctrl_element, text)
        else:
            logger.info("_inject_uia: 目标不支持 TextPattern/ValuePattern，回退原有链路")
            ok = False
        if ok:
            # 注入后尽量把目标窗口提到前台，方便用户继续输入
            try:
                if target_hwnd:
                    restore_focus(target_hwnd)
            except Exception:
                pass
        return ok
    except Exception as e:
        logger.exception("_inject_uia 异常: %s", e)
        return False


def _inject_word_com(text, target_hwnd):
    """Word/WPS 文档区专用：COM 自动化 Selection.TypeText 在光标处插入文本。

    适用场景：UIA TextPattern 的 InsertText 在本机精简 typelib 下不可用（comtypes
    无此方法），ValuePattern 对 Word 文档区又不支持——COM 自动化是 Word 文档区
    最可靠的注入方式。仅当目标窗口是 Word/WPS/Excel 时才尝试。"""
    if not text or not target_hwnd:
        return False
    try:
        # 确认目标是 Word/WPS/Excel 窗口（类名识别），避免误触发
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(int(target_hwnd), cls, 64)
        cname = cls.value
        is_word = (cname.startswith("OpusApp") or cname.startswith("wps")
                   or cname.startswith("WPS") or cname == "XLMAIN")
        if not is_word:
            return False
        import comtypes.client
        app = comtypes.client.GetActiveObject("Word.Application")
        app.Selection.TypeText(text)
        logger.info("_inject_word_com: Word COM 注入成功（%d 字, 类名=%s）", len(text), cname)
        return True
    except Exception as e:
        logger.debug("_inject_word_com: 失败（可能无 Word 实例/不是 Word）: %s", e)
        return False


# 可注入的 UIA 控件类型（白名单）：编辑框 / 文档区 / 组合框 / 数值框。
# 静态文本(TextControl)、图片、按钮等一律不注入——防止越权插入只读/系统提示文本
# （用户实测：语音文本被插进了 WorkBuddy 对话框的不可修改提示文本）。
_UIACTRL_EDITABLE_TYPES = ("EditControl", "DocumentControl", "ComboBoxControl",
                           "SpinnerControl")


def _uiauto_editable(ctl):
    """uiautomation 控件是否「可编辑、可注入」：启用中 + 可编辑类型 + 非只读。

    只读判定双通道：ValuePattern.IsReadOnly + LegacyIAccessible.State 的
    STATE_SYSTEM_READONLY(0x40)——某些控件 provider 报 ValuePattern 可写，
    但 LegacyIAccessible 标记只读（如 WorkBuddy 系统提示文本），任一命中即拒绝。"""
    try:
        if not getattr(ctl, "IsEnabled", True):
            return False
        ctype = getattr(ctl, "ControlTypeName", "") or ""
        if ctype not in _UIACTRL_EDITABLE_TYPES:
            return False
        try:
            vp = ctl.GetValuePattern()
            if vp is not None and getattr(vp, "IsReadOnly", False):
                return False
        except Exception:
            pass
        # LegacyIAccessible 只读状态（STATE_SYSTEM_READONLY = 0x40）
        try:
            la = ctl.GetLegacyIAccessiblePattern()
            if la is not None:
                st = getattr(la, "State", 0) or 0
                if st & 0x40:
                    return False
        except Exception:
            pass
        return True
    except Exception:
        return False


def _uiauto_focus_in_window(focus_ctl, target_hwnd):
    """焦点控件是否属于目标窗口（顶层窗口句柄或进程 PID 对比）。
    防止焦点在别的窗口时误注入错误目标（实测事故：测试文本写进用户
    正在看的记事本）。"""
    try:
        if not target_hwnd:
            return False
        tl = focus_ctl.GetTopLevelControl()
        if tl is not None:
            if (tl.NativeWindowHandle or 0) == int(target_hwnd):
                return True
        # PID 对比兜底（Electron 窗口句柄可能漂移）
        pid_tgt = ctypes.c_ulong()
        pid_cur = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(int(target_hwnd), ctypes.byref(pid_tgt))
        if tl is not None:
            user32.GetWindowThreadProcessId(tl.NativeWindowHandle or 0, ctypes.byref(pid_cur))
            return pid_cur.value == pid_tgt.value
        return False
    except Exception:
        return False


def _inject_uia_uiauto(text, target_hwnd):
    """uiautomation 库实现的 UIA 注入（纯 ctypes，不依赖 typelib）。

    本机 comtypes 生成的 UIA 接口（依赖 UIAutomationCore.dll 的 typelib）与实际
    vtable 布局错位：调用 InsertText/ValuePattern.SetValue 会 access violation
    （实测 0xFFFFFFFFFFFFFFFF）。uiautomation 库用 ctypes 手写标准接口定义，
    不经过 typelib，实测可正常注入（记事本 ValuePattern 读回验证通过）。

    安全与位置约束（用户实测反馈修正）：
    * 只注入「可编辑」控件（启用 + Edit/Document/ComboBox 类型 + 非只读），
      静态文本/系统提示文本绝不动；
    * 插入位置只认「光标处」（GetCaretRange / GetSelection 退化 Range），
      不用 DocumentRange（会插到文档开头）；
    * ValuePattern 仅作最后手段：输入框为空时直接设值（无位置问题）；
      已有内容时追加到末尾并告警（该场景应走 TextPattern 光标插入）。
    返回 True=已注入；False=不可用/失败。"""
    try:
        import uiautomation as auto
    except ImportError:
        logger.info("uiauto: uiautomation 库不可用")
        return False
    try:
        # 1) 取控件：**焦点控件优先**——用户焦点所在即真实输入框
        #    （WorkBuddy 实测：32 个空 ComboBox 噪音，ControlFromHandle 遍历
        #    会选错；焦点控件的 ValuePattern.SetValue 实测有效落字）。
        #    ⚠️ 必须校验焦点控件**属于目标窗口**（GetTopLevelControl 或 PID 对比），
        #    否则用户此刻焦点在别的窗口（如正在看的记事本）会误注入（实测事故）。
        #    ControlFromHandle(目标窗口) 仅作后备。
        ctrl = None
        if target_hwnd:
            try:
                fc = auto.GetFocusedControl()
                if fc is not None and _uiauto_focus_in_window(fc, target_hwnd):
                    ctrl = fc
                    logger.debug("uiauto: 焦点控件命中（属于目标窗口 %s）", target_hwnd)
            except Exception as e:
                logger.debug("uiauto GetFocusedControl 失败: %s", e)
        if ctrl is None and target_hwnd:
            try:
                ctrl = auto.ControlFromHandle(int(target_hwnd))
                logger.debug("uiauto: 目标窗口控件取得 (ControlFromHandle)")
            except Exception as e:
                logger.debug("uiauto ControlFromHandle 失败: %s", e)
        if ctrl is None:
            try:
                ctrl = auto.GetFocusedControl()
                logger.debug("uiauto: 最终回退焦点控件")
            except Exception as e:
                logger.debug("uiauto GetFocusedControl 兜底失败: %s", e)
        if ctrl is None:
            logger.info("uiauto: 未取得控件")
            return False

        # 2) 收集自身/后代中所有「可编辑」候选控件（深度放宽到 20——
        #    WorkBuddy 真实输入框在深度 15 的 ComboBox，浅遍历会漏掉；
        #    提示文本区通常只读会被 _uiauto_editable 过滤）
        candidates = []

        def collect_inject_ctls(ctl, depth=0):
            if depth > 20 or len(candidates) > 20:
                return
            try:
                if _uiauto_editable(ctl):
                    candidates.append(ctl)
                    return  # 该控件自身可注入，不再深入其子树
            except Exception:
                pass
            try:
                for ch in ctl.GetChildren():
                    collect_inject_ctls(ch, depth + 1)
            except Exception:
                pass

        collect_inject_ctls(ctrl)
        if not candidates:
            logger.info("uiauto: 未找到可编辑控件（只读/静态文本不注入）")
            return False

        # 选择最优候选：空 Value（真输入框特征）优先，内容越长越像提示区越靠后
        def _uiauto_score(c):
            s = 0
            try:
                vp = c.GetValuePattern()
                if vp:
                    v = vp.Value or ""
                    if not v:
                        s += 100
                    else:
                        s -= len(v)
                if getattr(c, "IsKeyboardFocusable", False):
                    s += 10
            except Exception:
                pass
            return s

        inj_ctl = max(candidates, key=_uiauto_score)
        _vp0 = None
        try:
            _vp0 = inj_ctl.GetValuePattern()
        except Exception:
            _vp0 = None
        _vlen0 = len(_vp0.Value or "") if _vp0 is not None else -1
        logger.info("uiauto: 目标控件 %s 类型=%s ValueLen=%s",
                    inj_ctl.Name or "?", inj_ctl.ControlTypeName, _vlen0)
        # 3) ValuePattern 注入（uiautomation 库未封装 TextPattern.InsertText，
        #    唯一可靠通道是 ValuePattern.SetValue）：
        #    * 空输入框 → 直接设值
        #    * 已有内容 → **追加到末尾（绝不清空原文）**——用户实测替换会删掉
        #      原有文字（记事本/文件/WorkBuddy 全部中招），不可接受；追加保留
        #      原文，仅位置在末尾（日志注明）。
        #    * 只读控件 → 拒绝
        try:
            vp = inj_ctl.GetValuePattern()
            if vp:
                if getattr(vp, "IsReadOnly", False):
                    logger.info("uiauto: 控件只读(ValuePattern IsReadOnly)，拒绝注入")
                    return False
                try:
                    old = vp.Value or ""
                except Exception:
                    old = ""
                if old:
                    logger.warning("uiauto: 输入框已有内容(%d字)，追加到末尾（不清空原文）",
                                   len(old))
                    vp.SetValue(old + text)
                    logger.info("uiauto: ValuePattern 追加成功（%d 字）", len(text))
                    return True
                vp.SetValue(text)
                logger.info("uiauto: ValuePattern 注入成功（%d 字）", len(text))
                return True
        except Exception as e:
            logger.debug("uiauto ValuePattern 注入失败: %s", e)
        return False
    except Exception as e:
        logger.debug("_inject_uia_uiauto 异常: %s", e)
        return False


def _fallback_inject(text, target_hwnd):
    """UIA 失败后的兜底注入链（v14：恢复 v2 的完整链路 + 修复版 post_text）：
    SendInput Unicode → 剪贴板 Ctrl+V → WM_CHAR(修复版读回验证) → WM_PASTE。

    v14 说明：用户实测 v2 行为最好，唯一问题是电子表格重复落字。v2 的重复
    根因是 post_text 的读回验证在「空文本」场景失效（_get_window_text 空返回
    None → 误判未落字 → 继续 WM_PASTE → 双份）。本版 post_text 已修复：
    空文本返回 "" + 无光标控件跳过 + 读回确认才成功 → Excel 单次落字。
    经典控件（记事本/Excel）走 WM_CHAR 通道即可单次成功；现代控件（Electron
    等）由 WM_PASTE 兜底。"""
    # 先把目标窗口置前台并恢复可编辑子控件焦点（SendInput 只投递给前台窗口）
    restore_focus(target_hwnd)
    time.sleep(0.1)
    # 1) SendInput KEYEVENTF_UNICODE：真实键盘输入，不经剪贴板/IME。
    if type_unicode(text, target_hwnd):
        logger.info("_fallback_inject: SendInput Unicode 注入成功（%d 字）", len(text))
        return True
    logger.warning("_fallback_inject: SendInput Unicode 未全部发出，继续兜底")
    # 2) 剪贴板 + SendInput Ctrl+V：与手动粘贴等效。
    if _paste_via_ctrlv(text, target_hwnd):
        logger.info("_fallback_inject: 剪贴板 Ctrl+V 注入成功")
        return True
    # 3) WM_CHAR 直投（修复版：空文本可读回验证 + 无光标跳过；读回确认才成功，
    #    避免「已生效却误判失败 → 再走 WM_PASTE」的重复落字）
    if post_text(target_hwnd, text):
        logger.info("_fallback_inject: WM_CHAR 注入成功（读回验证通过）")
        return True
    # 4) WM_PASTE 直投（单次注入兜底）
    if _paste_via_message(text, target_hwnd):
        logger.info("_fallback_inject: WM_PASTE 注入成功")
        return True
    logger.error("_fallback_inject: 全部注入方式失败，文本未能送达：%r", text[:40])


def inject_text(text, mode="type", target_hwnd=None):
    if not text:
        return
    logger.info("inject_text: mode=%s target_hwnd=%s 文本长度=%d 预览=%r",
                mode, target_hwnd, len(text), text[:40])
    if mode == "kb":
        # 语音速记：先落盘当日速记，再照常填入光标
        append_to_kb(text, os.environ.get("VI_KB_DIR"))
    # 【v14：恢复 v2 的注入结构】优先 UIA（comtypes 版）；失败后进入多级兜底链
    # （SendInput → 剪贴板 Ctrl+V → WM_CHAR(修复版读回) → WM_PASTE）。
    # 注：v2 行为经用户实测最佳；Excel 重复问题由修复版 post_text 解决
    # （空文本可读回验证 + 无光标跳过 + 读回确认才成功）。
    if _inject_uia(text, target_hwnd):
        logger.info("inject_text: UIA 注入成功，结束（已跳过兜底链）")
        return
    logger.info("inject_text: UIA 不可用/失败，进入多级兜底链")
    # 兜底链调度到 tkinter 主线程执行：restore_focus 在主线程调用
    # SetForegroundWindow 更可靠，且 SendInput 必须由前台线程发出才被系统接受。
    try:
        _app = app
    except NameError:
        _app = None
    if _app is not None:
        done = threading.Event()

        def _run():
            try:
                _fallback_inject(text, target_hwnd)
            finally:
                done.set()

        _app.root.after(0, _run)
        done.wait(timeout=10)
    else:
        _fallback_inject(text, target_hwnd)


# 开头噪音清洗：按住图标瞬间的按键声/说话前气声常被 ASR 误识别成语气词
# （如「啊啊就大庆油田…」），从识别文本开头剥离语气词串及紧随的填充词。
_ASR_LEAD_NOISE = re.compile(r'^[啊嗯呃哎哦哈咦呀呐啦]+')
_ASR_FILLERS = ("就是", "那个", "这个", "然后", "就", "那")


def clean_asr_prefix(text):
    """清洗 ASR 结果开头的语气词/填充词噪音。仅在开头操作，句中内容不受影响。"""
    if not text:
        return text
    m = _ASR_LEAD_NOISE.match(text)
    if m:
        text = text[m.end():]
        # 剥完语气词后，紧跟的常见句首填充词也一并剥掉（需剩余内容够长，防误删）
        for w in _ASR_FILLERS:
            if text.startswith(w) and len(text) > len(w) + 1:
                text = text[len(w):]
                break
    return text


def append_to_kb(text, kb_dir=None):
    """语音速记：把一段话追加写入当日速记文件 (默认 脚本目录/voice_notes/YYYY-MM-DD.md)。"""
    if kb_dir is None:
        kb_dir = os.path.join(app_dir(), "voice_notes")
    os.makedirs(kb_dir, exist_ok=True)
    date = time.strftime("%Y-%m-%d")
    path = os.path.join(kb_dir, date + ".md")
    stamp = time.strftime("%H:%M:%S")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 语音速记 {date}\n\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- ({stamp}) {text}\n")
    return path


# ---------- 录音 ----------
import numpy as np
import sounddevice as sd


def _enhance_audio(data, sr):
    """录音增强（识别前预处理）：
    1) 静音裁剪：去掉按住图标瞬间的按键声 / 松开后的尾音，只留语音主体；
    2) 预加重：提升高频（辅音/声母信息），弱化低频混响与轰鸣，改善 ASR 可懂度；
    3) 不做全局增益（实测放大音量会连同环境噪音一起放大，适得其反）。
    仅当前后静音比例过大时做轻度增益兜底（防音量过低）。
    """
    x = data.astype(np.float64)

    # 1) 静音裁剪（20ms 能量窗，阈值 = max(300, 峰值*3%)）
    win = int(sr * 0.02)
    rms = np.array([
        np.sqrt((x[i:i + win] ** 2).mean()) if len(x[i:i + win]) else 0
        for i in range(0, len(x), win)
    ])
    thr = max(300.0, rms.max() * 0.03)
    idx = np.where(rms > thr)[0]
    if len(idx):
        s0 = max(0, int(idx[0] * win) - int(sr * 0.1))
        s1 = min(len(x), int((idx[-1] + 1) * win) + int(sr * 0.1))
        if s1 - s0 > sr * 0.3:  # 裁剪后至少保留 0.3s，防误剪
            x = x[s0:s1]

    # 2) 预加重（系数 0.95，温和）
    x = np.append(x[0], x[1:] - 0.95 * x[:-1])

    # 3) RMS 增益：语音整体音量不足时提升到目标 RMS（讯飞推荐约 -18dBFS）。
    #    必须按 RMS 而非峰值判断——个别瞬态峰值高但整体音量弱（如本次 tmp0：
    #    peak=20248 但 RMS=936）会导致漏增益，讯飞对低音量前段大量"脑补"乱码。
    rms_now = np.sqrt((x ** 2).mean())
    if 0 < rms_now < 3000:
        x = x * min(6.0, 4000.0 / rms_now)

    return np.clip(x, -32767, 32767).astype(np.int16)


class Recorder:
    def __init__(self, samplerate=16000):
        self.samplerate = samplerate
        self.frames = []
        self.stream = None
        self.recording = False

    def _callback(self, indata, frames, time_info, status):
        if self.recording:
            self.frames.append(indata.copy())

    def start(self):
        self.frames = []
        self.recording = True
        self.stream = sd.InputStream(
            samplerate=self.samplerate, channels=1,
            dtype="int16", callback=self._callback
        )
        self.stream.start()

    def stop(self):
        self.recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return self.frames

    def save_wav(self, path):
        if not self.frames:
            return None
        data = np.concatenate(self.frames, axis=0)
        data = _enhance_audio(data, self.samplerate)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.samplerate)
            wf.writeframes(data.tobytes())
        return path


# ---------- 浮窗 UI ----------
class FloatingApp:
    def __init__(self, transcriber, recorder, mode="type"):
        self.transcriber = transcriber
        self.recorder = recorder
        self.mode = mode
        self.recording = False

        # hover 视觉状态（必须在首次 draw_circle 之前初始化）
        self._icon_size = _ICON_SIZE       # 当前渲染尺寸（28 平时 / 34 hover）
        self._hover = False                # 鼠标是否悬停在图标上（<Enter>/<Leave>）
        self._mouse_down_on_icon = False   # 左键是否按在图标上（按住录音中）
        self._hover_visual = False         # 有效放大态（上述任一为 True 即放大）
        self._anim_tick = 0                # 录音动画帧计数

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # 显示改由 UpdateLayeredWindow（per-pixel alpha）全权接管：
        # 高透明红饼 + 全透明背景，不再使用 -transparentcolor / -alpha。
        # 初次启动位置：屏幕底端中间（水平居中，底部留 12px 边距）
        try:
            sw, sh = _screen_bounds()
            _ix = (sw - _ICON_SIZE) // 2
            _iy = sh - _ICON_SIZE - 12
        except Exception:
            _ix = _iy = 24
        self.root.geometry("28x28+%d+%d" % (_ix, _iy))

        self.canvas = tk.Canvas(self.root, width=28, height=28,
                                bg="SystemButtonFace", highlightthickness=0)
        self.canvas.pack()
        self.idle_color = "#e57373"   # 中红（平时：63% 不透明，清晰可辨）
        self.rec_color = "#d9534f"    # 深红（录音中：78% 不透明，更明显）
        # 获取真实顶层 HWND 并启用分层窗口
        self.root.update_idletasks()
        self.root.update()
        found = _find_own_visible_hwnd()
        self._hwnd = found[0] if found else int(self.root.winfo_id())
        _enable_layered(self._hwnd)
        self.draw_circle(self.idle_color)

        # 左键：按住图标说话，松开图标结束并识别
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        # 悬停：移入放大+更清晰提示可互动；移出/松开恢复原状
        self.canvas.bind("<Enter>", self.on_hover_enter)
        self.canvas.bind("<Leave>", self.on_hover_leave)
        # 右键单击弹菜单：配置 / 帮助 / 隐藏 / 退出
        self.canvas.bind("<Button-3>", self.on_right_click)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="配置", command=self._open_config)
        self.menu.add_command(label="帮助", command=self._show_help)
        self.menu.add_command(label="隐藏", command=self._hide_icon)
        self.menu.add_separator()
        self.menu.add_command(label="重启", command=self.restart)
        self.menu.add_command(label="退出", command=self.quit)

        self._visible = True    # 图标是否可见
        self._last_fg = 0       # 上次锚定的前台窗口（检测窗口切换用）
        self._anchor = None     # 无标准 caret 应用下的冻结位置 (x, y)
        self._mouse_down = False  # 鼠标左键是否处于按下状态（点击锚定用）
        self._target_hwnd = 0   # 按下时快照的目标窗口句柄

        # 自动跟随：无光标跟踪——定位只由鼠标左键长按触发（按住超阈值才锚定）
        self._mouse_listener = None    # 全局鼠标监听（长按即时锚定）
        self._press_xy = None          # 待判定的按下位置（快速单击会清空）
        self._press_t0 = 0.0           # 按下时刻（备用）
        self._config_win = None        # 配置对话框引用（防重复弹出）
        self._help_win = None          # 帮助窗口引用（防重复弹出）
        self._win_icon_img = None      # 窗口图标 PhotoImage 引用（防 GC）
        self.root.after(300, self._follow_click)
        self._install_mouse_hook()

    def draw_circle(self, color, wave=None):
        """重绘悬浮窗：idle 态显示 AVI_logo28.png（PNG 缩放渲染，RGBA 透明底）；
        录音/提示态保留原红色圆饼+麦克风绘制（wave 为录音波形，None 不画）。
        透明度：平时 160、录音中 200（比原来更清晰）；hover 时再 +50。
        尺寸由 _set_hover_state 维护（28 平时 / 34 hover）；
        话筒颜色：平时暖白米色，hover 纯白。"""
        hover = self._hover_visual
        base = _ALPHA_REC if color == self.rec_color else _ALPHA_IDLE
        alpha = min(255, base + (_ALPHA_HOVER_BOOST if hover else 0))
        mic = MIC_WHITE if hover else MIC_GRAY
        if color == self.idle_color and wave is None:
            # idle：显示 AVI_logo28.png（SourceConstantAlpha=alpha 控制整体不透明度，
            # 与旧红饼透明度语义一致：平时 160 / hover 210）
            _apply_logo_layered(self._hwnd, self._icon_size, alpha, self.idle_color)
            return
        _apply_layered(self._hwnd, color, alpha, self._icon_size, mic, wave)

    def _set_hover_state(self, on):
        """hover 视觉开关：on=True 放大到 34（保持中心）+ 更清晰；False 恢复 28。
        仅在状态变化时重设窗口/canvas 尺寸，避免每帧重复布局。"""
        if on == self._hover_visual:
            return
        self._hover_visual = on
        size = _ICON_SIZE_HOVER if on else _ICON_SIZE
        self._icon_size = size
        try:
            # 保持中心不变：新左上角 = 旧中心 - 新尺寸/2
            rect = RECT()
            if user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                x, y = cx - size // 2, cy - size // 2
                self.root.geometry(f"{size}x{size}+{x}+{y}")
                self.canvas.configure(width=size, height=size)
        except Exception as e:
            logger.debug("_set_hover_state 异常: %s", e)
        self.draw_circle(self.rec_color if self.recording else self.idle_color)

    def on_hover_enter(self, e):
        """鼠标移入图标：放大 + 更清晰，提示可互动。"""
        self._hover = True
        self._set_hover_state(True)

    def on_hover_leave(self, e):
        """鼠标移出图标：恢复原状。"""
        self._hover = False
        self._set_hover_state(self._mouse_down_on_icon)

    def _snapshot_target(self):
        """快照目标输入窗口（鼠标按住浮窗 / 全局热键两条路径共用）。
        目标 = 你开始说话"之前"正在输入的窗口。
        优先级：钩子记录的上一个真实前台窗口 > 当前前台(排除本程序浮窗)。
        ⚠️ 按下浮窗图标那一刻，前台窗口恰恰是这个浮窗自己，绝不能把它当目标
        （否则文字会送回浮窗自身而消失）。"""
        lf = last_foreground.get("hwnd", 0)
        fg = user32.GetForegroundWindow()
        fg_int = int(fg) if fg else 0
        if fg_int and not _is_our_window(fg_int):
            self._target_hwnd = fg_int
        else:
            self._target_hwnd = lf or fg_int
        logger.info("快照目标窗口 hwnd=%s (lf=%s, fg=%s, fg_is_ours=%s)",
                    self._target_hwnd, lf, fg_int, _is_our_window(fg_int))
        return self._target_hwnd

    def on_press(self, e):
        logger.info("on_press: 按住图标，开始录音")
        # 背景已透明：仅当点击落在圆圈内（圆心=当前尺寸中心，半径 11 按比例）
        # 才响应，避免点到透明角落误触发录音
        c = self._icon_size / 2.0
        r = 11 * self._icon_size / _ICON_SIZE
        dx, dy = e.x - c, e.y - c
        if dx * dx + dy * dy > r * r:
            return
        self._mouse_down_on_icon = True
        self._set_hover_state(True)
        if not self.recording:
            self._snapshot_target()
            # 按住瞬间把焦点还给目标窗口，避免后续注入时焦点还在浮窗
            if self._target_hwnd and not _is_our_window(self._target_hwnd):
                restore_focus(self._target_hwnd)
            self.start_rec()

    def on_release(self, e):
        logger.info("on_release: 松开图标，停止录音并尝试识别")
        self._mouse_down_on_icon = False
        # 松开即恢复原状；若鼠标仍停在图标上，<Enter> 悬停态已生效，
        # 之后移出由 <Leave> 恢复，不会冲突。
        self._set_hover_state(False)
        if self.recording:
            self.stop_rec()

    def on_right_click(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def _fit_to_screen(self, win, text_widget=None):
        """把弹窗完整显示在屏幕工作区内（不含任务栏）：
        1) 高度超屏时先压缩文本控件行数；
        2) 仍超屏则把窗口收缩到工作区尺寸（可调整大小）；
        3) 最后居中于主浮窗附近并做边界夹取。"""
        try:
            win.update_idletasks()
            wa_l, wa_t, wa_r, wa_b = _work_area()
            wa_w, wa_h = wa_r - wa_l, wa_b - wa_t
            ww, wh = win.winfo_width(), win.winfo_height()
            # 高度超出工作区：优先压缩文本控件行数
            if text_widget is not None and wh > wa_h:
                try:
                    rows = int(text_widget.cget("height"))
                    per_row = text_widget.winfo_height() / max(1, rows)
                    new_rows = max(4, int(rows - (wh - wa_h) / max(1, per_row)))
                    if new_rows < rows:
                        text_widget.config(height=new_rows)
                        win.update_idletasks()
                        ww, wh = win.winfo_width(), win.winfo_height()
                except Exception:
                    pass
            # 仍超屏：收缩窗口到工作区尺寸
            if ww > wa_w or wh > wa_h:
                win.resizable(True, True)
                win.minsize(0, 0)
                win.geometry("%dx%d" % (min(ww, wa_w), min(wh, wa_h)))
                win.update_idletasks()
                ww, wh = win.winfo_width(), win.winfo_height()
            # 居中于主浮窗附近 + 边界夹取到工作区（保证不延伸出屏幕/任务栏）
            cx = self.root.winfo_rootx() + (self.root.winfo_width() - ww) // 2
            cy = self.root.winfo_rooty() + (self.root.winfo_height() - wh) // 2
            x = max(wa_l, min(cx, wa_r - ww))
            y = max(wa_t, min(cy, wa_b - wh))
            win.geometry("+%d+%d" % (x, y))
        except Exception as e:
            logger.debug("_fit_to_screen 异常: %s", e)

    def _set_win_icon(self, win):
        """给配置/帮助等 Toplevel 窗口设置左上角图标（AVI_logo28.png）。
        iconphoto 使用 Tk 内置 PhotoImage（支持 PNG）；引用须保存在 self 上防 GC。"""
        try:
            icon_path = resource_path("AVI_logo28.png")
            if not os.path.exists(icon_path):
                logger.debug("窗口图标缺失，跳过: %s", icon_path)
                return
            img = tk.PhotoImage(file=icon_path)
            self._win_icon_img = img          # 防 GC（Tk 引用必须存活）
            win.iconphoto(True, img)
            logger.debug("窗口图标已设置: %s", icon_path)
        except Exception as e:
            logger.debug("设置窗口图标失败: %s", e)

    def _open_config(self):
        """菜单-配置：弹出配置对话框，编辑讯飞三个凭证参数并保存到 xfyun_config.ini。"""
        try:
            self._show_config_dialog()
        except Exception as e:
            logger.error("打开配置对话框失败: %s", e)
            self._err(str(e))

    def _show_config_dialog(self):
        """讯飞凭证配置对话框：标题「阿色语音快捷输入法-讯飞版」。
        可填写 app_id / api_key / api_secret，点击网址可打开申请页面，保存写回配置文件。"""
        import webbrowser
        try:
            from xfyun_asr import load_xfyun_config, _CONFIG_PATH
        except Exception:
            return

        cur_aid, cur_key, cur_sec = load_xfyun_config()
        TITLE = "阿色语音快捷输入法-讯飞版"

        # 防重复：已有配置窗口时只聚焦，不新建
        if self._config_win is not None:
            try:
                if self._config_win.winfo_exists():
                    self._config_win.lift()
                    self._config_win.focus_force()
                    return
            except Exception:
                self._config_win = None

        win = tk.Toplevel(self.root)
        self._config_win = win
        win.title(TITLE)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._set_win_icon(win)

        def close():
            self._config_win = None
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", close)

        # 第一行：产品标题；第二行：版本号（小字）；第三行：功能副标题
        tk.Label(win, text=TITLE, font=("", 13, "bold")).pack(pady=(14, 0))
        tk.Label(win, text=APP_VERSION, font=("", 9)).pack(pady=(0, 2))
        tk.Label(win, text="讯飞语音听写 (iat) 配置",
                 font=("", 10)).pack(pady=(0, 6))
        # 申请网址（换行两行显示，均可点击）
        for link_text in ("免费获取密钥：https://www.xfyun.cn",
                          "控制台→创建应用→语音听写iat"):
            link = tk.Label(win, text=link_text, fg="#0a66c2", cursor="hand2")
            link.pack(pady=(0, 0))
            link.bind("<Button-1>", lambda e: webbrowser.open("https://www.xfyun.cn"))

        fields = [
            ("APPID", "app_id", cur_aid),
            ("APIKey", "api_key", cur_key),
            ("APISecret", "api_secret", cur_sec),
        ]
        vars_ = {}
        for label, key, val in fields:
            row = tk.Frame(win)
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(row, text=label, width=10, anchor="w").pack(side="left")
            v = tk.StringVar(value=val or "")
            vars_[key] = v
            tk.Entry(row, textvariable=v, width=34).pack(side="left", fill="x", expand=True)

        # 开机自动启动（HKCU Run 键，勾选即生效）
        auto_row = tk.Frame(win)
        auto_row.pack(fill="x", padx=18, pady=(10, 0))
        auto_var = tk.BooleanVar(value=_autostart_enabled())

        def toggle_autostart():
            try:
                _set_autostart(auto_var.get())
            except Exception as e:
                logger.error("设置开机自启动失败: %s", e)
                auto_var.set(not auto_var.get())
                tk.messagebox.showerror(TITLE, "设置开机自启动失败：%s" % e)

        tk.Checkbutton(auto_row, text="开机自动启动", variable=auto_var,
                       command=toggle_autostart).pack(side="left")
        tk.Label(auto_row, text="（勾选后开机自动运行，无需手动启动）",
                 fg="#888888").pack(side="left")

        def save():
            try:
                app_id = vars_["app_id"].get().strip()
                api_key = vars_["api_key"].get().strip()
                api_secret = vars_["api_secret"].get().strip()
                if not (app_id and api_key and api_secret):
                    tk.messagebox.showwarning(TITLE, "三项凭证均不能为空！")
                    return
                header = (
                    "; 讯飞开放平台 语音听写(iat) 凭证配置文件\n"
                    "; 免费申请：https://www.xfyun.cn 控制台 - 创建应用 - 语音听写iat\n"
                    "; 程序启动时会自动读取本文件（同目录），无需再配置环境变量。\n"
                    "[xfyun]\n"
                    "app_id = %s\n"
                    "api_key = %s\n"
                    "api_secret = %s\n" % (app_id, api_key, api_secret)
                )
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(header)
                close()
                tk.messagebox.showinfo(TITLE, "配置已保存到 xfyun_config.ini\n重启程序后生效。")
            except Exception as e:
                logger.error("保存配置失败: %s", e)
                tk.messagebox.showerror(TITLE, "保存失败：%s" % e)

        btns = tk.Frame(win)
        btns.pack(pady=(10, 4))
        tk.Button(btns, text="保存", width=10, command=save).pack(side="left", padx=8)
        tk.Button(btns, text="取消", width=10, command=close).pack(side="left", padx=8)

        # GitHub 仓库（可点击打开 https://github.com/arthurqwang/ArthurVoiceInput）
        gh = tk.Label(win, text="GitHub：github.com/arthurqwang/ArthurVoiceInput",
                      fg="#0a66c2", cursor="hand2")
        gh.pack(pady=(0, 0))
        gh.bind("<Button-1>",
                lambda e: webbrowser.open("https://github.com/arthurqwang/ArthurVoiceInput"))

        # 最底端：作者与官网（可点击打开 https://www.holomind.com.cn）
        foot = tk.Label(win, text="作者：阿色  大系统观官网：www.holomind.com.cn",
                        fg="#555555", cursor="hand2")
        foot.pack(pady=(2, 10))
        foot.bind("<Button-1>",
                  lambda e: webbrowser.open("https://www.holomind.com.cn"))

        # 完整显示在屏幕内（超出则自动缩小），居中于主浮窗附近
        self._fit_to_screen(win)

    def _show_help(self):
        """菜单-帮助：弹出帮助窗口（样式与配置窗口一致）。"""
        try:
            self._show_help_dialog()
        except Exception as e:
            logger.error("打开帮助窗口失败: %s", e)
            self._err(str(e))

    def _show_help_dialog(self):
        """帮助窗口：标题/版本号/使用说明/申请密钥说明/作者信息。"""
        TITLE = "阿色语音快捷输入法-讯飞版"

        # 防重复：已有帮助窗口时只聚焦，不新建
        if self._help_win is not None:
            try:
                if self._help_win.winfo_exists():
                    self._help_win.lift()
                    self._help_win.focus_force()
                    return
            except Exception:
                self._help_win = None

        win = tk.Toplevel(self.root)
        self._help_win = win
        win.title(TITLE)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._set_win_icon(win)

        def close():
            self._help_win = None
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", close)

        tk.Label(win, text=TITLE, font=("", 13, "bold")).pack(pady=(14, 0))
        tk.Label(win, text=APP_VERSION, font=("", 9)).pack(pady=(0, 2))
        tk.Label(win, text="使用说明", font=("", 10)).pack(pady=(0, 6))

        help_text = (
            "【使用方法】\n"
            "1. 在欲输入位置长按鼠标左键（约 0.3 秒），\n"
            "   把输入图标（淡红色麦克风）呼唤出来；\n"
            "2. 按住图标不松手 → 开始录音说话；\n"
            "3. 说完了松手 → 自动识别并输出到当前光标；\n"
            "4. 隐藏后，长按鼠标左键，可再次把输入图标\n"
            "   呼唤出来；\n"
            "5. 快捷热键：按住 Ctrl+Alt+Space 说话，\n"
            "   松手同样识别输出。\n\n"
            "【首次使用】\n"
            "需先到科大讯飞开放平台申请密钥：\n"
            "   https://www.xfyun.cn\n"
            "   控制台 → 创建应用 → 语音听写 iat\n"
            "获取 APPID / APIKey / APISecret 三项密钥后，\n"
            "右键“配置”填入并保存，重启程序即可生效。\n"
            "每日可免费使用500次，每次最长一分钟。\n\n"
            "【环境要求】\n"
            "电脑需连接麦克风，并保持网络正常。"
        )
        txt = tk.Text(win, width=48, height=17, font=("", 10),
                      wrap="word", relief="solid", bd=1)
        txt.pack(padx=14, pady=(0, 6))
        txt.insert("1.0", help_text)
        txt.config(state="disabled")

        btns = tk.Frame(win)
        btns.pack(pady=(0, 4))
        tk.Button(btns, text="知道了", width=12, command=close).pack()

        # GitHub 仓库（可点击打开 https://github.com/arthurqwang/ArthurVoiceInput）
        gh = tk.Label(win, text="GitHub：github.com/arthurqwang/ArthurVoiceInput",
                      fg="#0a66c2", cursor="hand2")
        gh.pack(pady=(0, 0))
        gh.bind("<Button-1>",
                lambda e: webbrowser.open("https://github.com/arthurqwang/ArthurVoiceInput"))

        # 最底端：作者与官网（可点击打开 https://www.holomind.com.cn）
        import webbrowser
        foot = tk.Label(win, text="作者：阿色  大系统观官网：www.holomind.com.cn",
                        fg="#555555", cursor="hand2")
        foot.pack(pady=(2, 10))
        foot.bind("<Button-1>",
                  lambda e: webbrowser.open("https://www.holomind.com.cn"))

        # 完整显示在屏幕内（高度超屏先压缩文本行数），居中于主浮窗附近
        self._fit_to_screen(win, txt)

    def _hide_icon(self):
        """菜单-隐藏：撤走浮窗；长按鼠标左键任意位置可召唤回来。"""
        logger.info("隐藏浮窗（长按左键可召唤）")
        self._visible = False
        self.root.withdraw()

    def _install_mouse_hook(self):
        """全局鼠标监听：左键按下时启动长按计时（按住超过 _PRESS_MS 才锚定），
        快速单击（如点网页链接）不触发跟位。"""
        try:
            from pynput import mouse

            def on_click(x, y, button, pressed):
                try:
                    if button != mouse.Button.left:
                        return
                    if pressed:
                        if self._point_in_icon((x, y)):
                            return          # 点的是浮窗自己（录音），跳过
                        self.root.after(0, lambda: self._on_press_possible(x, y))
                    else:
                        self._press_xy = None   # 松开：取消长按判定
                except Exception:
                    pass

            listener = mouse.Listener(on_click=on_click)
            listener.daemon = True
            listener.start()
            self._mouse_listener = listener
            logger.debug("全局鼠标钩子已安装（长按 %dms 才跟位）", _PRESS_MS)
        except Exception as e:
            logger.debug("鼠标钩子安装失败: %s", e)

    def _on_press_possible(self, x, y):
        """左键按下入口：记录按下位置，安排长按检查（按住超阈值才锚定）。
        浮窗隐藏时不拦截（长按任意位置用于召唤）。"""
        if self.recording or (self._visible and self._point_in_icon((x, y))):
            return
        self._press_xy = (x, y)
        self._press_t0 = time.time()
        self.root.after(_PRESS_MS + 30, self._check_press_anchor)

    def _check_press_anchor(self):
        """长按判定：此刻左键仍按住 且 未明显移动（非拖选文本）→ 锚定到按下位置。
        若浮窗处于隐藏状态，则长按任意位置把它召唤回来。"""
        try:
            if self.recording or self._press_xy is None:
                return
            if not self._mouse_pressed_now():
                return                      # 已松开（快速单击），不跟位
            # 排除拖选文本：按住期间鼠标位移超过阈值 → 视为在选文本，不跟位
            cur = _cursor_pos_once()
            if cur:
                px, py = self._press_xy
                dx, dy = cur[0] - px, cur[1] - py
                if dx * dx + dy * dy > _PRESS_MOVE_PX * _PRESS_MOVE_PX:
                    logger.debug("长按判定: 位移 %dpx 视为拖选文本，不跟位",
                                 int((dx * dx + dy * dy) ** 0.5))
                    return
            x, y = self._press_xy
            if not self._visible:
                # 隐藏态长按 → 召唤浮窗到长按点下方
                logger.info("长按召唤浮窗 -> (%d, %d)", x, y)
                self._visible = True
                self.root.deiconify()
                # withdraw 后分层窗口表面可能丢失，重新绘制
                self.draw_circle(self.rec_color if self.recording else self.idle_color)
                pos = _place_below_rect(x, y, x + 2, y + 18)
                if pos:
                    self._anchor = pos
                    self._show_at(pos)
                return
            self._anchor_at(x, y)
        except Exception as e:
            logger.debug("_check_press_anchor 异常: %s", e)

    def _mouse_pressed_now(self):
        """查询左键此刻是否处于按住状态。"""
        try:
            user32.GetAsyncKeyState.argtypes = [wt.INT]
            user32.GetAsyncKeyState.restype = wt.SHORT
            return bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return False

    def _anchor_at(self, x, y):
        """把图标锚定到鼠标点击处正下方。定位只跟随鼠标左键点击，不跟踪光标。"""
        try:
            if self.recording or self._point_in_icon((x, y)):
                return
            pos = _place_below_rect(x, y, x + 2, y + 18)
            if pos:
                self._anchor = pos
                self._show_at(pos)
                logger.debug("anchor: 单击即时锚定 -> %s", pos)
        except Exception as e:
            logger.debug("_anchor_at 异常: %s", e)

    def _show_at(self, pos):
        """把图标移动到 pos=(x,y)（28 语义，等效 28x28 窗口的左上角）；
        pos 为 None 时隐藏（撤回屏幕）。
        若当前窗口已放大（hover），按尺寸差向左上偏移以保持视觉中心。"""
        if pos:
            x, y = pos
            if self._icon_size != _ICON_SIZE:
                d = (self._icon_size - _ICON_SIZE) // 2
                x, y = x - d, y - d
            self.root.geometry(f"+{x}+{y}")
            if not self._visible:
                self.root.deiconify()
                self._visible = True
                # withdraw 后分层窗口表面可能丢失，重新绘制
                self.draw_circle(self.rec_color if self.recording else self.idle_color)
        else:
            if self._visible:
                self.root.withdraw()
                self._visible = False

    def _point_in_icon(self, pt):
        """鼠标点是否落在悬浮窗自身范围内（点击自身时忽略，防止自移位）。"""
        if not pt or not self._anchor:
            return False
        x, y = pt
        ax, ay = self._anchor
        return ax <= x <= ax + 28 and ay <= y <= ay + 28

    def _follow_click(self):
        """轮询后备：检测鼠标左键按住超过阈值才锚定（主路径是全局鼠标钩子）。
        快速单击（< _PRESS_MS）不跟位。录音中冻结。"""
        try:
            if not self.recording:
                down = self._mouse_pressed_now()
                if down and not self._mouse_down:
                    self._mouse_down = True
                    cp = _cursor_pos_once()
                    if cp and not self._point_in_icon(cp):
                        self._on_press_possible(cp[0], cp[1])
                elif not down:
                    self._mouse_down = False
        except Exception as e:
            logger.debug("follow_click 异常: %s", e)
        finally:
            self.root.after(300, self._follow_click)

    def toggle(self):
        if not self.recording:
            self.start_rec()
        else:
            self.stop_rec()

    def start_rec(self):
        # 防御：目标窗口缺失时（全局热键路径可能未走 on_press 快照）补一次快照，
        # 避免注入落到 0/旧目标窗口导致文字丢失
        if not getattr(self, "_target_hwnd", 0):
            self._snapshot_target()
        self.recording = True
        self._anim_tick = 0
        self.draw_circle(self.rec_color)
        self._anim_step()                 # 启动录音动画（波形跳动+话筒闪烁）
        logger.info("start_rec: 开始录音")
        try:
            self.recorder.start()
            logger.info("start_rec: 录音流已启动")
        except Exception as ex:
            self.recording = False
            self.draw_circle(self.idle_color)
            self._err(str(ex))

    def _anim_step(self):
        """录音动画帧：波形条正弦跳动 + 话筒深灰/白交替闪烁。
        每 _ANIM_MS 重绘一帧；recording 置 False 后下一帧自动停止。"""
        if not self.recording:
            return
        self._anim_tick += 1
        t = self._anim_tick
        # 三条波形高度 2~7px（相位错开的正弦），0 由渲染端忽略
        wave = [max(0, int(2 + 5 * abs(math.sin(t * 0.5 + i * 1.1)))) for i in range(3)]
        # 话筒闪烁：每 2 帧（240ms）纯白/深酒红交替
        mic = MIC_WHITE if (t // 2) % 2 == 0 else MIC_DIM
        hover = self._hover_visual
        alpha = min(255, _ALPHA_REC + (_ALPHA_HOVER_BOOST if hover else 0))
        _apply_layered(self._hwnd, self.rec_color, alpha, self._icon_size, mic, wave)
        self.root.after(_ANIM_MS, self._anim_step)

    def stop_rec(self):
        self.recording = False
        self.draw_circle(self.idle_color)
        logger.info("stop_rec: 停止录音")
        frames = self.recorder.stop()
        logger.info("stop_rec: 采集到 %d 帧", len(frames) if frames else 0)
        path = os.path.join(tempfile.gettempdir(), "voice_input_tmp.wav")
        if not self.recorder.save_wav(path):
            logger.warning("stop_rec: 无音频数据，未生成 wav")
            return
        logger.info("stop_rec: wav 已保存 %s，启动后台识别", path)
        threading.Thread(target=self._transcribe_and_inject,
                         args=(path,), daemon=True).start()

    def _transcribe_and_inject(self, path):
        logger.info("_transcribe_and_inject: 进入后台识别线程")
        try:
            text = self.transcriber.transcribe(path)
            text = clean_asr_prefix(text)
            if text:
                logger.info("_transcribe_and_inject: 识别到文本，准备注入")
                # 注入前先把浮窗藏起来，避免它抢焦点导致文字送不到目标窗口
                self.root.after(0, self.root.withdraw)
                self._visible = False
                inject_text(text, self.mode, target_hwnd=self._target_hwnd)
                # 注入完成后再显示浮窗
                self.root.after(0, self._reveal)
            else:
                logger.warning("_transcribe_and_inject: 识别结果为空")
                # 非阻塞提示：图标闪一下黄即可，不再弹模态对话框
                # （避免误触/安静环境下被反复打断；真实异常仍走 _err 弹窗）
                self._flash("未识别到语音")
        except Exception as ex:
            self._err(str(ex))

    def _reveal(self):
        """注入完成后重新显示浮窗（回到上次点击锚定的位置）。"""
        try:
            self.root.deiconify()
            self._visible = True
            # 回到锚点（定位只跟随鼠标点击，不跟踪光标）
            if self._anchor:
                self._show_at(self._anchor)
        except Exception as e:
            logger.debug("_reveal 异常: %s", e)

    def _err(self, msg):
        logger.error("错误: %s", msg)
        self.root.after(0, lambda: self._show_err(msg))

    def _show_err(self, msg):
        self.draw_circle("#e0a800")  # 黄色提示
        try:
            tk.messagebox.showerror("语音输入法", msg)
        except Exception:
            pass

    def _flash(self, msg=None):
        """非阻塞提示：图标闪一下黄色再恢复，不弹模态对话框。"""
        logger.info("flash: %s", msg or "")
        try:
            self.root.after(0, self._do_flash)
        except Exception:
            pass

    def _do_flash(self):
        self.draw_circle("#e0a800")
        self.root.after(1500, lambda: self.draw_circle(self.idle_color))
        self.root.after(2500, lambda: self.draw_circle(self.idle_color))

    def _check_config_and_prompt(self):
        """启动检查：配置文件缺失或 APPID/APIKey/APISecret 未填写时，
        提醒用户到讯飞申请密钥，并自动弹出配置窗口。"""
        try:
            from xfyun_asr import load_xfyun_config, _CONFIG_PATH
            missing = not os.path.exists(_CONFIG_PATH)
            if not missing:
                _a, _k, _s = load_xfyun_config()
                missing = not (_a and _k and _s)
            if missing:
                logger.warning("讯飞配置缺失，提醒并弹出配置窗口")
                tk.messagebox.showwarning(
                    "阿色语音快捷输入法-讯飞版",
                    "尚未配置讯飞密钥。\n\n"
                    "请先到科大讯飞开放平台（免费）申请：\n"
                    "   https://www.xfyun.cn\n"
                    "   控制台 → 创建应用 → 语音听写 iat\n\n"
                    "获取 APPID / APIKey / APISecret 三项密钥后\n"
                    "在配置窗口中填写并保存，重启程序即可生效。"
                )
                self._open_config()
        except Exception as e:
            logger.debug("配置检查异常: %s", e)

    def restart(self):
        """重启程序：先启动新进程（继承当前环境变量/参数），再退出当前进程。
        启动新进程前先关闭日志句柄——否则新旧实例短暂共存时会并发写同一日志
        文件，字节交错导致日志损坏（本次日志乱码的根因）。"""
        try:
            import subprocess
            script = sys.executable if getattr(sys, "frozen", False) \
                else os.path.abspath(sys.argv[0])
            cmd = [script, "-u"] if getattr(sys, "frozen", False) else \
                [sys.executable, "-u", os.path.abspath(sys.argv[0])]
            cmd += sys.argv[1:]
            logger.info("重启：%s", " ".join(cmd))
            logging.shutdown()   # 先落盘重启记录，再关句柄，防并发写坏日志
            subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(sys.argv[0])))
        except Exception as e:
            logger.error("重启失败: %s", e)
            self._err("重启失败：%s" % e)
            return
        self.quit()

    def quit(self):
        try:
            self.recorder.stop()
        except Exception:
            pass
        try:
            if self._mouse_listener:
                self._mouse_listener.stop()
        except Exception:
            pass
        _restore_foreground_lock()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------- 入口 ----------
def main():
    set_dpi_aware()   # 高 DPI 下坐标才准，必须在创建窗口前调用
    setup_logging()
    acquire_single_instance()
    _sync_autostart_path()   # exe 版自检：自启动指向当前 exe（改名/移动后自动修正）
    _disable_foreground_lock()   # 让跨进程 SetForegroundWindow 可靠生效
    import atexit
    atexit.register(_restore_foreground_lock)
    mode = os.environ.get("VI_MODE", "clipboard")
    from xfyun_asr import XfyunTranscriber
    transcriber = XfyunTranscriber()
    logger.info("=== 语音输入法启动 mode=%s engine=xfyun 完整性=%s ===",
                mode, _process_integrity_level())

    recorder = Recorder()
    global app
    app = FloatingApp(transcriber, recorder, mode=mode)

    global our_hwnd
    our_hwnd = int(app.root.winfo_id())
    # 校准：winfo_id() 在 overrideredirect/Tk 顶层包装下可能与浮窗真实 HWND 不一致，
    # 曾导致浮窗被误判为"外部窗口"、文字注入回自身。用「本进程可见顶层窗口」校准，
    # 并打印对比值供日志验证。
    own = _find_own_visible_hwnd()
    if own:
        our_hwnd = own[0]
    logger.info("our_hwnd=%s (winfo_id=%s, 枚举=%s)", our_hwnd, int(app.root.winfo_id()), own)
    install_foreground_hook()

    # 全局热键 Ctrl+Alt+Space：按住说话，松开识别（与鼠标长按交互一致）
    # 不用 GlobalHotKeys：它在 Windows 上把 <ctrl>+<alt> 当作 AltGr，且只在下按
    # 时回调一次（开关式），无法实现"按住说话、松手识别"。改用 Listener 手动
    # 检测：Ctrl+Alt 按住时按 Space → 开始录音；松开 Space → 停止并识别。
    try:
        from pynput import keyboard

        hot_pressed = set()
        _HOT_CTRL = (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        _HOT_ALT = (keyboard.Key.alt_l, keyboard.Key.alt_r)

        def hot_on_press(key):
            try:
                hot_pressed.add(key)
                if key == keyboard.Key.space and not app.recording:
                    if any(k in hot_pressed for k in _HOT_CTRL) and \
                       any(k in hot_pressed for k in _HOT_ALT):
                        # 热键路径：按下 Space 时前台正是用户正在输入的目标窗口，
                        # 立即快照（on_press 只覆盖鼠标按住浮窗路径）
                        try:
                            app._snapshot_target()
                        except Exception:
                            pass
                        app.root.after(0, app.start_rec)
            except Exception:
                pass

        def hot_on_release(key):
            try:
                if key == keyboard.Key.space and app.recording:
                    app.root.after(0, app.stop_rec)
                hot_pressed.discard(key)
            except Exception:
                pass

        hot_listener = keyboard.Listener(on_press=hot_on_press,
                                         on_release=hot_on_release)
        hot_listener.start()
        logger.info("全局热键 Ctrl+Alt+Space 已注册：按住说话，松手识别")
    except Exception as e:
        logger.warning("全局热键注册失败: %s", e)

    logger.info("[voice-input] 浮窗已启动：按住图标说话，松开即识别并输出 (Ctrl+Alt+Space 也可按住切换)")
    # 启动期预校验讯飞凭证（缺密钥会在此抛出明确错误）
    threading.Thread(target=transcriber.load, daemon=True).start()
    # 启动后延迟检查讯飞配置：缺失则提醒并自动弹出配置窗口
    app.root.after(800, app._check_config_and_prompt)
    app.run()


if __name__ == "__main__":
    main()
