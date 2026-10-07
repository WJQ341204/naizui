#!/usr/bin/env python3
"""环境自检：一眼看清这台机器能跑哪条出片路线、缺什么、怎么补。

新人拿到仓库最常卡在三件事上——ffmpeg 不在 PATH、LTX 依赖没装、
权重没下或下残了。跑一次这个就知道该做什么，不用等出片跑到一半才报错。

    python tools/doctor.py

退出码：0 = 至少有一条可用出片路线；1 = 一条都没有。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = "  [ok]  "
MISS = "  [--]  "
WARN = "  [!!]  "


def has(mod: str) -> tuple[bool, str]:
    try:
        m = importlib.import_module(mod)
        return True, getattr(m, "__version__", "?")
    except Exception:
        return False, ""


def section(title: str) -> None:
    print(f"\n── {title} ──")


def main() -> int:
    print(f"LuminaForge 环境自检  ·  Python {sys.version.split()[0]}  ·  {os.cpu_count()} 线程")

    # ── 1. ffmpeg ──
    section("ffmpeg")
    try:
        from engines.base import resolve_ffmpeg
        ff = resolve_ffmpeg()
    except Exception as exc:
        print(f"{WARN}engines.base 导入失败：{exc}")
        ff = shutil.which("ffmpeg")
    if ff:
        print(f"{OK}{ff}")
    else:
        print(f"{MISS}找不到 ffmpeg（所有路线都需要）")
        print("        装好后加入 PATH，或设置 FFMPEG_PATH 指向 ffmpeg.exe")

    # ── 2. 核心依赖（QuickCut / 云端路线）──
    section("核心依赖（QuickCut 兜底路线）")
    core_ok = True
    for mod in ("edge_tts", "PIL", "numpy", "pydub", "yaml", "dotenv", "httpx"):
        ok, ver = has(mod)
        print(f"{OK if ok else MISS}{mod} {ver}")
        core_ok &= ok

    # ── 3. 本地模型依赖（LTX 路线）──
    section("本地模型依赖（--engine ltx）")
    ltx_ok = True
    for mod in ("torch", "diffusers", "transformers", "safetensors",
                "sentencepiece", "google.protobuf"):
        ok, ver = has(mod)
        print(f"{OK if ok else MISS}{mod} {ver}")
        ltx_ok &= ok
    if ltx_ok:
        import torch
        print(f"{OK}CUDA 可用：{torch.cuda.is_available()}"
              f"（本机为 False 属正常：AMD 核显无 CUDA，走 CPU 推理）")
    else:
        print("        补齐：pip install torch --index-url https://download.pytorch.org/whl/cpu")
        print("              pip install -r requirements-ltx.txt")

    # ── 4. 权重 ──
    section("LTX 权重（models/ltx）")
    mdir = ROOT / "models" / "ltx"
    needed = {
        "ltxv-2b-0.9.8-distilled-fp8.safetensors": 3_800,
        "vae/diffusion_pytorch_model.safetensors": 1_400,
        "text_encoder/model-00001-of-00004.safetensors": 4_000,
        "text_encoder/model-00002-of-00004.safetensors": 4_000,
        "text_encoder/model-00003-of-00004.safetensors": 4_000,
        "text_encoder/model-00004-of-00004.safetensors": 4_000,
        "transformer/config.json": 0,
        "tokenizer/spiece.model": 0,
        "scheduler/scheduler_config.json": 0,
    }
    weights_ok = mdir.exists()
    if not mdir.exists():
        print(f"{MISS}目录不存在：{mdir}")
        print("        下载见 README「权重下载」一节（约 21.5 GB）")
    else:
        for rel, min_mb in needed.items():
            p = mdir / rel
            if not p.exists():
                print(f"{MISS}{rel}  缺失")
                weights_ok = False
            else:
                mb = p.stat().st_size / 1048576
                bad = mb < min_mb
                if bad:
                    weights_ok = False
                print(f"{WARN if bad else OK}{rel}  {mb:.0f} MB"
                      + (f"  ← 偏小，期望 ≥{min_mb} MB，可能没下完" if bad else ""))
        total = sum(p.stat().st_size for p in mdir.rglob("*") if p.is_file())
        print(f"        合计 {total/1073741824:.2f} GB")

    # ── 5. 云端 key ──
    section("云端 provider")
    cloud_keys = ["ARK_API_KEY", "KLING_API_KEY", "MINIMAX_API_KEY", "FAL_KEY",
                  "FAL_AI_API_KEY", "VOLC_ACCESSKEY", "XAI_API_KEY", "GOOGLE_API_KEY"]
    env = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    found = [k for k in cloud_keys if env.get(k)]
    if found:
        print(f"{OK}已配置：{', '.join(found)}")
    else:
        print(f"{MISS}未配置任何云端 key（不影响本地路线）")
    if env.get("DEEPSEEK_API_KEY"):
        print(f"{OK}DEEPSEEK_API_KEY 已配置（AI 分镜可用）")
    else:
        print(f"{MISS}DEEPSEEK_API_KEY 未配置（AI 分镜不可用）")

    # ── 6. 结论 ──
    section("可用出片路线")
    quickcut = bool(ff) and core_ok
    ltx = bool(ff) and ltx_ok and weights_ok
    cloud = bool(ff) and core_ok and bool(found)
    print(f"{OK if quickcut else MISS}quickcut  ——零显卡零 key，永远可用（程序化底图）")
    print(f"{OK if ltx else MISS}ltx        ——本地 LTX-Video 2B，真 AIGC 画面（CPU 约 10-14 分钟/镜）")
    print(f"{OK if cloud else MISS}cloud      ——云端 provider，画质最好（按次计费）")

    if not (quickcut or ltx or cloud):
        print("\n一条路线都不可用，先补上面的缺失项。")
        return 1
    print("\n下一步：")
    print("  python scripts/shoot.py --demo                      # 30 秒验证链路")
    if ltx:
        print("  python scripts/shoot.py --demo --engine ltx          # 真 AIGC 画面")
        print("  （长任务记得 export CODEBUDDY_SAFE_DELETE_ENABLED=0，否则清理帧文件会被拦截）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
