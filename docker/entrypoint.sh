#!/usr/bin/env bash
# Novel-to-Video 容器入口
#   1) 补齐运行时目录
#   2) 若配置了 COMFYUI_URL，等 ComfyUI 就绪（可选依赖，没配就直接跑，不阻塞）
#   3) 接管 CMD
set -euo pipefail

echo "[entry] 启动容器，PYTHONPATH=$PYTHONPATH"

mkdir -p /app/output /app/models /app/.ltx

# ── ComfyUI 就绪等待（可选）─────────────────────────────────────────────
COMFY_URL="${COMFYUI_URL:-}"
if [ -n "$COMFY_URL" ]; then
    host="${COMFY_URL#http://}"
    host="${COMFY_URL#https://}"
    host="${host%%/*}"
    host="${host%%:*}"                       # 剥掉端口，健康检查走 8188 默认端口
    if [ -n "$host" ] && [ "$host" != "localhost" ] && [ "$host" != "127.0.0.1" ]; then
        echo "[entry] 等待 ComfyUI 就绪：$COMFY_URL （最多 180s）"
        ok=0
        for _ in $(seq 1 60); do
            if curl -fsS --max-time 5 "${COMFY_URL%/}/api/health" >/dev/null 2>&1; then
                ok=1; break
            fi
            sleep 3
        done
        [ "$ok" = "1" ] && echo "[entry] ComfyUI 就绪" \
            || echo "[entry] 警告：ComfyUI 在 180s 内未就绪，继续启动（画面将回落到 QuickCut 底图）"
    fi
fi

# HF 下载走国内镜像（镜像内默认直连，可在 .env 里设 HF_ENDPOINT）
[ -n "${HF_ENDPOINT:-}" ] && echo "[entry] HF_ENDPOINT=$HF_ENDPOINT"

exec "$@"
