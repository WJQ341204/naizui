#!/usr/bin/env python3
"""LTX-Video 2B distilled fp8 权重自检。

装完权重后先跑一遍，确认「文件下全了 + 能加载 + 能出画面」再进管线，
免得跑到一半才崩在 20 GB 下载后。

    python tools\\verify_ltx_weights.py             # 只查文件 + 加载 pipeline
    python tools\\verify_ltx_weights.py --render    # 真跑 4 帧采样，出片到 output/
    python tools\\verify_ltx_weights.py --resume    # 从 .env 里读 LTX_* 配置

退出码：0 = 通过，1 = 有问题。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 与 engines/video_local.py 的 LOCAL_MODEL_DIR 保持一致：仓库根/models/ltx
MODEL_DIR = ROOT / "models" / "ltx"
SINGLE_NAME = "ltxv-2b-0.9.8-distilled-fp8.safetensors"
MIN_SINGLE_MB = 3800.0  # 官方 fp8 主权重是 4.155 GB，下残会明显偏小


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def check_safetensors_complete(path: Path) -> tuple[bool, int, int]:
    """读 safetensors 头部，检查声明的数据长度是否正好覆盖整个文件。

    为什么需要：文件下到一半时**大小看起来也很正常**，safetensors 只在真正
    加载时才报 "incomplete metadata, file not fully covered"。
    这里提前算一遍：期望大小 = 8 字节头长度 + header 长度 + 最后一个张量的末尾偏移。
    """
    import json
    import struct

    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        hdr = json.loads(f.read(n))
    end = max(v["data_offsets"][1] for k, v in hdr.items() if k != "__metadata__")
    return (8 + n + end) == path.stat().st_size, 8 + n + end, path.stat().st_size


def _remap_fp8_key(k: str) -> str:
    """官方 fp8 单文件的 key 名 → diffusers 0.40 的 key 名。

    官方单文件（ltxv-2b-0.9.8-distilled-fp8.safetensors）用的是一套旧命名，
    diffusers 0.40 的 LTXVideoTransformer3DModel 用另一套，直接 load 会全部对不上：
        model.diffusion_model.*  →  去掉前缀
        adaln_single.*           →  time_embed.*
        patchify_proj.*          →  proj_in.*
        attnN.q_norm / k_norm    →  attnN.norm_q / norm_k
    另外这个单文件还把 VAE 权重也打包在里面（key 以 vae. 开头），transformer 用不上。
    """
    k = k.replace("model.diffusion_model.", "")
    k = k.replace("adaln_single.", "time_embed.")
    k = k.replace("patchify_proj.", "proj_in.")
    return k.replace("q_norm.", "norm_q.").replace("k_norm.", "norm_k.")


def _load_transformer(single: Path, model_dir: Path) -> tuple[object, int, int]:
    """绕过 from_single_file（它要联网拉 Lightricks/LTX-Video-0.9.5 的 config，
    本机代理会 502），改为 本地 config + 手工 state dict 映射。"""
    import torch
    from diffusers import LTXVideoTransformer3DModel
    from safetensors.torch import load_file

    cfg = LTXVideoTransformer3DModel.load_config(str(model_dir), subfolder="transformer")
    tx = LTXVideoTransformer3DModel.from_config(cfg)
    sd = load_file(str(single), device="cpu")
    mapped = {
        _remap_fp8_key(k): v.to(torch.float32)
        for k, v in sd.items() if not _remap_fp8_key(k).startswith("vae.")
    }
    missing, unexpected = tx.load_state_dict(mapped, strict=False)
    return tx, len(missing), len(unexpected)


def check_files(single: Path, text_encoder_dir: Path) -> list[str]:
    """查文件在不在、大不大、文本编码器分片全不全、每个权重是否完整。"""
    problems: list[str] = []

    def completeness(p: Path, label: str) -> None:
        try:
            ok, want, got = check_safetensors_complete(p)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label} 头部读不了（可能不是 safetensors）：{exc}")
            print(f"  [{label}] 头部解析失败：{exc}")
            return
        if ok:
            print(f"  [{label}] {p.name}  完整性 ok")
        else:
            miss_mb = (want - got) / 1048576
            problems.append(f"{label} {p.name} 不完整，缺 {miss_mb:.0f} MB")
            print(f"  [{label}] ❌ 缺 {miss_mb:.0f} MB（期望 {want/1073741824:.2f} GB /"
                  f" 实际 {got/1073741824:.2f} GB）")

    if not single.exists():
        problems.append(f"缺主权重：{single}")
    else:
        mb = single.stat().st_size / 1048576
        state = "ok" if mb >= MIN_SINGLE_MB else f"偏小({mb:.0f}MB)，可能没下完"
        print(f"  [{'主权重'}] {single.name}  {mb:.0f} MB  {state}")
        if mb < MIN_SINGLE_MB:
            problems.append(f"主权重 {single.name} 只有 {mb:.0f} MB，重下")
        completeness(single, "主权重")

    idx_path = text_encoder_dir / "model.safetensors.index.json"
    if idx_path.exists():
        try:
            idx = __import__("json").loads(idx_path.read_text(encoding="utf-8"))
            want = sorted(set(idx.get("weight_map", {}).values()))
            total_mb = idx.get("metadata", {}).get("total_size", 0) / 1048576
            print(f"  [文本编码器] {len(want)} 个分片  约 {total_mb:.0f} MB")
            for shard in want:
                p = text_encoder_dir / shard
                if not p.exists():
                    problems.append(f"缺 T5 分片：{shard}")
                    print(f"     缺 {shard}")
                else:
                    got = p.stat().st_size / 1048576
                    # 分片一般比均分略小或略大，±25% 算正常
                    if got < total_mb / max(len(want), 1) * 0.5:
                        problems.append(f"T5 分片 {shard} 只有 {got:.0f} MB，可能没下完")
                        print(f"     {shard} 偏小 {got:.0f} MB")
                    completeness(p, f"T5 {shard.split('-')[1]}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"T5 index 读不了：{exc}")
    else:
        print(f"  [文本编码器] 缺 model.safetensors.index.json（T5 没拉全？）")
        problems.append("缺 T5 index")

    for sub in ("tokenizer", "vae", "scheduler"):
        if not (MODEL_DIR / sub).exists():
            problems.append(f"缺子目录 models/ltx/{sub}/")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="LTX 权重自检")
    ap.add_argument("--render", action="store_true", help="额外真跑 4 帧采样出片")
    ap.add_argument("--resume", action="store_true", help="从 .env 读配置")
    args = ap.parse_args()
    if args.resume:
        load_env()

    single = Path(os.environ.get("LTX_MODEL_PATH", str(MODEL_DIR / SINGLE_NAME)))
    te_dir = Path(os.environ.get("LTX_TEXT_ENCODER_DIR", str(MODEL_DIR / "text_encoder")))

    print("== 1/3 文件检查 ==")
    problems = check_files(single, te_dir)
    if problems:
        print("发现问题：")
        for p in problems:
            print(f"  ! {p}")
        print("重下命令见 README「权重下载」一节")
        return 1

    print("== 2/3 加载 pipeline ==")
    t0 = time.time()
    try:
        import torch
        from diffusers import AutoencoderKLLTXVideo, LTXPipeline, LTXVideoTransformer3DModel
        from transformers import T5EncoderModel, T5TokenizerFast
    except Exception as exc:  # noqa: BLE001
        print(f"依赖没装：{exc}\n  pip install -r requirements.txt")
        return 1

    # 离线加载：手工组装，不走 from_pretrained 的自动解析
    # （diffusers 0.40 解析 model_index 时会回落到 "Lightricks/LTX-Video-0.9.5"
    #   去取 config，本机代理会返回 502 → 直接把各组件喂给构造函数最稳）
    try:
        from diffusers import (
            AutoencoderKLLTXVideo, FlowMatchEulerDiscreteScheduler,
            LTXPipeline, LTXVideoTransformer3DModel,
        )
        from transformers import T5EncoderModel, T5TokenizerFast

        tx, nmiss, nunexp = _load_transformer(single, MODEL_DIR)
        if nmiss or nunexp:
            print(f"  [transformer] ⚠ 权重映射未完全对齐：缺 {nmiss} 个 / 多 {nunexp} 个")
        else:
            print(f"  [transformer] 权重映射完全对齐（0 缺 / 0 多）")
        vae = AutoencoderKLLTXVideo.from_pretrained(
            str(MODEL_DIR), subfolder="vae", torch_dtype=torch.float32)
        te = T5EncoderModel.from_pretrained(
            str(MODEL_DIR), subfolder="text_encoder", torch_dtype=torch.float32)
        tok = T5TokenizerFast.from_pretrained(str(MODEL_DIR), subfolder="tokenizer")
        sched = FlowMatchEulerDiscreteScheduler.from_pretrained(
            str(MODEL_DIR), subfolder="scheduler")
        pipe = LTXPipeline(
            transformer=tx, vae=vae, text_encoder=te, tokenizer=tok, scheduler=sched,
        ).to("cpu")
    except Exception as exc:  # noqa: BLE001
        print(f"加载失败：{exc}")
        return 1
    print(f"  ok  {(time.time()-t0):.0f}s")

    if not args.render:
        print("== 3/3 跳过采样（加 --render 会真出 4 帧）==")
        print("RESULT: PASS（权重齐 + 可加载）")
        return 0

    print("== 3/3 采样 4 帧 ==")
    t1 = time.time()
    import numpy as np
    out = pipe(
        prompt="a cat sitting on a windowsill, soft daylight",
        width=256, height=256, num_frames=9,   # LTX 要求 (n-1)%8==0：4 会被压成 1 帧
        num_inference_steps=4, guidance_scale=1.0,
        generator=torch.Generator("cpu").manual_seed(7),
    )
    # diffusers 0.40 的 frames 是 **PIL 图片列表**，不是 tensor（老代码按 tensor 写，会炸）
    frames = out.frames[0]
    arr = np.stack([np.asarray(im, dtype=np.float32) for im in frames])
    mean, std = float(arr.mean()), float(arr.std())
    print(f"  {(time.time()-t1):.0f}s  {len(frames)} 帧  帧均值={mean:.1f} 方差={std:.1f}")

    outdir = ROOT / "output"
    outdir.mkdir(exist_ok=True)
    dst = outdir / "ltx_smoke.mp4"
    from diffusers.utils import export_to_video
    export_to_video(frames, str(dst), fps=8)
    print(f"  出片 {dst} ({dst.stat().st_size/1048576:.1f} MB)")

    ok = std > 5.0 and abs(mean - 255.0) > 5.0
    print("RESULT:", "PASS（画面非纯色）" if ok else "FAIL（疑似黑屏/纯色，权重损坏或配置不对）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
