@echo off
chcp 65001 >nul
title LuminaForge - 出片 (QuickCut)
cd /d "%~dp0"

rem ffmpeg / ffprobe 入 PATH（QuickCut 推镜、xfade 转场、ASS 字幕烧录都依赖）
set PATH=C:\Users\Administrator\WorkBuddy\2026-10-05-17-21-11\tools\ffmpeg\bin;%PATH%

set PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe

echo ============================================================
echo   LuminaForge 出片
echo   默认用包内示例故事，引擎 quickcut（无需显卡 / 无需 API key）
echo   想换自己的小说： python scripts\shoot.py --novel 你的文本.txt
echo   想换音色：       python scripts\shoot.py --demo --voice 晓晓
echo   想走云端 AIGC：  python scripts\shoot.py --demo --engine cloud
echo ============================================================
echo.

"%PY%" scripts\shoot.py --demo --out output\film_demo.mp4 %*
echo.
pause
