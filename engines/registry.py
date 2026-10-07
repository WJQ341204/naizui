"""引擎注册与路由（设计文档 §2.2 engines/registry.py）。

路由策略（video_mode）：
  - auto   : 云端优先（可用且预算内），失败降级本地，再降级 Ken Burns
  - cloud  : 仅云端
  - local  : 仅本地（ComfyUI）
  - hybrid : 按场景特征混用（关键帧本地、视频段云端等，P2 细化）

用法：
    from engines.registry import EngineRouter
    router = EngineRouter()
    clip = await router.generate(req)          # 自动路由
    router.status_report()                     # 全引擎状态
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import BaseEngine, ClipResult, EngineError, GenerateRequest, ProviderUnavailable
from .cloud import CloudEngine


class EngineRouter:
    """统一生成入口：按配置路由到云端/本地引擎，含降级链。

    配置驱动：可用 EngineRouter.from_settings() 从 settings.yaml 初始化。
    """

    def __init__(
        self,
        video_mode: str = "auto",
        prefer_cloud: Optional[str] = None,
        max_cost_usd: float = 1.0,
        # comfyui(LTX) | wan(Wan2.2 A14B) | wan5b | quickcut(纯本地兜底)
        local_kind: str = "comfyui",
        allow_quickcut: bool = True,
    ):
        self.video_mode = video_mode
        self.local_kind = local_kind
        self.allow_quickcut = allow_quickcut
        self.cloud = CloudEngine(prefer=prefer_cloud, max_cost_usd=max_cost_usd)
        self._local: Optional[BaseEngine] = None  # 延迟构建（comfyui / wan / wan5b）
        self._warned: dict[str, int] = {}  # 降级原因 → 已出现次数（用于提示去重）

    @classmethod
    def from_settings(cls, settings=None) -> "EngineRouter":
        """从 Settings 构建（配置驱动入口）。"""
        if settings is None:
            from config.settings import load_settings
            settings = load_settings()
        cfg = settings.engines.cloud
        local_cfg = getattr(settings.engines, "local", None)
        local_kind = getattr(local_cfg, "local_kind", "comfyui") if local_cfg else "comfyui"
        return cls(
            video_mode=cfg.video_mode,
            prefer_cloud=None,
            max_cost_usd=cfg.max_cost_per_clip_usd,
            local_kind=local_kind,
        )

    # ─── 本地引擎（延迟构建）─────────────────────────────────
    def _get_local(self) -> BaseEngine:
        if self._local is None:
            # quickcut：无需显卡/key 的纯本地兜底，永远可用
            if self.local_kind == "quickcut":
                from .quickcut import QuickCutEngine
                self._local = QuickCutEngine()
                return self._local
            # 延迟导入，避免首屏引入 ComfyUI 依赖
            try:
                if self.local_kind == "wan":
                    from .wan import WanEngine
                    self._local = WanEngine()
                elif self.local_kind == "wan5b":
                    from .wan5b import Wan5BEngine
                    self._local = Wan5BEngine()
                elif self.local_kind == "ltx":
                    # 本地视频模型（LTX-Video 2B distilled fp8，CPU 推理）
                    from .video_local import LTXVideoEngine
                    self._local = LTXVideoEngine()
                elif self.local_kind in ("h3", "h3ref"):
                    # MiniMax-H3（30B，需 GPU）；h3ref=参考图链路 R2V
                    from .h3_local import H3Engine
                    self._local = H3Engine(reference=(self.local_kind == "h3ref"))
                else:
                    from .local import ComfyUIEngine
                    self._local = ComfyUIEngine()
            except Exception as exc:  # noqa: BLE001
                raise ProviderUnavailable(f"本地引擎不可用: {exc}")
        return self._local

    def _get_quickcut(self) -> BaseEngine:
        """最后一层兜底：图 + 配音 + Ken Burns，只要 ffmpeg 在就出片。"""
        from .quickcut import QuickCutEngine
        return QuickCutEngine()

    def _warn_once(self, key: str, message: str) -> None:
        """同一降级原因只完整提示一次，之后静默。

        `auto` 模式下路由是每个场景跑一遍的，而 CloudEngine 的不可用原因里
        带 10 行「缺哪个 key」清单——逐场景打印会把真正的进度信息埋掉
        （4 个场景 = 刷 4 遍 40 行）。这里按 key 去重：首次完整打印，
        第二次补一句「后续不再重复」，之后完全静默。
        """
        seen = self._warned.get(key, 0)
        self._warned[key] = seen + 1
        if seen == 0:
            print(message, flush=True)
        elif seen == 1:
            print(f"[Router] 上述提示后续场景不再重复（{key}）", flush=True)

    def is_available(self) -> bool:
        """与 BaseEngine 接口对齐：至少有一条出片路径。"""
        return self.has_any_engine()

    def has_any_engine(self) -> bool:
        """是否至少有一条出片路径（决定 pipeline 能否跑）。"""
        if self.cloud.is_available():
            return True
        if self.video_mode in ("auto", "hybrid"):
            return True
        try:
            if self._get_local().is_available():
                return True
        except Exception:  # noqa: BLE001
            pass
        return self.allow_quickcut and self._get_quickcut().is_available()

    # ─── 路由 ────────────────────────────────────────────────
    async def generate(self, req: GenerateRequest) -> ClipResult:
        if self.video_mode == "local":
            return await self._get_local().generate(req)
        if self.video_mode == "cloud":
            return await self.cloud.generate(req)
        # auto / hybrid：云端优先，失败降级本地，再降级 QuickCut
        try:
            return await self.cloud.generate(req)
        except ProviderUnavailable as exc:
            if self.video_mode == "cloud":
                raise
            self._warn_once("cloud-unavailable",
                            f"[Router] 云端不可用，降级本地: {exc}")
        except EngineError as exc:
            self._warn_once("cloud-failed", f"[Router] 云端失败，降级本地: {exc}")

        try:
            local = await self._get_local().generate(req)
            local.downgraded_from = "cloud"
            return local
        except Exception as local_exc:  # noqa: BLE001
            if self.video_mode == "hybrid" or not self.allow_quickcut:
                raise
            self._warn_once(f"local-unavailable:{type(local_exc).__name__}",
                            f"[Router] 本地引擎也不可用，降级 QuickCut: {local_exc}")

        clip = await self._get_quickcut().generate(req)
        clip.downgraded_from = "cloud"
        return clip

    def status_report(self) -> dict:
        quickcut_ok = self._get_quickcut().is_available()
        return {
            "video_mode": self.video_mode,
            "cloud": self.cloud.status_report(),
            "local_kind": self.local_kind,
            "local_comfyui": {"status": "P1 未接入"},
            "quickcut": {
                "status": "AVAILABLE" if quickcut_ok else "UNAVAILABLE",
                "note": "本地兜底：图 + Ken Burns 推镜 + Edge-TTS 配音",
            },
        }
