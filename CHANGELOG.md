# 更新日志（CHANGELOG）

本项目所有值得记录的变更都写在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

---

## [未发布]

### 新增
- `scripts/make_stepfun_demo.py`：配音效果演示片生成器（可换音色）
  - 复用已有场景成片画面，只重配阶跃星辰 TTS，用于对比不同音色效果
  - `python scripts/make_stepfun_demo.py --voice linjiajiejie`
  - 内置规避：concat list 绝对路径、ffmpeg 不在 PATH 时自动补齐、`tpad=stop_mode=clone` + `-shortest` 音画对齐

### 修复
- **ffprobe 输出解码崩溃（问题复盘 P17）**：Windows 中文环境下 ffprobe/ffmpeg 输出 GBK(cp936) 或 UTF-8 字节，用 `text=True` 交给 Python 按本地编码解码时编码不匹配，会在子进程 **reader 线程**抛 `UnicodeDecodeError`（该异常外层 `try` 拦不住，导致时长误判为 0.0）
  - `quality/gate.py` 新增 `_safe_decode()`：字节捕获 + `utf-8 → gbk → cp936 → latin-1` 多编码回退
  - `story2/assemble.py`、`story2/gen_dialogue.py`、`story2/gen_ltx.py`、`post/compose.py`、`scripts/video_engine.py`、`scripts/main.py` 的 `subprocess.run(..., text=True)` 统一加 `errors="replace"`
  - 实测：UTF-8 中文 / GBK 中文 / ASCII 数值 / 非法字节 四类输入全部零异常

---

## [12.3] — 2026-09-26

### 新增
- **阶跃星辰（StepFun）TTS 引擎**，并设为默认
  - 端点 `POST /v1/audio/speech`，OpenAI 兼容格式，默认模型 `stepaudio-2.5-tts`
  - **Step Plan 订阅 Key 专属端点**支持：`STEPFUN_BASE_URL=https://api.stepfun.com/step_plan/v1`（订阅 Key 必须走此端点，否则报 `402 exceeded quota`）
  - 21 种情绪 → `instruction` 情绪指导词（≤200 字符），旁白附加「沉稳电影旁白」
  - 长文本自动分段：>900 字按句切分，多段 ffmpeg concat 拼接（无 ffmpeg 时 MP3 二进制直拼）
  - `rate` → `speed`、`volume` → `0.1~2.0` 参数换算
  - 33 个阶跃官方音色接入 UI（`/api/tts-voices` 带 `stepfun_available` 标志）
  - Edge→StepFun 音色映射表，回退 Edge 时反向翻译
- **引擎自动选择链**：`TTS_ENGINE` 显式指定 > stepfun（配了 Key）> cosyvoice（已启动）> edge（免费兜底）
- `test_stepfun_tts.py` 测试脚本（`--dry` 逻辑 18 项 / 完整 21 项，含真实合成与假 Key 回退链验证）

### 修复
- StepFun 返回 401/402 时自动回退 Edge-TTS，出片不中断，并给出 402 专属提示文案
- 3 个脚本 docstring 无效转义序列 `SyntaxWarning`（`compose_story2` / `smoke_render` / `train_wan_lora` 改 raw string）
- QA 阶段全场景失败并连环重跑（问题复盘 P15）：Step 7 清理的全局通配 `*_faded*` 会误删其他场景中间文件，收窄为 `scene_{id:03d}_` 前缀

### 文档
- `README.md` 重写为 v12.3 现状（此前仍写 LTX 22B + CosyVoice2）
- 新增 `README_问题复盘.md`（21 个坑的根因分类、解法与复发预防清单）
- 新增 `README_本机部署.md` 第八章 + 阶跃星辰 FAQ
- 新增 `docs/TROUBLESHOOTING.md`（18 条精简速查）
- 新增 `LICENSE`（MIT，此前 README 声称 MIT 但文件缺失）
- 补齐 `.env.example`（`STEPFUN_API_KEY` / `STEPFUN_BASE_URL` / `STEPFUN_TTS_MODEL` / `TTS_ENGINE` / `COMFYUI_PATH`）
- 补齐 `requirements.txt`（`pydantic` / `PyYAML` / `aiohttp`），GPU 重依赖改为注释化可选

### 视频链路
- Wan 2.2 A14B（wan5b）两段式引擎打通：采样+解码 → RIFE 补帧 → 2x 超分
- 换用 Kijai 版 T5 编码器（Comfy-Org repackaged 版含 `scaled_fp8` 键，节点拒收）
- `cupy-cuda12x` 依赖补齐，RIFE VFI 节点补齐 `dtype` / `torch_compile` / `batch_size` 必填参数
- clip_vision 改名适配 IPAdapter 正则（`CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors`）

### 其他
- PyInstaller onefile 打包 `dist/LuminaForge.exe`（`BASE_DIR` = exe 所在目录，便携数据）
- 项目推送至 GitHub：https://github.com/WJQ341204/naizui

---

## [12.0] 及更早

> 早期版本未纳入 Git 版本管理，变更记录不完整。以下为可追溯的关键节点：

- **JobStorage v12.0**：任务存储从 JSON 迁移到 SQLite（`output/luminaforge.db`），支持断点续跑
- 成片合并 `/api/remerge`：12 场景情绪感知转场合并
- MuseTalk 口型同步接入（`story2/assemble.py`）
- 质量门 L1（`quality/gate.py`）：时长 / 分辨率 / 帧率 / 码率 / 音频流 / 静态帧检测

---

## 版本命名说明

- **主版本**：架构级变更（如存储引擎替换、管线重构）
- **次版本**：功能性变更（如新增 TTS 引擎、新增视频引擎）
- **修订号**：Bug 修复与文档更新
