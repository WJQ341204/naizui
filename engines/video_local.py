"""本地视频模型出片引擎（LTX-Video 2B / distilled fp8）。

选型说明
--------
本机（AMD 核显 + 12 线程 CPU，Windows）没有任何可用的 GPU 后端：
没有 CUDA、没有 ROCm（Windows 上不存在）、`torch-directml` 只发到 cp310 wheel
而当前 venv 是 Python 3.13。所以本地推理只能走 CPU。

在这个前提下选 LTX-Video 2B distilled fp8 是因为：

* **fp8 是官方量化权重**（`Lightricks/LTX-Video` 仓库里的
  `ltxv-2b-0.9.8-distilled-fp8.safetensors`，4.46 GB），不是社区 GGUF，
  diffusers 能直接读，不用自己转格式。
* **distilled = 4 步采样**。普通版要 30+ 步，纯 CPU 上根本等不起；
  蒸馏版 4 步就够，这是"能不能用"的分水岭。
* **2B 是能跑得动的数量级**。同仓库的 13B distilled fp8 也有（15.7 GB），
  但 13B 参数在 12 线程 CPU 上一帧都出不来。
* **任意宽高比**，竖屏（512×768 / 576×1024）原生支持，不用裁切。
* diffusers 有 `LTXPipeline` 一等公民支持，本项目 `agent_skills=["ltx2"]`
  也是按 LTX 设计的，换引擎成本最低。

注意：fp8 在这里省的是**下载量和常驻内存**，不是速度。diffusers 在 CPU 上
会自动把 fp8 权重上转成 fp32 做计算，画质与 fp16 版一致。想让 fp8 真正
吃加速，必须落到 CUDA 上（换有 N 卡的机器）。

用法
----
    python scripts\\shoot.py --demo --engine ltx
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
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

DEFAULT_MODEL_REPO = os.environ.get("LTX_MODEL_REPO", "Lightricks/LTX-Video")
DEFAULT_MODEL_DIR = os.environ.get("LTX_MODEL_DIR", "")
# 项目根下的本地权重目录（手工用 hf-mirror 拉的那份 fp8，避免重复下载）
# 注意：必须是「仓库根/models/ltx」，与 README 一致。以前这里多写了一层 .parent，
# 指向仓库的上一级目录，导致按 README 放权重却永远找不到，只能靠 LTX_MODEL_DIR 兜。
LOCAL_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "ltx"
DEFAULT_STEPS = int(os.environ.get("LTX_STEPS", "4"))          # distilled 默认 4 步
DEFAULT_WIDTH = int(os.environ.get("LTX_WIDTH", "512"))
DEFAULT_HEIGHT = int(os.environ.get("LTX_HEIGHT", "768"))      # 2:3 竖屏
DEFAULT_FPS = int(os.environ.get("LTX_FPS", "24"))
MAX_FRAMES = int(os.environ.get("LTX_MAX_FRAMES", "192"))      # LTX 最长约 8 秒
FRAME_STEP = 8                                                  # 帧数必须是 8 的倍数


# ─── 工具 ──────────────────────────────────────────────────
def _snap(v: int) -> int:
    """对齐到 16（模型内部要求），并返回 >= 原值的 8 的倍数。"""
    return max(FRAME_STEP, int((v + 15) // 16 * 16))


def _frames_to_mp4(frames_dir: Path, out_path: Path, fps: int,
                   loop_to: Optional[float] = None) -> None:
    """把 PNG 帧序列编码成 mp4。

    frames_dir 里的帧必须按名字排序（frame_00001.png …）。
    loop_to 给定时，把循环片段拉长到这么多秒（LTX 帧数有限，配音往往更长）。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(fps), "-i", str(frames_dir / "frame_%05d.png")]
    if loop_to:
        # 数一下有多少帧，用 loop 滤镜把循环片段拉到目标时长
        n = len([p for p in frames_dir.iterdir() if p.suffix == ".png"])
        cmd += ["-vf", f"loop=loop=-1:size={n},setsar=1", "-t", f"{loop_to:.2f}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-an", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not out_path.exists():
        raise EngineError(f"帧序列编码失败：{r.stderr[:300]}")


def _mux_audio(video: Path, audio: Path, out: Path) -> Path:
    """把配音并进画面（-shortest 让时长跟着音频走）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not out.exists():
        raise EngineError(f"音画合成失败：{r.stderr[:300]}")
    return out


class LTXVideoEngine(BaseEngine):
    """LTX-Video 本地模型（CPU 推理）。"""

    name = "ltx"
    kind = "local"
    capability = "video"

    def __init__(self, model_dir: Optional[str] = None,
                 width: Optional[int] = None, height: Optional[int] = None,
                 steps: Optional[int] = None, fps: int = DEFAULT_FPS,
                 seed: Optional[int] = None):
        # 本地手工拉的权重优先，其次 .env，最后回落到 HF 仓库名
        if model_dir:
            self.model_dir = model_dir
        elif DEFAULT_MODEL_DIR and Path(DEFAULT_MODEL_DIR).exists():
            self.model_dir = DEFAULT_MODEL_DIR
        elif LOCAL_MODEL_DIR.exists():
            self.model_dir = str(LOCAL_MODEL_DIR)
        else:
            self.model_dir = DEFAULT_MODEL_REPO
        self.width = width or DEFAULT_WIDTH
        self.height = height or DEFAULT_HEIGHT
        self.steps = steps or DEFAULT_STEPS
        self.fps = fps
        self.seed = seed
        self._pipe = None

    # ─── 可用性 ────────────────────────────────────────────
    def is_available(self) -> bool:
        try:
            import torch          # noqa: F401
            import diffusers      # noqa: F401
        except Exception as exc:  # noqa: BLE001
            print(f"[ltx] 推理依赖缺失：{exc}", flush=True)
            return False
        return FFMPEG is not None

    def status_report(self) -> dict:
        return {
            "model_dir": self.model_dir,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "device": "cpu（本机无可用 GPU 后端）",
        }

    # ─── 管道（懒加载，省启动时间）──────────────────────────
    def _get_pipe(self):
        if self._pipe is not None:
            return self._pipe
        try:
            import torch
            from diffusers import LTXPipeline
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"LTX 依赖缺失：{exc}") from exc

        # fp8 权重：CPU 上由 diffusers 自动 upcast 到 fp32 计算
        dtype = torch.float32
        pipe = self._load_pipeline(dtype)
        if pipe is None:
            raise ProviderUnavailable(
                f"LTX 模型加载失败（目录 {self.model_dir}）："
                f"标准目录结构缺失，且未在根目录找到单文件权重 "
                f"*.safetensors。请确认 ltxv-2b-*.distilled-fp8.safetensors "
                f"已下载到 {self.model_dir}")

        if torch.cuda.is_available():
            pipe = pipe.to("cuda")
        else:
            pipe.enable_attention_slicing()   # CPU / 共享内存下省内存
        self._pipe = pipe
        return pipe

    # ─── 管道加载（多策略兜底）─────────────────────────────
    @staticmethod
    def _remap_fp8_key(k: str) -> str:
        """官方 fp8 单文件的 key → diffusers 的 key。

        官方单文件用的是一套旧命名，直接 load 会全部对不上：
            model.diffusion_model.*  → 去前缀
            adaln_single.*           → time_embed.*
            patchify_proj.*          → proj_in.*
            attnN.q_norm / k_norm    → attnN.norm_q / norm_k
        （单文件还打包了 VAE，key 以 vae. 开头，transformer 用不上）
        """
        k = k.replace("model.diffusion_model.", "")
        k = k.replace("adaln_single.", "time_embed.")
        k = k.replace("patchify_proj.", "proj_in.")
        return k.replace("q_norm.", "norm_q.").replace("k_norm.", "norm_k.")

    def _load_transformer(self, single: Path, model_dir: Path, dtype):
        """本地 config + 手工 state dict 映射。

        为什么不用 from_single_file：它会去 hub 拉 `Lightricks/LTX-Video-0.9.5`
        的 config，本机代理直接返回 502（见 tools/verify_ltx_weights.py 同款处理）。
        """
        from diffusers import LTXVideoTransformer3DModel
        from safetensors.torch import load_file

        cfg = LTXVideoTransformer3DModel.load_config(str(model_dir), subfolder="transformer")
        tx = LTXVideoTransformer3DModel.from_config(cfg)
        sd = load_file(str(single), device="cpu")
        mapped = {
            self._remap_fp8_key(k): v.to(dtype)
            for k, v in sd.items() if not self._remap_fp8_key(k).startswith("vae.")
        }
        missing, unexpected = tx.load_state_dict(mapped, strict=False)
        if missing or unexpected:
            print(f"[ltx] transformer 权重映射未完全对齐：缺 {len(missing)} / 多 {len(unexpected)}",
                  flush=True)
        return tx

    def _load_pipeline(self, dtype):
        """本地手工拉的权重不是标准目录结构，按「标准 → 单文件」两级兜底。"""
        import os

        # 权重都在本地，强制离线：否则 diffusers 会去 hub 拉 config，
        # 本机代理直接 502（见 tools/verify_ltx_weights.py 同款处理）。
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        from diffusers import LTXVideoTransformer3DModel
        from transformers import T5EncoderModel, T5TokenizerFast

        single = None
        d = Path(self.model_dir)
        if d.exists():
            hits = sorted(d.glob("*.safetensors"))
            if hits:
                single = hits[-1]          # 仓库根目录的整包权重

        # 策略 1：标准目录（含 model_index.json）
        # 用 LTXImageToVideoPipeline：本引擎的玩法是「首帧图 + 提示词 → 视频」，
        # 而纯 LTXPipeline 只做文生视频，不接受 image 参数（diffusers 0.40）。
        try:
            from diffusers import LTXImageToVideoPipeline
            return LTXImageToVideoPipeline.from_pretrained(
                self.model_dir, torch_dtype=dtype,
                local_files_only=bool(d.exists()))
        except Exception:  # noqa: BLE001
            pass

        if single is None:
            return None

        # 策略 2：单文件权重 + 其余组件按子目录手工组装
        try:
            from diffusers import (
                AutoencoderKLLTXVideo, FlowMatchEulerDiscreteScheduler,
                LTXImageToVideoPipeline,
            )
            tx = self._load_transformer(single, d, dtype)
            pipe = LTXImageToVideoPipeline(
                transformer=tx,
                vae=AutoencoderKLLTXVideo.from_pretrained(
                    self.model_dir, subfolder="vae", torch_dtype=dtype),
                text_encoder=T5EncoderModel.from_pretrained(
                    self.model_dir, subfolder="text_encoder", torch_dtype=dtype),
                tokenizer=T5TokenizerFast.from_pretrained(
                    self.model_dir, subfolder="tokenizer"),
                scheduler=FlowMatchEulerDiscreteScheduler.from_pretrained(
                    self.model_dir, subfolder="scheduler"),
            )
            return pipe
        except Exception as exc:  # noqa: BLE001
            print(f"[ltx] 手工组装失败：{exc}", flush=True)
            return None

    # ─── 生成 ──────────────────────────────────────────────
    async def generate(self, req: GenerateRequest) -> ClipResult:
        if not self.is_available():
            raise ProviderUnavailable("LTX 引擎不可用（缺 torch/diffusers 或 ffmpeg）")

        out_path = Path(req.output_dir) / f"{req.output_name}.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        work = out_path.parent / ".ltx"
        work.mkdir(parents=True, exist_ok=True)
        frames_dir = work / f"{req.output_name}_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        started = time.time()

        # 尺寸优先用本次请求指定的（CLI 的 --width/--height），否则回落引擎默认。
        # 踩过：以前只用 self.width，导致 --width 256 完全无效，仍按 512x768 推理，
        # CPU 上白白慢 6 倍。
        width = req.width or self.width
        height = req.height or self.height

        # 1) 首帧：有参考图就用，没有就程序化生成一张同调性的底图
        #    注意 make_backdrop 返回的已经是 PIL.Image，别再套 Path()
        from PIL import Image

        first_frame = Path(req.first_frame) if req.first_frame else None
        if first_frame is not None and first_frame.exists():
            image = Image.open(first_frame).convert("RGB").resize(
                (width, height), Image.LANCZOS)
        else:
            from .quickcut import make_backdrop
            image = make_backdrop(width, height,
                                  seed=req.seed or random.randint(0, 9999))

        # 2) 帧数：按镜头时长算，对齐到 8 的倍数，并设上限
        want = int(max(req.duration_seconds, 1.0) * self.fps)
        num_frames = min(MAX_FRAMES, max(FRAME_STEP, (want // FRAME_STEP) * FRAME_STEP))

        # 3) 配音（先出音频，后面按它的长度决定循环到几秒）
        audio = work / f"{req.output_name}.mp3"
        text = (req.narration or req.prompt or "").strip()
        if text:
            await synthesize(text, audio, voice=getattr(req, "voice", None))
        audio_dur = 0.0
        if audio.exists() and audio.stat().st_size > 0:
            from .quickcut import probe_duration
            audio_dur = probe_duration(audio) or 0.0

        # 4) 模型推理
        pipe = self._get_pipe()
        import torch
        seed = req.seed if req.seed is not None else random.randint(0, 2**31 - 1)
        print(f"[ltx] 推理中：{width}x{height} / {num_frames} 帧 / "
              f"{self.steps} 步（CPU，首次加载含权重读取）…", flush=True)
        t0 = time.time()
        try:
            out = pipe(
                prompt=req.prompt or text or "cinematic shot",
                image=image,
                width=width,
                height=height,
                num_frames=num_frames,
                num_inference_steps=self.steps,
                guidance_scale=float(os.environ.get("LTX_GUIDANCE", "3.0")),
                generator=torch.Generator().manual_seed(seed),
            )
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"LTX 推理失败：{exc}") from exc
        print(f"[ltx] 推理耗时 {time.time() - t0:.1f}s", flush=True)

        # 5) 帧序列落盘
        #    diffusers 0.40 的 out.frames 是「批次 → 帧」的二维列表，且帧是 PIL.Image，
        #    不是 tensor（老代码两个假设都错了）
        frames_seq = out.frames[0] if (out.frames and isinstance(out.frames[0], list)) else out.frames
        for p in frames_dir.glob("*.png"):
            p.unlink()
        for i, frame in enumerate(frames_seq):
            frame.convert("RGB").save(frames_dir / f"frame_{i + 1:05d}.png")

        # 6) 编码 + 配音
        raw = work / f"{req.output_name}_raw.mp4"
        _frames_to_mp4(frames_dir, raw, self.fps,
                       loop_to=(audio_dur + 0.35) if audio_dur else None)
        if audio_dur:
            final = _mux_audio(raw, audio, out_path)
        else:
            final = Path(shutil.move(str(raw), str(out_path)))

        return ClipResult(
            video_path=final,
            engine=self.name,
            duration_seconds=float(req.duration_seconds),
            quality_report={
                "engine": self.name,
                "model": self.model_dir,
                "frames": num_frames,
                "steps": self.steps,
                "size": f"{self.width}x{self.height}",
                "device": "cpu",
                "wall_seconds": round(time.time() - started, 1),
                "downgraded_from": "",
            },
        )
