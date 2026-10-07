@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title novel-to-video-codex 新机安装

echo ============================================================
echo   novel-to-video-codex 新机安装
echo ============================================================
echo.
echo   前提：权重目录 models\ 与本目录【平级】放置：
echo     工作区\
echo       ├── models\                 (权重，从旧机器拷来或下面重新下)
echo       └── novel-to-video-codex\   (本目录)
echo.

REM ─── 1) Python ────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [x] 没找到 python，请先安装 Python 3.11+ 并勾选 Add to PATH
    pause & exit /b 1
)
for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
echo [1/6] Python %PYVER%

REM ─── 2) 虚拟环境 ──────────────────────────────────
if not exist venv (
    echo [2/6] 创建虚拟环境 venv ...
    python -m venv venv || (echo [x] venv 创建失败 & pause & exit /b 1)
) else (
    echo [2/6] venv 已存在，跳过
)
set PY=venv\Scripts\python.exe

REM ─── 3) 依赖（必须用阿里源，清华源会返回空列表）────
echo [3/6] 安装依赖（阿里源，约需几分钟）...
%PY% -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple
%PY% -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
if errorlevel 1 (echo [x] 依赖安装失败 & pause & exit /b 1)

REM ─── 4) ffmpeg（含 xfade + libass）────────────────
echo [4/6] 安装 ffmpeg（imageio-ffmpeg，自带二进制）...
%PY% -m pip install imageio-ffmpeg -i https://mirrors.aliyun.com/pypi/simple
for /f "delims=" %%i in ('%PY% -c "import imageio_ffmpeg,os;print(os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe()))"') do set FFDIR=%%i
echo       ffmpeg 目录: %FFDIR%

REM ─── 5) .env（仓库里没有，必须本地生成）────────────
if not exist .env (
    echo [5/6] 生成 .env ...
    copy .env.example .env >nul
    (
        echo.
        echo # ─── 本机自动写入 ───
        echo FFMPEG_PATH=%FFDIR%
        echo FFPROBE_PATH=%FFDIR%
    ) >> .env
) else (
    echo [5/6] .env 已存在，跳过（如需改权重路径请手动编辑）
)

REM ─── 6) 权重检查 ──────────────────────────────────
echo [6/6] 检查权重 ...
set MODELSDIR=%~dp0..\models
if exist "%MODELSDIR%\ltx" (
    %PY% tools\verify_ltx_weights.py
) else (
    echo.
    echo [!] 没找到 ..\models\ltx
    echo     方式A：从旧机器拷贝 models 目录过来（推荐，省 23.5GB 下载）
    echo     方式B：在本机重新下载，执行：
    echo       python tools\fast_download.py --repo hf:Lightricks/LTX-Video ^
    echo         --file ltxv-2b-0.9.8-distilled-fp8.safetensors ^
    echo         --out ..\models\ltx\ltxv-2b-0.9.8-distilled-fp8.safetensors --threads 12
)

echo.
echo ============================================================
echo   安装完成。验证命令：
echo     venv\Scripts\python.exe tools\verify_ltx_weights.py --render
echo     venv\Scripts\python.exe scripts\shoot.py --demo --engine quickcut --images dataset\yhkf --out out.mp4
echo ============================================================
pause
