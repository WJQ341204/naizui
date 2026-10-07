# 墨影流光 / LuminaForge — 小说转视频 AI 管线

> **v12.3** · 把小说文本自动转成竖屏短视频：AI 分镜 → 关键帧 → 图生视频 → 情绪配音 → 字幕/BGM → 成片。

![status](https://img.shields.io/badge/TTS-%E9%98%B6%E8%B7%83%E6%98%9F%E8%BE%B0-green) ![status](https://img.shields.io/badge/%E8%A7%86%E9%A2%91-Wan2.2%20A14B-blue) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

**文档导航**：[**Docker 部署**（容器化，CPU/双模）](docs/Docker部署.md) · [部署与出片手册](README_本机部署.md) · [**问题复盘与故障档案（21 个坑的根因与解法）**](README_问题复盘.md) · [故障速查](docs/TROUBLESHOOTING.md) · [更新日志](CHANGELOG.md) · [**优化方案（8 类）**](docs/OPTIMIZATION.md) · [架构](docs/ARCHITECTURE.md) · [管线](docs/PIPELINE.md)

> 本机是 **AMD 核显 + 32 线程 CPU，无可用 GPU 后端**（无 CUDA、无 ROCm、Windows 无 ROCm）。
> 不确定自己机器缺什么，先跑 `python tools/doctor.py`。
> 直接跑本机流程请看 `README_本机部署.md`；想容器化、或要把管线搬到有 N 卡的机器上，走 `docs/Docker部署.md`。
> 一键部署脚本：`deploy-docker.bat`（自动装 Docker Desktop + WSL2 + 构建 + 启动）。

---

## 一、它能做什么

输入一段小说文本，输出一条带**情绪配音 + 精确字幕 + 背景音乐**的竖屏视频：

| 环节 | 实现 |
|---|---|
| AI 分镜 | DeepSeek 拆解场景、生成画面描述与台词 |
| 关键帧 | ComfyUI SDXL / PULID 角色一致性出图 |
| 图生视频 | **Wan 2.2 A14B（wan5b 两段式引擎）**：采样+解码 → RIFE 补帧 → 2x 超分 |
| 配音 | **阶跃星辰 StepFun（默认）** / CosyVoice2（本地）/ Edge-TTS（兜底） |
| 情绪 | 21 种场景情绪自动转配音指导，强度 1-10 可调 |
| 后期 | 精确字幕、BGM 混音、转场、快进/快退、成片合并 |

**实测**：12 场景成片 68 秒 / 1408×2560@48fps；单场景全链路约 12-17 分钟（RTX 5070 8GB，低显存模式）。

---

## 二、最快出片：QuickCut（零显卡 / 零 API key）

只想先看到成片？不用 ComfyUI、不用显卡、不用任何 key，只要 ffmpeg 在 PATH 里：

```bat
shoot.bat
:: 等价于： python scripts/shoot.py --demo --out output/film_demo.mp4
```

`--demo` 用包内示例故事（《被辞退那天，我写的代码卖了八千万》，4 镜头 / 约 36 秒），
实测本机 30 秒出片。**换成自己的小说**：

```bat
python scripts\shoot.py --novel 我的小说.txt --out output\film.mp4
python scripts\shoot.py --novel 我的小说.txt --max-scenes 12 --voice 晓晓
```

**QuickCut 的出片原理**：Edge-TTS 逐段配音 → 程序化暗调底图（PIL）+ Ken Burns 推镜
→ ffmpeg xfade 拼接转场 → ASS 字幕烧录。画质不是 AIGC，但整条链路完整、随时可替换。

**升级成 AIGC 画质**：填了 `ARK_API_KEY` / `KLING_API_KEY` / `FAL_KEY` 等任一 key 后，
`--engine auto`（默认）自动走云端 provider 出图生视频；本机装了 ComfyUI 则走本地。
引擎优先级：**cloud（填 key）→ comfyui（有 N 卡）→ quickcut（永远可用）**。

> 输出物：`output/film_demo.mp4`（成片）、`output/film_demo.ass`（字幕）、
> `output/manifest.json`（每镜头时长 / 字幕时间轴 / 文本清单）。
> 其它参数：`--voice`（音色）、`--transition`（转场）、`--width/--height`（分辨率）、
> `--zoom`（推镜速度）、`--engine quickcut|cloud|auto`。

---

## 三、本地视频模型：LTX-Video 2B distilled fp8（真 AIGC 画面）

QuickCut 出的是程序化底图，不算 AIGC。想让画面**真的由模型生成**，用本地视频模型：

```bat
python scripts\shoot.py --demo --engine ltx --out output/film_ltx.mp4
```

出片链路：Edge-TTS 配音 → LTX-Video 2B（T2V/I2V）生成真实运动画面
→ ffmpeg 编码 → 转场 → ASS 字幕烧录。**不需要任何 API key，不联网调用外部服务。**

### 为什么选这个模型

| 候选 | 体积 | 结论 |
|---|---|---|
| **LTX-Video 2B distilled fp8** | 4.2 GB（+ VAE 1.6 GB + T5 17.8 GB） | **选它**：官方量化、4 步出片、8 秒长镜头、任意宽高比 |
| LTX-Video 13B distilled fp8 | 15.7 GB | 排除：13B 参数在 32 线程 CPU 上一帧都出不来 |
| Wan2.2-TI2V-5B fp8 | ≈10 GB | 待定：社区量化版在 hf-mirror 无缓存可查，落地风险高 |
| SVD-XT fp16 | ≈4 GB | 备选：只出 14 帧（0.5 秒），适合验证链路，不适合成片 |

**fp8 在这里省的是下载量与常驻内存，不是速度**——本机没有 CUDA、没有 ROCm
（Windows 上不存在 ROCm）、`torch-directml` 只发到 cp310 wheel 而 venv 是 Python 3.13，
所以实际只能 CPU 推理，diffusers 会自动把 fp8 权重上转成 fp32 计算，画质等同 fp16 版。
想让 fp8 真正吃加速，必须换有 N 卡的机器。

> 总成本约 **23.5 GB**（fp8 transformer 4.2 GB + VAE 1.6 GB + T5-v1_1-xxl 文本编码器 17.8 GB）。
> T5 是大头，一次下载后本地缓存复用，之后换 prompt 不再重复拉。

### 权重下载（hf-mirror，直连 HF 会被墙）

```bat
set HF_ENDPOINT=https://hf-mirror.com
:: 三个组件，放到项目根的 models/ltx/
::   1) Lightricks/LTX-Video → ltxv-2b-0.9.8-distilled-fp8.safetensors   (4.2 GB)
::   2) Lightricks/LTX-Video → vae/diffusion_pytorch_model.safetensors    (1.5 GB)
::   3) Lightricks/LTX-Video → text_encoder/model-0000{1..4}-of-00004.safetensors (17.8 GB)
```

`curl -C -` 可断点续传，下到一半断掉直接重跑同一条命令即可。

下载齐了引擎会自动从 `models/ltx/` 读（不用改代码）。

**下完先自检**，别等跑管线才崩：

```bat
python tools\verify_ltx_weights.py            :: 只查文件齐不齐 + 能不能加载
python tools\verify_ltx_weights.py --render   :: 再真跑 4 帧，确认出片不是黑屏
```

自检会顺带查 T5 四个分片的大小（按 index 的 `total_size` 均分比对），
下残了会直接告诉你是哪个分片、去重下。

### 可调参数（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LTX_MODEL_DIR` | `models/ltx` | 本地权重目录，留空则回落到 HF 仓库名 |
| `LTX_STEPS` | `4` | 采样步数。distilled 权重 4 步就够，调大只会变慢 |
| `LTX_WIDTH` / `LTX_HEIGHT` | `512 / 768` | 出图尺寸，**必须能被 32 整除**（512×288 / 768×448 / 1024×576 / 1280×704 都可以，768×432 会直接报错） |
| `LTX_FPS` | `24` | 输出帧率 |
| `LTX_MAX_FRAMES` | `192` | 单镜头最长帧数（约 8 秒） |
| `LTX_GUIDANCE` | `3.0` | guidance scale |

**性能预期（本机 32 线程实测）**：CPU 吞吐约 **120 token/s**，
token 量 = `(宽/32) × (高/32) × 帧数`。实测：

| 规格 | 帧数 | 实测耗时 |
|---|---|---|
| 256×256 | 9 | 16 s |
| 512×288 | 192 | 3.9 min |
| 768×448 | 25 | 1.0 min |
| 1024×576 | 120 | 10–14 min |
| 1280×704 | 120 | 约 20 min |

一部 5 镜头的片子：1024×576 约 **60 分钟**，1280×704 约 **100 分钟**。
想快就调小 `--width/--height` 和镜头时长，或者填云端 key 走 `--engine cloud`。

> ⚠️ **长任务必读**：引擎出完一个镜头会清理上百个中间帧 PNG，
> 若环境里有 safe-delete 钩子，会因「一次删除 ≥50 个文件」把进程杀掉，
> **白跑十几分钟**。跑之前先：
> ```bash
> export CODEBUDDY_SAFE_DELETE_ENABLED=0    # Linux/macOS/Git Bash
> set CODEBUDDY_SAFE_DELETE_ENABLED=0       # Windows cmd
> ```

引擎优先级：**cloud（填 key）→ ltx（本地模型，需权重）→ quickcut（永远可用）**。

---

### 3.6 MiniMax-H3（30B 旗舰）—— 只配 GPU 机器，本机自动降级

H3 是比 LTX 高一级的国产旗舰：30B 扩散主干 + **Qwen3-VL-32B 视觉语言编码器**
（原生读懂画面内容，不只是文本），支持 **FL2VA 首尾帧到视频** 和
**Ref2VA 参考图到视频** 两种模式，还带独立音频 VAE。

**本机（AMD 核显 / 47.6 GB 内存）跑不动**，这是物理限制不是配置问题：

- 本机没有 CUDA、Windows 没有 ROCm，只能 CPU 推理；
- H3 的 30B 权重在 CPU 上会被上转成 fp32，**常驻内存 > 100 GB**，远超 47.6 GB；
- 所以 `engines/h3_local.py::is_available()` 会**直接返回 False 并打印降级提示**，
  命令行 `auto` 会自动落到 `ltx` / `quickcut`，不会中途崩。

换一台 NVIDIA 24GB+（推荐 32GB）的机器就能跑。部署步骤：

```bat
:: 1) 拉官方 ComfyUI 工作流模板
python tools\build_h3_workflows.py
::    → comfyui/h3_fl2va.json（首尾帧，--engine h3）
::    → comfyui/h3_ref2va.json（参考图，--engine h3ref）

:: 2) 起 ComfyUI（8188）并装 MiniMaxH3 自定义节点
:: 3) 出片
python scripts\shoot.py --demo --engine h3    :: 首尾帧
python scripts\shoot.py --demo --engine h3ref :: 参考图
```

权重（Comfy-Org 重打包的单文件，放 `models/minimax-h3/`，**约 38 GB**）：

| 文件 | 体积 | 放哪 |
|---|---|---|
| `minimax_h3_fl2va_pruned_fp8_scaled.safetensors` | 20.0 GB | 根目录 / ComfyUI `diffusion_models` |
| `minimax_h3_ref2va_pruned_fp8_scaled.safetensors` | — | 同上 |
| `qwen3vl_32b_nvfp4_awq.safetensors` | 15.0 GB | `text_encoder` |
| `minimax_h3_video_vae_int8_convrot.safetensors` | 2.7 GB | `vae` |
| `minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB | `vae` |

`.env` 对应变量：`COMFYUI_URL`、`H3_UNET_NAME` / `H3_CLIP_NAME` /
`H3_VAE_NAME` / `H3_AUDIO_VAE_NAME`、`H3_STEPS`（默认 4）、`H3_FPS`（24）、
`H3_WIDTH` / `H3_HEIGHT`（默认 768×1344 竖屏）。

> 帧长不是任意值：ComfyUI 侧官方用
> `max(5, round(秒×fps)) + (5 - max(5, round(秒×fps)) % 17) % 17` 对齐到 17 网格，
> `engines/h3_local.py::_snap_length()` 复刻了这套算法，传 2 秒→56 帧、5 秒→124 帧。

---

## 四、快速开始（Windows 本机）

### 环境要求

- Python 3.11+（推荐 3.13）· Node.js 18+（仅前端构建需要）
- GPU：走 ComfyUI 出图需 NVIDIA 8GB+；**AMD / 无独显也能用**——本地视频模型
  LTX-Video 2B 走 CPU 推理（见第三章），QuickCut 更是零显卡
- FFmpeg 7+（要 `xfade` 和 `libass`，已随工作区自带在 `tools/ffmpeg/bin`）
- ComfyUI（端口 8188）· 可选 CosyVoice2（端口 50000）

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

只需要 QuickCut / 云端路线的话，到这里就够了。
要用本地 LTX 模型（`--engine ltx`）再补两步——`torch` 必须走官方 CPU 索引，
否则默认源会拉 2 GB+ 的 CUDA 版：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-ltx.txt
python tools/doctor.py        # 验证缺什么
```

### 2. 启动 ComfyUI（**必须低显存**）

```bat
python "<ComfyUI目录>\main.py" --lowvram --async-offload 2 --port 8188 --listen 127.0.0.1
```

> 不加 `--lowvram` 会 OOM 崩进程。

### 3. 配置 `.env`

```bash
cp .env.example .env
```

必填 `DEEPSEEK_API_KEY`（AI 分镜）。**阶跃星辰配音**另需填 `STEPFUN_API_KEY`（见下文）。

### 4. 启动应用

双击 `start_app.bat` → 浏览器打开 **http://127.0.0.1:8190**，或运行 `dist/LuminaForge.exe`（桌面版，免安装 Python）。

前端开发模式：`cd web && npm install && npm run dev`

---

## 五、配音引擎（v12.3 重点）

引擎按优先级自动选择，**任何引擎失败都会自动降级，出片不会中断**：

```
stepfun（配了 Key） → cosyvoice（本地已启动） → edge（免费兜底）
```

可用 `TTS_ENGINE=stepfun|cosyvoice|edge` 强制指定。

### 阶跃星辰 StepFun（推荐）

1. 到 https://platform.stepfun.com 注册并创建 API Key
2. 填入 `.env` 的 `STEPFUN_API_KEY=`
3. **Step Plan 订阅 Key 必须再设** `STEPFUN_BASE_URL=https://api.stepfun.com/step_plan/v1`
   （按量计费 Key 注释掉这行。搞错会报 `402 exceeded quota`）

能力：

- 21 种场景情绪自动转为情绪指导（紧张/悲伤/热血/神秘…），强度 1-10 控制浓淡
- 旁白/对白差异化，旁白自动附加「沉稳电影旁白」风格
- 语速 `+8%` → speed 1.08；音量 `+20%` → volume 1.2
- Edge 音色自动映射官方音色（晓晓→邻家姐姐、云健→磁性男声…）
- UI 音色列表内含 32 个阶跃官方音色，可直接选
- 长文本（>900 字）自动按句分段合成后拼接

自检：`python test_stepfun_tts.py --dry`（逻辑验证）/ `python test_stepfun_tts.py`（真实合成，耗额度）

---

## 六、环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | - | AI 分镜分析（必填） |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `COMFYUI_MODELS_DIR` | 空 | 模型目录，如 `D:/ComfyUI/models` |
| `COMFYUI_PATH` | 空 | ComfyUI 根目录（用于定位自带 ffmpeg） |
| `STEPFUN_API_KEY` | 空 | 阶跃星辰 Key，填写后自动成为默认配音引擎 |
| `STEPFUN_BASE_URL` | `https://api.stepfun.com/v1` | **Step Plan 订阅 Key 改成 `https://api.stepfun.com/step_plan/v1`** |
| `STEPFUN_TTS_MODEL` | `stepaudio-2.5-tts` | 可选 `step-tts-2` / `step-tts-mini`（轻量但不支持情绪指导） |
| `TTS_ENGINE` | 空（自动） | 强制 `stepfun` / `cosyvoice` / `edge` |

> ⚠️ `.env` 已在 `.gitignore` 中，**切勿提交密钥**。

---

## 七、项目结构

```
novel-to-video-codex/
├── scripts/main.py          # FastAPI 主应用（分镜/生成/TTS/后期全流程）
├── scripts/launcher.py      # PyInstaller 桌面启动器
├── app/                     # 配置、数据模型、ComfyUI 客户端
├── engines/                 # 视频引擎（wan5b 两段式、ltx、cloud 等）
├── config/settings.py       # YAML 配置（需 PyYAML）
├── quality/ scheduler/ pipeline/ post/   # 质量门、队列、流水线、后期合成
├── web/                     # React + Vite + TypeScript 前端
├── story2/                  # 独立脚本管线（不走桌面应用）
├── comfyui/                 # ComfyUI 工作流模板 + 启动脚本
├── cosyvoice2/              # CosyVoice2 本地 TTS 集成
├── dist/LuminaForge.exe     # 打包好的桌面版（免安装运行）
├── docs/                    # 文档（架构/搭建/管线/踩坑）
└── README_本机部署.md        # 本机实战部署指南（含实测性能与 FAQ）
```

详细文档：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/SETUP.md`](docs/SETUP.md) · [`docs/PIPELINE_USE.md`](docs/PIPELINE_USE.md) · [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)

---

## 八、技术栈

- **后端**：FastAPI + uvicorn + httpx + websockets
- **前端**：React 18 + TypeScript + Vite + Tailwind + Zustand
- **视频**：ComfyUI + Wan 2.2 A14B（I2V）· RIFE 补帧 · 2x 超分
- **配音**：阶跃星辰 StepFun / CosyVoice2 / Edge-TTS
- **后期**：FFmpeg（ASS 字幕烧录 + BGM 混音 + concat）
- **打包**：PyInstaller（onefile exe，便携数据存 exe 同级目录）

---

## 九、常见问题

见 [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)，高频问题速查：

| 症状 | 原因 |
|---|---|
| StepFun `402 exceeded quota` | Step Plan Key 走错端点，设 `STEPFUN_BASE_URL` |
| StepFun `401 invalid_api_key` | Key 填错/过期（已自动回退 Edge，出片不中断） |
| 进程莫名退出、日志有 SystemExit | safe-delete 钩子，启动脚本需 `CODEBUDDY_SAFE_DELETE_ENABLED=0` |
| LTX 出片跑到第 2 个镜头就中断 | 同上：清理上百个中间帧 PNG 触发 safe-delete 批量确认（阈值 50），非交互下直接杀进程 |
| httpx 报 `[Errno 22]` | 系统代理残留（Clash 未开），`set HTTP_PROXY=` 清空 |
| T5 报 `fp8 scaled is not supported` | text_encoders 放的是 Comfy-Org repackaged 版，换 Kijai 版 |
| `ValueError: Invalid voice '晓晓'` | 已修复：音色别名统一走 `engines/tts.py::resolve_voice()`（旧版只在读环境变量时映射） |
| `height and width have to be divisible by 32` | 尺寸必须是 32 的倍数，768×432 不行，用 768×448 / 1024×576 / 1280×704 |

---

## 十、许可证

MIT — 详见 [`LICENSE`](LICENSE)。
