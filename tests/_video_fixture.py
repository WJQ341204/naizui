"""测试用「真实视频」夹具。

背景：仓库里的质量门测试原本直接依赖
`OpenMontage/projects/overtime-cat/renders/final.mp4`（30s 真成片），
但该成片并未随仓库分发（projects/ 目录为空），导致
test_quality_loop.py / test_orchestrator.py 在任何一台新机器上都必失败——
质量门拿到不存在的路径后判定「文件不存在或为空」，进而多触发一次重试，
连带把 test_retry_reseed 的调用次数断言（2 次）也带偏成 3 次。

这里提供一个自包含夹具：优先复用仓库内已有的真实视频，
否则用 ffmpeg 现场合成一段 5s 测试视频；两者都不可用时跳过测试。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 候选真实视频（按优先级）
_CANDIDATES = [
    REPO_ROOT / "OpenMontage" / "projects" / "overtime-cat" / "renders" / "final.mp4",
    REPO_ROOT / "_交付包" / "ac_opening.mp4",
]


def _generate(dest: Path) -> bool:
    """用 ffmpeg 合成一段 5s / 640x360 / 24fps 测试视频（120 帧，必过质量门）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-f", "lavfi",
        "-i", "testsrc=duration=5:size=640x360:rate=24",
        "-pix_fmt", "yuv420p", str(dest),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return False
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 10_000


def real_video_path() -> Path | None:
    """返回一个可通过质量门的真实视频路径；无法获得时返回 None。"""
    for p in _CANDIDATES:
        if p.exists() and p.stat().st_size > 10_000:
            return p
    cached = Path(tempfile.gettempdir()) / "luminaforge_test_5s.mp4"
    if cached.exists() and cached.stat().st_size > 10_000:
        return cached
    return cached if _generate(cached) else None
