"""生成引擎统一抽象：GenerateRequest / ClipResult / BaseEngine。

所有引擎（本地 ComfyUI、云端 fal/可灵/Seedance/MiniMax…）实现同一接口，
由 engines/registry.py 按配置与可用性路由。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class GenerateRequest:
    """一次视频/图片生成请求（I2V 或 T2V）。"""
    prompt: str                       # 英文提示词：动作/运镜/情绪
    # 旁白/台词文本（中文）。QuickCutEngine 用它合成配音；云端引擎忽略此字段。
    narration: Optional[str] = None
    first_frame: Optional[Path] = None    # 首帧图（I2V）；None=纯 T2V
    last_frame: Optional[Path] = None     # 尾帧（可选，双帧约束）
    width: int = 960
    height: int = 1728
    duration_seconds: float = 5.0
    fps: int = 24
    seed: Optional[int] = None
    reference_images: list[Path] = field(default_factory=list)  # 角色参考图
    negative_prompt: str = ""
    output_dir: Optional[Path] = None     # 视频落盘目录（默认引擎工作目录）
    output_name: str = ""                 # 输出文件名（不含扩展名）
    task_id: Optional[str] = None         # 断点恢复用
    max_cost_usd: Optional[float] = None  # 单段预算上限
    timeout_seconds: float = 900.0


@dataclass
class ClipResult:
    video_path: Path
    engine: str                       # 实际使用的引擎名（如 seedance_ark / kling_official / comfyui）
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    seed: Optional[int] = None
    task_id: Optional[str] = None
    model: str = ""
    quality_report: Optional[dict] = None
    downgraded_from: Optional[str] = None   # 若因失败降级，记录原引擎


class EngineError(Exception):
    """引擎执行失败（网络/API/校验等）。"""


class ProviderUnavailable(EngineError):
    """Provider 不可用：缺 key、依赖缺失、未配置。message 附修复指引。"""


class BaseEngine(abc.ABC):
    """引擎基类。name 需全局唯一。"""

    name: str = "base"
    kind: str = "cloud"               # cloud | local | hybrid
    capability: str = "both"          # i2v | t2v | both

    @abc.abstractmethod
    async def generate(self, req: GenerateRequest) -> ClipResult:
        """执行一次生成。失败抛 EngineError / ProviderUnavailable。"""

    def is_available(self) -> bool:
        """是否可立即使用（key/依赖就绪）。"""
        return True

    def estimate_cost(self, req: GenerateRequest) -> float:
        """预估成本（USD）。"""
        return 0.0

    def describe(self) -> str:
        """人类可读描述（供 UI/日志）。"""
        return self.__class__.__doc__ or self.name


# ─── ffmpeg / ffprobe 定位（全项目统一入口）────────────────────────────
# 为什么要统一：项目 .env 里 `FFMPEG_PATH=.../tools/ffmpeg/bin` 配的是**目录**，
# 而 engines/quickcut.py、post/compose.py 把它当**可执行文件**直接丢进
# subprocess.run([FFMPEG, ...])，scripts/agent_image.py 又把它当**目录**遍历。
# 两种语义并存 → 同一台机器上 quickcut 判定「没有 ffmpeg」而 agent_image 找得到。
# 这里把两种写法都抹平，调用方只管要 exe 路径。
def _resolve_tool(env_key: str, exe_name: str, fallback_key: Optional[str] = None) -> Optional[str]:
    """按「环境变量 → PATH」找可执行文件；环境变量可以是 exe 路径或所在目录。

    `fallback_key`：主变量没命中时再去这个变量里找同名工具的兄弟 exe——
    用于 ffprobe 回落到 FFMPEG_PATH 所在目录（ffmpeg/ffprobe 永远装在一起，
    而项目 .env 通常只写了 FFMPEG_PATH）。
    """
    import os
    import shutil

    def _from_dir(raw: str, name: str) -> Optional[str]:
        p = Path(raw)
        d = p if p.is_dir() else p.parent
        for candidate in (f"{name}.exe", name):
            c = d / candidate
            if c.exists():
                return str(c)
        return None

    for key in (env_key, fallback_key):
        if not key:
            continue
        raw = (os.environ.get(key) or "").strip().strip('"').strip("'")
        if not raw:
            continue
        p = Path(raw)
        if p.is_dir():
            hit = _from_dir(raw, exe_name)
            if hit:
                return hit
        elif p.exists():
            # 配的是 exe 本身就用它；配的是别的 exe 就取同目录的兄弟 exe
            return str(p) if p.name.lower().startswith(exe_name.lower()) else _from_dir(raw, exe_name)
    return shutil.which(exe_name)


def resolve_ffmpeg() -> Optional[str]:
    """ffmpeg 可执行文件路径；找不到返回 None。"""
    return _resolve_tool("FFMPEG_PATH", "ffmpeg")


def resolve_ffprobe() -> Optional[str]:
    """ffprobe 可执行文件路径；找不到返回 None（会回落到 FFMPEG_PATH 同目录）。"""
    return _resolve_tool("FFPROBE_PATH", "ffprobe", fallback_key="FFMPEG_PATH")
