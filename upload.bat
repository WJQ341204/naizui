@echo off
chcp 65001 >nul
title 上传 / 新建 GitHub 仓库
echo ============================================================
echo   novel-to-video-codex  →  GitHub 新建仓库并推送 main
echo ============================================================
echo.
echo 先去这里造一个 token（勾 repo / workflow）：
echo   https://github.com/settings/tokens^?type=token
echo.
set /p TOKEN=把 token 粘进来（直接回车=取消): 
if "%TOKEN%"=="" (echo 已取消。 & pause & exit /b 1)
set GITHUB_TOKEN=%TOKEN%

set /p REPO=新仓库名(直接回车=用 novel-to-video-codex): 
if "%REPO%"=="" set REPO=novel-to-video-codex

python scripts\upload_repo.py --repo %REPO% --private
echo.
pause
