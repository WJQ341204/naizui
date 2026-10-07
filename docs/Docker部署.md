# Docker 部署

把整个 novel-to-video 管线装进容器：Web UI（`:8190`）、Edge-TTS、ffmpeg 合成、
QuickCut 底图、LTX-Video 2B 本地视频模型。默认 **CPU 版**，没有显卡也能跑。

---

## 一、本机现状（2026-10-05 实测）

| 项目 | 状态 |
|---|---|
| Windows | **10 Enterprise LTSC 2024**，build 26200.6899，Client |
| Docker | **未安装**（PATH 里没有 `docker`，无安装残留） |
| WSL2 引擎 | `VmCompute` / `wslservice` 服务**不存在**（未启用 WSL2） |
| Hyper-V | 注册表存在，Enterprise 版可用 |
| 显卡 | **AMD Radeon 集显**（`atidxx64.dll` / `amdxc64.dll`），DriverStore 无 `nv_*` |
| CPU / 内存 | Family 26 Model 68，12 逻辑核 / 47.6 GB |

结论：**装 Docker Desktop 可以，但装完只能跑 CPU 模式**。ComfyUI / Wan 那套
需要 NVIDIA 的引擎在这里用不了，`--profile gpu` 是给有卡的机器准备的。

> 自动安装脚本 `docker\install-docker.ps1` 会启用 WSL2 + 装 Docker Desktop，
> 需要管理员权限、首次可能要重启一次。

---

## 二、装 Docker（本机只需做这一步）

管理员身份打开 PowerShell：

```bat
cd C:\Users\Administrator\WorkBuddy\2026-10-05-17-21-11\novel-to-video-codex
powershell -ExecutionPolicy Bypass -File docker\install-docker.ps1
```

脚本流程：启用 WSL2 / 虚拟机平台 → 装 WSL 内核 → 下载装 Docker Desktop。
中途提示重启就重启，重启后再跑一次（后面几步会跳过已完成项）。

装完手动验证：

```bat
docker --version
docker compose version
```

首次启动 Docker Desktop 要接受服务协议、等状态变绿（预计 1–3 分钟）。

### 手装（不想用脚本）

```bat
wsl --install --web-download
:: 装完重启
docker desktop:installer.exe install --accept-license
```

---

## 三、构建并启动

```bat
cd novel-to-video-codex
docker compose up -d --build
```

第一次构建要拉 node 前端 + Python 依赖 + **CPU 版 torch（约 200 MB）**，
10 分钟内能好。之后改代码只重建 app 层。

打开 http://localhost:8190

看日志：

```bat
docker compose logs -f app
docker compose ps            # 看 health 是否 up
```

---

## 四、目录挂载（重点）

| 宿主机 | 容器 | 说明 |
|---|---|---|
| `${MODEL_DIR}` → `../models` | `/app/models` (ro) | 模型权重 |
| `./output` | `/app/output` | 成片，Windows 资源管理器直接能看 |
| 项目源码 | `/app` | 镜像内打进去的，改动走 `docker compose restart app` |

`MODEL_DIR` 在 `.env` 里配，**docker compose 会自动读 `.env` 做变量替换**。
项目里的权重默认放在 workspace 的 `models/`（项目目录**外面**），所以 `.env`
写的是 `MODEL_DIR=../models`。换到别处部署就改成 `./models` 或绝对 Windows 路径
（`C:\Users\...\models`）。

> Windows 上如果报 `path is not shared with Docker Desktop`，去
> **Docker Desktop → Settings → Resources → File sharing** 勾上 `C:` 驱动。

---

## 五、有 NVIDIA 显卡的机器（可选）

```bat
:: 装驱动层的容器工具链
wsl --install
winget install NVIDIA.NVIDIAContainerToolkit
docker compose --profile gpu up -d --build
```

`gpu` profile 会额外起一个 `comfyui` 服务（8188 端口），走
`comfyanonymous/comfyui:latest` 镜像（或 `COMFYUI_IMAGE` 指定的别的镜像）。
`app` 通过 `COMFYUI_URL=http://comfyui:8188` 连它，没配就自动回落到 QuickCut 底图，
不会阻塞启动。

---

## 六、常用命令

```bat
:: 重新构建（改了 requirements.txt / Dockerfile）
docker compose build --no-cache app

:: 跑一次命令行出片
docker compose run --rm app python scripts\shoot.py --demo --engine quickcut
docker compose run --rm app python scripts\shoot.py --novel 小说.txt --engine ltx

:: 只跑 TTS 单测
docker compose run --rm app python -c "from engines.tts import synthesize; import asyncio; asyncio.run(synthesize('测试', 't.mp3'))"

:: 停 / 删
docker compose down
docker compose down -v        :: 连同匿名卷一起删

:: 进容器
docker compose exec app bash
```

---

## 七、坑位

1. **别把权重 COPY 进镜像** —— 25 GB 的 LTX 权重走挂载，`.dockerignore` 已排除
   `models/` 和 `*.safetensors`。
2. **`runtime: nvidia` 不能写在默认服务里** —— 有卡没卡都用同一份 compose，
   GPU 相关配置放进 `gpu` profile，否则 CPU 机器上 `docker compose up` 直接报错。
3. **Edge-TTS 要出网** —— Edge-TTS 走微软公共域名，容器里没网就配音失败，
   此时可改 CosyVoice2（本机 `cosyvoice2/` 目录）或换云端 TTS。
4. **LTX 在 CPU 上很慢** —— 一个 512×768 的镜头约 3–8 分钟。想快就调小
   `LTX_WIDTH`/`LTX_HEIGHT`，或直接用云端 provider（`ARK_API_KEY` 等）。
5. **改了代码要重启容器** —— 源码在镜像里，不是挂载进去的（挂载只有 models 和 output）。
   要热改就挂载源码卷，或用 `docker compose run` 临时跑脚本。
6. **`web/dist` 在容器内构建** —— 本地 `web/dist` 被 `.dockerignore` 排除，
   容器内 `npm run build` 生成；没生成时前端自动回落到 `static/` 旧版。

---

## 八、文件清单

| 文件 | 作用 |
|---|---|
| `Dockerfile` | CPU/GPU 双模镜像，三层构建（前端 → 依赖 → 运行时） |
| `docker-compose.yml` | 编排：`app`（常驻）+ `comfyui`（`gpu` profile） |
| `.dockerignore` | 排除权重 / 成片 / 素材，别把上下文撑爆 |
| `docker/entrypoint.sh` | 入口：等 ComfyUI → 补目录 → exec |
| `docker/Dockerfile.comfyui` | ComfyUI 镜像（GPU 机器用） |
| `docker/install-docker.ps1` | 本机一键装 Docker Desktop + WSL2 |
