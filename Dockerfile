# ---- Novel-to-Video 部署镜像 ------------------------------------------------
#
# 默认构建 **CPU 版**：没有 GPU 的机器（含本机 AMD 核显 + 12 线程 CPU）也能跑，
# 走 QuickCut 底图 / LTX-Video 2B(CPU) 出片，出图走云端 provider 或 ComfyUI。
#
# 有 N 卡时构建 GPU 版（需 nvidia-container-toolkit 与 WSL2 的 GPU 支持）：
#     docker build --build-arg ENABLE_GPU=1 -t novel2vid-app:gpu .
#
# 分层策略：前端 → 依赖 → 运行时。改业务代码不会触发 torch 重装（依赖层有缓存）。
# -----------------------------------------------------------------------------

# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13
ARG ENABLE_GPU=0

# ── 1. 前端（React + Vite）───────────────────────────────────────────────────
FROM node:20-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── 2. 依赖层（缓存热点：只有 requirements.txt 或 torch 开关变了才重装）──────
FROM python:${PYTHON_VERSION}-slim AS deps
ARG ENABLE_GPU=0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && if [ "$ENABLE_GPU" = "1" ]; then \
           pip install -r requirements.txt torch --index-url https://download.pytorch.org/whl/cu128; \
       else \
           pip install -r requirements.txt torch --index-url "$TORCH_INDEX_URL"; \
       fi

# ── 3. 运行时 ────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG PYTHON_VERSION=3.13
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    COMFYUI_URL="" \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

# 依赖整层搬过来（不写死 site-packages 路径，换 Python 版本也不炸）
COPY --from=deps /usr/local /usr/local
COPY --from=web-build --link /web/dist /app/web/dist

COPY requirements.txt ./
COPY settings.yaml ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY scripts/ scripts/
COPY app/ app/
COPY engines/ engines/
COPY config/ config/
COPY static/ static/
COPY prompts/ prompts/
COPY assets/ assets/
COPY bgm/ bgm/
COPY sfx/ sfx/
COPY comfyui/ comfyui/

# 模型与产出目录（生产环境由 compose 挂盘覆盖）
RUN mkdir -p /app/output /app/models /app/.ltx

EXPOSE 8190

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8190/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "scripts/main.py"]
