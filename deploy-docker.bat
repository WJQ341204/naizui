@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Novel-to-Video / Docker 部署
echo ============================================================
echo.

:: ── 1. 检查 / 安装 Docker ──────────────────────────────────
echo [1/3] 检查 Docker 环境...
docker --version >nul 2>&1
if errorlevel 1 (
    echo      未检测到 docker，先装 Docker Desktop（WSL2 后端）...
    powershell -ExecutionPolicy Bypass -File "%~dp0docker\install-docker.ps1"
    echo.
    echo [!] 安装流程结束。若提示重启，重启后再运行本脚本。
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('docker --version') do echo      %%i
docker compose version >nul 2>&1
if errorlevel 1 (
    echo [!] 缺少 compose 插件，请升级 Docker Desktop。
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('docker compose version') do echo      %%i
echo.

:: ── 2. 构建镜像 ───────────────────────────────────────────
set /p REBUILD="重新构建镜像？(y/N，默认 N 直接用缓存): "
if /i "%REBUILD%"=="y" (
    echo [2/3] 构建镜像（首次含 CPU torch，约 5-10 分钟）...
    docker compose build app
) else (
    echo [2/3] 跳过构建，直接启动现有镜像。
)
echo.

:: ── 3. 启动 ───────────────────────────────────────────────
echo [3/3] 启动服务...
docker compose up -d
echo.
echo ── 状态 ──────────────────────────────────────────────────
docker compose ps
echo.
echo 打开 http://localhost:8190
echo 日志： docker compose logs -f app
echo 有 NVIDIA 显卡时加 ComfyUI： docker compose --profile gpu up -d
echo.
pause
