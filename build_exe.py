# -*- coding: utf-8 -*-
"""把 voice_input.py 打包成 onedir 目录版 exe（讯飞引擎版，无 faster-whisper）。

GitHub: https://github.com/arthurqwang/ArthurVoiceInput

前置: pip install pyinstaller
用法: python build_exe.py
产物: dist/ArthurVoiceInput/ArthurVoiceInput.exe  (--noconsole, 无黑窗, onedir 目录)

为什么用 onedir 而不是 onefile：
- onefile 每次启动都要把整个包（100MB+，900+ 文件）解压到 %TEMP%\\_MEIxxxx，
  在本机低资源环境（C盘87%满、内存负载74%）下解压偶发失败/漏解压子目录，
  表现为 _tcl_data\\auto.tcl 缺失、failed to start Python、图标回退等。
- onedir 启动零解压，数据固定在同目录 _internal/ 中，彻底消除该类问题；
  且整个目录拷到任何位置都能运行（绿色软件），日志/配置写 exe 所在目录。

打包版行为差异:
- 日志 voice_input.log / 配置 xfyun_config.ini / 语音速记 voice_notes/ 均落在 exe 所在目录
- 开机自启动命令直接使用 exe 自身（_set_autostart 已适配 sys.frozen）
- 右键菜单"重启"用 exe 自身重启（restart 已适配 sys.frozen）
- 打包成功后自动同步开机自启动注册表（若已启用则指向新 exe，避免改名后失效）
"""
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))

EXE_NAME = "ArthurVoiceInput"
_AUTOSTART_NAME = "阿色语音快捷输入法"   # 与 voice_input.py 保持一致

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconsole",
    "--onedir",
    "--name", EXE_NAME,
    "--icon", os.path.join(HERE, "AVI_logo.ico"),
    # 禁用 UPX 压缩：降低构建复杂度，onedir 下不影响运行
    "--noupx",
    # 浮窗 idle logo / 窗口图标：随 exe 打包（_internal 数据目录，resource_path 读取）
    "--add-data", os.path.join(HERE, "AVI_logo28.png") + os.pathsep + ".",
    "--hidden-import", "websocket",
    "--hidden-import", "soundfile",
    # soundfile 的 DLL 数据包：必须显式收集 __init__.py，否则运行时
    # "import _soundfile_data" 失败 → cannot load libsndfile.dll (0x7e)
    "--hidden-import", "_soundfile_data",
    # PIL：浮窗 idle logo / 窗口图标渲染依赖，必须收集 Python 模块
    # （仅 .pyd 二进制的旧打包会导致 from PIL import Image 失败 → 图标回退旧版）
    "--hidden-import", "PIL.Image",
    "--hidden-import", "PIL",
    "--hidden-import", "sounddevice",
    "--hidden-import", "pyperclip",
    "--hidden-import", "pynput",
    "--hidden-import", "pynput.keyboard",
    "--hidden-import", "pynput.mouse",
    "--hidden-import", "tkinter.messagebox",
    os.path.join(HERE, "voice_input.py"),
]


def sync_autostart():
    """打包完成后同步开机自启动：若用户已启用（Run 键存在），
    更新为指向本次新生成的 exe，保证改名/重打包后自启动始终正确。"""
    import winreg
    exe = os.path.join(HERE, "dist", EXE_NAME, EXE_NAME + ".exe")
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.QueryValueEx(k, _AUTOSTART_NAME)
    except OSError:
        print("[build_exe] 开机自启动未启用，跳过同步（用户未勾选）")
        return
    cmd_val = '"%s"' % exe
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd_val)
    print("[build_exe] 开机自启动已同步:", cmd_val)


if __name__ == "__main__":
    subprocess.run(cmd, check=True)
    sync_autostart()
