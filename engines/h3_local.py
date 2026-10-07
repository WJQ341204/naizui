"""MiniMax-H3 本地模型出片引擎（走 ComfyUI 管线）。

选型说明
--------
MiniMax-H3 是社区里**同源模型最大的一条线**（30B diffusion + Qwen3-VL-32B
文本编码器，768p / 1080p / 2K，原生带音频）。本项目出的是竖屏漫剧成片，
H3 的 `FL2VA`（首尾帧到视频）和 `Ref2VA`（参考图到视频）两条链路都吃竖屏比例，
画质明显强于 LTX-Video 2B。

**为什么走 ComfyUI 而不是 diffusers**：diffusers 0.40 有
`MiniMaxH3ModularPipeline`，但它是新的 modular 架构——`__call__` 收的是
`(state, output, kwargs)`，没有 `prompt=`/`num_frames=` 这种常规签名，
`MiniMaxH3Transformer3DModel` 也没有 `from_single_file`；本地手上的是
Comfy-Org 重打包的**单文件 ComfyUI 权重**，跟 modular 目录结构对不上。
而 ComfyUI 是社区里 H3 支持最完整的运行时，所以引擎走 ComfyUI HTTP API。

权重（Comfy-Org/MiniMax-H3，量化版）
    diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors   20.0 GB
    text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors       15.0 GB
    vae/minimax_h3_video_vae_int8_convrot.safetensors                  2.7 GB
    vae/minimax_h3_audio_vae_fp32.safetensors                          0.6 GB
合计约 38 GB 落盘 + 推理时常驻 40 GB 以上。

本机（AMD 核显 / 12 线程 CPU / 47 GB 内存）**跑不动**：
diffusers 与 ComfyUI 在 CPU 上会把 fp8 权重上转成 fp32，30B 参数光权重就要
100 GB 以上，远超物理内存。所以这个引擎的定位是
**给有 N 卡（建议 24 GB+ 显存，fp8 / int8 权重能整驻显存）的机器准备**，
在这台机器上 `--engine h3` 会直接报「需要 GPU」而不是跑 halfway 后崩。

用法
----
    python scripts\\shoot.py --demo --engine h3
    python scripts\\shoot.py --demo --engine h3ref    # 参考图链路（R2V）
"""

from __future__ import annotations

import copy
import json
import os
import random
import shutil
import time
from pathlib import Path
from typing import Optional

from .base import (
    BaseEngine,
    ClipResult,
    EngineError,
    GenerateRequest,
    ProviderUnavailable,
    resolve_ffmpeg,
)
from .tts import synthesize

FFMPEG = resolve_ffmpeg() or "ffmpeg"

# 与 ComfyUI 的 models/diffusion_models|text_encoders|vae 目录对应
DEFAULT_UNET = os.environ.get(
    "H3_UNET_NAME", "minimax_h3_fl2va_pruned_fp8_scaled.safetensors")
DEFAULT_CLIP = os.environ.get(
    "H3_CLIP_NAME", "qwen3vl_32b_nvfp4_awq.safetensors")
DEFAULT_VAE = os.environ.get(
    "H3_VAE_NAME", "minimax_h3_video_vae_int8_convrot.safetensors")
DEFAULT_AUDIO_VAE = os.environ.get(
    "H3_AUDIO_VAE_NAME", "minimax_h3_audio_vae_fp32.safetensors")

DEFAULT_STEPS = int(os.environ.get("H3_STEPS", "4"))   # 配 turbo 权重时 4 步就够
DEFAULT_FPS = int(os.environ.get("H3_FPS", "24"))
DEFAULT_WIDTH = int(os.environ.get("H3_WIDTH", "768"))
DEFAULT_HEIGHT = int(os.environ.get("H3_HEIGHT", "1344"))   # 9:16 竖屏
# H3 的帧长必须落在 17 的整数倍 + 5 的网格上（官方 ComfyMathExpression 就是这么算的）
FRAME_GRID = 17
FRAME_BASE = 5


def _snap_length(seconds: float, fps: int = DEFAULT_FPS) -> int:
    """把镜头时长换算成 H3 接受的 length（帧数）。

    官方公式：`max(5, round(a*24)) + (5 - (max(5, round(a*24)) % 17)) % 17`
    —— 即先按 24fps 取整，再对齐到 17 的网格且保持 ≡5 (mod 17)。
    """
    n = max(5, round(seconds * fps))
    pad = (FRAME_BASE - (n % FRAME_GRID)) % FRAME_GRID
    return n + pad


