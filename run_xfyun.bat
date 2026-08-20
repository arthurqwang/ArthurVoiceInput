chcp 65001 >nul
@echo off
REM ============================================================
REM  语音输入法 · 讯飞引擎一键启动脚本
REM  GitHub: https://github.com/arthurqwang/ArthurVoiceInput
REM  讯飞密钥已移出本脚本，统一放在同目录的 xfyun_config.ini 中：
REM    [xfyun]
REM    app_id = 你的APPID
REM    api_key = 你的APIKey
REM    api_secret = 你的APISecret
REM  密钥获取（免费）：https://www.xfyun.cn 控制台 - 创建应用(语音听写iat)
REM ============================================================
cd /d "%~dp0"

REM ===== 强制使用讯飞引擎 =====
set VI_ENGINE=xfyun

REM ===== 默认注入方式：clipboard（Ctrl+V 粘贴）=====
REM  对 Electron / WorkBuddy / 浏览器等 Web 输入框最稳。
REM  如果想用底层键盘注入，可改成 set VI_MODE=type
set VI_MODE=clipboard

REM ===== 可选：识别语言/模型（默认中文，可不改）=====
REM set VI_LANG=zh_cn

REM ===== 检查凭证配置文件 =====
if not exist "xfyun_config.ini" (
    echo [提示] 未找到 xfyun_config.ini，请在脚本同目录创建并填写讯飞密钥，格式：
    echo        [xfyun]
    echo        app_id = 你的APPID
    echo        api_key = 你的APIKey
    echo        api_secret = 你的APISecret
    echo        免费申请： https://www.xfyun.cn  控制台 - 创建应用 - 语音听写iat
    pause
    exit /b 1
)

echo [启动] 正在加载语音输入法（讯飞引擎）...
.venv_sys\Scripts\python.exe -u voice_input.py

echo [退出] 程序已结束。按任意键关闭窗口。
pause
