"""QuickCut —— 零显卡 / 零 API key 的本地出片引擎（兜底最后一层）。

它不做 AI 生成，只用「一张图 + 一段配音 + ffmpeg」合成视频段：
    静态画面 → Ken Burns 推镜/横摇 + 配音 + 轻微胶片颗粒 → mp4

定位（engines/registry.py 的降级链末端）：
    cloud(填 key) → comfyui(本机有 N 卡) → quickcut(永远可用)

这样在没有 GPU、没有 DeepSeek/可灵 key 的机器上，整条
「小说 → 分镜 → 配音 → 出图 → 成片」链路仍能跑通并产出 mp4；
把 key 接上后，同一条流水线自动升级成 AIGC 画质。

用法：
    from engines.quickcut import QuickCutEngine
    engine = QuickCutEngine()
    clip = await engine.generate(GenerateRequest(
        prompt="叶玄机推开药铺的门，一股苦香扑面而来。", narration="叶玄机推开药铺的门，一股苦香扑面而来。"))
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseEngine, ClipResult, EngineError, GenerateRequest, resolve_ffmpeg, resolve_ffprobe
from .tts import synthesize

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ffmpeg 定位统一走 base.resolve_ffmpeg()：环境变量 FFMPEG_PATH 可以写 exe 路径，
# 也可以写 exe 所在目录（本项目 .env 写的是目录），PATH 找不到时再回落到裸名。
FFMPEG = resolve_ffmpeg() or "ffmpeg"
FFPROBE = resolve_ffprobe() or "ffprobe"

# 电影感调色：暗调青橙 / 墨影（本项目的古风药铺调性）
# 注意 dark/light 是"渐变两端"，light 别压太暗——叠暗角后画面会糊成一团黑。
PALETTES = [
    ((30, 42, 54), (196, 132, 78)),    # 冷青 + 暖褐（药铺灯）
    ((26, 32, 48), (150, 138, 196)),   # 夜蓝 + 紫
    ((38, 32, 28), (206, 142, 84)),    # 墨褐 + 灯橙
    ((24, 40, 44), (132, 186, 168)),   # 苔绿墨色
]


# ─── 程序化底图（无参考图时）─────────────────────────────────
def make_backdrop(width: int, height: int, seed: Optional[int] = None,
                  palette: int = 0) -> Path:
    """生成一张有质感的暗调渐变底图（PIL，纯 CPU，毫秒级）。"""
    from PIL import Image, ImageDraw, ImageFilter
    import numpy as np

    rnd = random.Random(seed or 0)
    dark, light = PALETTES[palette % len(PALETTES)]

    # 竖直渐变
    grad = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    base = np.zeros((height, width, 3), dtype=np.float32)
    for c in range(3):
        base[..., c] = dark[c] + (light[c] - dark[c]) * grad[..., 0]

    # 柔和光斑（2~3 团，随机位置，模拟侧光/灯笼）
    img = Image.fromarray(base.astype(np.uint8))
    draw = ImageDraw.Draw(img)
    for _ in range(rnd.randint(2, 3)):
        cx = rnd.uniform(0.15, 0.85) * width
        cy = rnd.uniform(0.1, 0.6) * height
        r = rnd.uniform(0.25, 0.6) * width
        glow = rnd.uniform(0.30, 0.55) * 255
        layer = Image.new("L", (width, height), 0)
        ImageDraw.Draw(layer).ellipse(
            [cx - r, cy - r, cx + r, cy + r], fill=int(glow))
        layer = layer.filter(ImageFilter.GaussianBlur(radius=r * 0.45))
        img = Image.composite(
            Image.new("RGB", (width, height),
                      tuple(min(255, int(light[i] + 70)) for i in range(3))),
            img, layer)

    # 细噪点（胶片颗粒）
    arr = np.asarray(img).astype(np.float32)
    arr += np.random.normal(0, 3.0, arr.shape)
    # 暗角（别压太狠，0.55 已足够出氛围）
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    nx, ny = x / width - 0.5, y / height - 0.5
    vig = 1.0 - 0.55 * np.clip((nx * nx + ny * ny) * 1.9, 0, 1)
    arr *= vig[..., None]
    # 轻微提亮中间调，避免整帧贴着黑位
    arr = arr * 1.08 - 6.0
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(out)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(0, 0, 0), width=2)
    return out


def _save_backdrop(width: int, height: int, seed: Optional[int] = None,
                   palette: int = 0) -> Path:
    out = PROJECT_ROOT / "output" / "quickcut" / f"bg_{palette}_{seed or 0}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        make_backdrop(width, height, seed, palette).save(out)
    return out


def _ff_path(p: Path) -> str:
    """Windows 路径 → ffmpeg 可接受的 `C:/...` 正斜杠形式。

    踩坑：老教程里的 `C\\:/` 转义写法在现代 ffmpeg 上会
    「Invalid argument」（冒号被当成转义序列首字符）。只用正斜杠。
    """
    return str(Path(p).resolve()).replace("\\", "/")


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                           capture_output=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


class QuickCutEngine(BaseEngine):
    """本地兜底引擎：图 + 配音 → Ken Burns 视频段。"""

    name = "quickcut"
    kind = "local"
    capability = "i2v"

    def __init__(self, voice: Optional[str] = None,
                 zoom: float = 0.0006,      # 每帧推镜步长
                 max_zoom: float = 1.10,
                 tail_pad: float = 0.35,    # 配音后留白
                 audio_cache_dir: Optional[Path] = None):
        self.voice = voice
        self.zoom = zoom
        self.max_zoom = max_zoom
        self.tail_pad = tail_pad
        # 与 CLI 共用的 TTS 缓存，避免同一句重复请求 edge-tts
        self.audio_cache_dir = Path(audio_cache_dir) if audio_cache_dir else None

    # ─── 可用性：只要 ffmpeg 在就恒可用 ──────────────────────
    def is_available(self) -> bool:
        return resolve_ffmpeg() is not None

    def estimate_cost(self, req: GenerateRequest) -> float:
        return 0.0

    def describe(self) -> str:
        return "QuickCut（本地兜底）：静态图 + Ken Burns 推镜 + Edge-TTS 配音，无需显卡/API key"

    # ─── 执行 ────────────────────────────────────────────────
    async def generate(self, req: GenerateRequest) -> ClipResult:
        if not self.is_available():
            raise EngineError("QuickCut 需要 ffmpeg（请的路径加入 PATH 或设 FFMPEG_PATH）")

        text = (req.narration or req.prompt or "").strip()
        if not text:
            raise EngineError("QuickCutEngine 需要配音文本：GenerateRequest.narration 或 prompt")

        out_dir = Path(req.output_dir or (PROJECT_ROOT / "output" / "quickcut"))
        out_dir.mkdir(parents=True, exist_ok=True)
        name = req.output_name or f"quickcut_{req.task_id or 'clip'}"
        out_dir = Path(out_dir)
        out_path = out_dir / f"{name}.mp4"

        width, height = int(req.width or 960), int(req.height or 1728)
        fps = int(req.fps or 24)
        return await asyncio.to_thread(self._render, req, text, out_path,
                                       width, height, fps)

    # ─── 同步渲染（放线程里跑，避免阻塞事件循环）──────────────
    def _render(self, req: GenerateRequest, text: str, out_path: Path,
                width: int, height: int, fps: int) -> ClipResult:
        return _render_sync(req, text, out_path, width, height, fps,
                            self.voice, self.zoom, self.max_zoom, self.tail_pad,
                            self.audio_cache_dir)


# ─── 核心渲染 ────────────────────────────────────────────────
def _render_sync(req: GenerateRequest, text: str, out_path: Path,
                 width: int, height: int, fps: int,
                 voice: Optional[str], zoom: float, max_zoom: float,
                 tail_pad: float, audio_cache_dir: Optional[Path] = None) -> ClipResult:
    tmp_dir = out_path.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 1) 配音
    audio = tmp_dir / f"{out_path.stem}.mp3"
    try:
        asyncio.run(synthesize(text, audio, voice=voice, cache_dir=audio_cache_dir))
    except Exception as exc:  # noqa: BLE001
        raise EngineError(f"配音失败（TTS）: {exc}") from exc
    audio_dur = probe_duration(audio)
    if audio_dur <= 0:
        raise EngineError("配音时长探测失败（ffprobe 不可用？）")

    # 2) 时长策略：配音 + 尾留白，但不超过请求时长（留出导演留白）
    target = max(audio_dur + tail_pad, req.duration_seconds * 0.8)
    target = max(target, audio_dur + 0.15)
    total_dur = round(min(target, audio_dur + 1.2), 3)
    frames = max(24, int(round(total_dur * fps)))

    # 3) 画面
    if req.first_frame and Path(req.first_frame).exists():
        image = Path(req.first_frame)
    else:
        seed = req.seed if req.seed is not None else abs(hash(text)) % 9999
        image = _save_backdrop(width, height, seed)
    img_arg = _ff_path(image)

    # 4) Ken Burns：z 随帧线性增长（on = 当前输出帧序号），中心缩放
    zoom_expr = f"min(1+{zoom}*on,{max_zoom})"
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},"
        f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={width}x{height}:fps={fps}"
    )

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", img_arg,
        "-i", str(audio),
        "-filter_complex",
        f"[0:v]{vf}[v];[1:a]apad[a]",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{total_dur}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-shortest", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=900)
    if r.returncode != 0 or not out_path.exists():
        err = (r.stderr or b"").decode("utf-8", "replace")[-600:]
        raise EngineError(f"QuickCut 渲染失败: {err}")

    return ClipResult(
        video_path=out_path,
        engine="quickcut",
        cost_usd=0.0,
        duration_seconds=probe_duration(out_path) or total_dur,
        seed=req.seed,
        task_id=req.task_id,
        model="quickcut/kenburns+edgetts",
        quality_report={
            "narration_seconds": round(audio_dur, 3),
            "video_target_seconds": total_dur,
            "backdrop": str(image),
            "note": "本地兜底画质：图+推镜+配音；接入云端 provider 可升级为 AIGC",
        },
    )