def _mux_audio(video: Path, audio: Path, out: Path) -> Path:
    """用项目自己的配音替换模型自带音频（成片口型/旁白统一走 Edge-TTS）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if not out.exists():
        raise EngineError(f"音画合成失败（H3）：{r.stderr[:300]}")
    return out


class H3Engine(BaseEngine):
    """MiniMax-H3（FL2VA / Ref2VA）本地引擎，经 ComfyUI 出片。"""

    name = "h3"
    kind = "local"
    capability = "both"

    def __init__(self, base_url: str | None = None, unet: str | None = None,
                 workflow_dir: Optional[Path] = None, reference: bool = False):
        # 复用项目 ComfyUI 引擎的 HTTP 交互（上传 / 提交 / 轮询 / 下载）
        from .local import ComfyUIEngine
        self.base_url = (base_url or os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")).rstrip("/")
        self._comfy = ComfyUIEngine(base_url=self.base_url, workflow_dir=workflow_dir)
        self.unet = unet or DEFAULT_UNET
        self.reference = reference   # True → 走 R2V（MiniMaxH3ReferenceToVideo）
        self.steps = DEFAULT_STEPS
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.fps = DEFAULT_FPS
        self.model = self.unet

    # ─── 可用性 ──────────────────────────────────────────────
    def is_available(self) -> bool:
        if FFMPEG is None:
            print("[h3] 未找到 ffmpeg", flush=True)
            return False
        # H3 的显存/内存开销极大，CPU-only 机器上直接判不可用，避免跑一半崩
        try:
            import torch
            has_mps = getattr(torch.backends, "mps", None)
            metal = bool(has_mps and has_mps.is_built())
            if not (torch.cuda.is_available() or metal):
                print("[h3] 本机无 CUDA / Metal，H3 需要 GPU（建议 24GB+ 显存）；"
                      "已自动降级使用 ltx / quickcut", flush=True)
                return False
        except Exception:  # noqa: BLE001  torch 都没装更不可能跑
            print("[h3] 未安装 torch，H3 需要 GPU", flush=True)
            return False
        return self._comfy.is_available()

    def describe(self) -> str:
        kind = "R2V(参考图)" if self.reference else "FL2VA(首尾帧)"
        return f"MiniMax-H3 {kind} · {self.unet} · {self.width}x{self.height} · {self.steps} 步"

    def status_report(self) -> dict:
        return {"engine": self.name, "mode": "reference" if self.reference else "fl2va",
                "unet": self.unet, "width": self.width, "height": self.height,
                "steps": self.steps, "comfyui": self.base_url}

    # ─── 生成 ────────────────────────────────────────────────
    async def generate(self, req: GenerateRequest) -> ClipResult:
        if not self.is_available():
            raise ProviderUnavailable("H3 引擎不可用（需要 GPU + 已启动的 ComfyUI）")

        out_dir = Path(req.output_dir or (Path(__file__).resolve().parent.parent / "output" / "h3"))
        out_dir.mkdir(parents=True, exist_ok=True)
        name = req.output_name or f"h3_{int(time.time())}"
        dest = out_dir / f"{name}.mp4"

        length = _snap_length(max(req.duration_seconds, 2.0), self.fps)
        seed = req.seed if req.seed is not None else random.randint(0, 2**31 - 1)

        # 配音先出（H3 自带音频会在最后被替换掉）
        audio = out_dir / f"{name}.mp3"
        text = (req.narration or req.prompt or "").strip()
        if text:
            await synthesize(text, audio, voice=getattr(req, "voice", None))

        wf_name = "h3_ref2va" if self.reference else "h3_fl2va"
        template = self._comfy.load_workflow(wf_name)

        replacements = {
            "H3_UNET_NAME": self.unet,
            "H3_CLIP_NAME": DEFAULT_CLIP,
            "H3_VAE_NAME": DEFAULT_VAE,
            "H3_AUDIO_VAE_NAME": DEFAULT_AUDIO_VAE,
            "H3_WIDTH": self.width,
            "H3_HEIGHT": self.height,
            "H3_LENGTH": length,
            "H3_STEPS": self.steps,
            "H3_SEED": seed,
            "POSITIVE_PROMPT": req.prompt or text or "cinematic shot",
        }

        # 参考图链路：把官图的 LoadImage 节点换成已上传的角色参考图
        if self.reference and req.reference_images:
            uploaded = []
            for i, p in enumerate(list(req.reference_images)[:2]):
                p = Path(p)
                if p.exists():
                    uploaded.append(await self._comfy._upload_image(p))
            if uploaded:
                for node in template.values():
                    if node.get("class_type") == "LoadImage":
                        node["inputs"]["image"] = uploaded[len(uploaded) - 1] \
                            if not uploaded else uploaded[0]

        workflow = self._comfy.fill_workflow(template, replacements)
        prompt_id = await self._comfy._submit(workflow)
        print(f"[h3] 已提交 {prompt_id[:12]}… ({wf_name}, {length} 帧@{self.fps}fps, "
              f"{self.steps} 步) — 30B 模型请耐心等", flush=True)

        entry = await self._comfy._wait_result(prompt_id, timeout=req.timeout_seconds)
        found = self._comfy.find_output(entry.get("outputs", {}))
        if not found:
            raise EngineError("H3 无输出（检查显存是否溢出：换 fp8/int8 权重或调低分辨率）")
        filename, subfolder, out_type = found

        await self._comfy._download(filename, subfolder, out_type, dest)
        if not dest.exists() or dest.stat().st_size < 10000:
            raise EngineError(f"H3 输出文件缺失或过小: {dest}")

        # 换配音（模型自带音频丢弃，成片统一用 Edge-TTS）
        if audio.exists() and audio.stat().st_size > 0:
            try:
                from .quickcut import probe_duration
                if probe_duration(dest) is None:
                    dest = _mux_audio(dest, audio, out_dir / f"{name}_配音.mp4")
                    dest = out_dir / f"{name}_配音.mp4"
                dest = _mux_audio(dest, audio, dest)
            except Exception as exc:  # noqa: BLE001
                print(f"[h3] 配音合并跳过：{exc}", flush=True)

        return ClipResult(
            video_path=dest,
            engine=self.name,
            cost_usd=0.0,
            duration_seconds=round(length / self.fps, 2),
            seed=seed,
            model=self.unet,
        )
