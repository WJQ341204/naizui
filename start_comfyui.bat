@echo off
chcp 65001 >nul
title LuminaForge - ComfyUI (port 8188)
cd /d D:\ComfyUI

rem 禁用 safe-delete 钩子，防止清理临时文件时误杀进程
set CODEBUDDY_SAFE_DELETE_ENABLED=0

echo [1/1] Starting ComfyUI on http://127.0.0.1:8188 ...
"D:\ComfyUI\venv\Scripts\python.exe" main.py --lowvram --reserve-vram 2 --port 8188 --listen 127.0.0.1
pause
