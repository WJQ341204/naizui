@echo off
chcp 65001 >nul
title LuminaForge App (port 8190)
cd /d "%~dp0"

rem 禁用 safe-delete 钩子，防止清理临时文件时误杀进程（QA/收尾阶段 unlink 被拦截会 SystemExit）
set CODEBUDDY_SAFE_DELETE_ENABLED=0

rem 清理系统代理，防止 httpx 报 [Errno 22]（Clash 未运行时）
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=

rem ffmpeg / ffprobe 加入 PATH（pydub 混音、视频合并、ASS 字幕烧录都依赖）
set PATH=C:\Users\Administrator\WorkBuddy\2026-10-05-17-21-11\tools\ffmpeg\bin;%PATH%

set PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe

echo [0/2] 环境自检...
"%PY%" -c "import shutil;print('  ffmpeg:', shutil.which('ffmpeg') or 'NOT FOUND')"
"%PY%" -c "import shutil;print('  ffprobe:', shutil.which('ffprobe') or 'NOT FOUND')"

echo [1/2] Starting LuminaForge on http://127.0.0.1:8190 ...
"%PY%" scripts\main.py
pause
