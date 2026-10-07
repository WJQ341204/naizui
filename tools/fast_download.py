#!/usr/bin/env python3
"""多源 + 多线程分段下载器（针对 HuggingFace 权重这类 GB 级单文件）。

为什么不用 curl 直接下
----------------------
实测本机（2026-10）两条通道的脾气完全不同：

| 通道 | 单连接 | 说明 |
|---|---|---|
| hf-mirror.com | 约 28 KB/s | 多文件并发时总吞吐可到 6.5 MB/s，**单文件单连接被限速** |
| modelscope.cn | 约 1.0 MB/s | 按 IP 限速，同文件加并发也上不去，但**和 hf-mirror 是两条独立通道，可叠加** |

所以加速的三个正确手段，本脚本都实现了：
1. **同一文件切段多线程** —— 绕开单连接限速；
2. **多源故障切换** —— 某个源限速/挂了就换另一个；
3. **分段级断点续传** —— 中断后只补没下完的段。

用法
----
    # HuggingFace（走 hf-mirror，失败自动试 ModelScope 同名仓库）
    python tools\\fast_download.py --repo hf:Comfy-Org/MiniMax-H3 \\
        --file diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors \\
        --out models\\minimax-h3\\transformer_fp8.safetensors --threads 8

    # ModelScope 原生仓库
    python tools\\fast_download.py --repo ms:Comfy-Org/MiniMax-H3 \\
        --file vae/minimax_h3_audio_vae_fp32.safetensors --out ... --threads 4

    # 指定多个候选源，依次尝试（第一个能用就锁定）
    --source https://hf-mirror.com/ORG/REPO/resolve/main/FILE \\
    --source https://www.modelscope.cn/api/v1/models/ORG/REPO/repo?Revision=master&FilePath=FILE

**已存在的部分文件会被自动接管**（按「连续前缀」假设续传），
所以可以直接接管 `curl -C -` 下到一半的文件，不用重下。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# 常见仓库的候选源：同一个文件在两边都试
SOURCE_TEMPLATES = {
    "hf": "https://hf-mirror.com/{repo}/resolve/main/{path}",
    "ms": "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={path}",
}
# ModelScope 的常见仓库别名（HF 仓库名 → ModelScope 仓库名）
MS_REPO_ALIAS = {
    "Comfy-Org/MiniMax-H3": "Comfy-Org/MiniMax-H3",
    "Lightricks/LTX-Video": "Lightricks/LTX-Video",
}


def build_sources(repo: str | None, path: str, extra: list[str]) -> list[str]:
    urls: list[str] = []
    for u in extra or []:
        urls.append(u.replace("{path}", path).replace("{repo}", repo or ""))
    if repo:
        kind, _, name = repo.partition(":")
        kind = kind or "hf"
        urls.append(SOURCE_TEMPLATES.get(kind, SOURCE_TEMPLATES["hf"]).format(repo=name, path=path))
        # 同一仓库在另一个源上也试一下（ModelScope 限速更低但更稳）
        other = "ms" if kind != "ms" else "hf"
        alias = MS_REPO_ALIAS.get(name, name)
        urls.append(SOURCE_TEMPLATES[other].format(repo=alias, path=path))
    return [u for u in urls if u]


def http_size(url: str, timeout: int = 20) -> tuple[int, bool]:
    """返回 (文件总大小, 是否支持 Range)。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        size = int(r.headers.get("Content-Length") or 0)
        ranges = (r.headers.get("Accept-Ranges") or "").lower()
        # ModelScope 的 HEAD 不一定给 Content-Length，退化为请求前 1 字节
        if size == 0:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=timeout) as r2:
                cr = r2.headers.get("Content-Range") or ""
                size = int(cr.split("/")[-1]) if "/" in cr else 0
                ranges = "bytes" if r2.status == 206 else ranges
        return size, ("bytes" in ranges or ranges == "")


class Progress:
    def __init__(self, total: int, label: str):
        self.total, self.label = total, label
        self.done, self.t0, self.last = 0, time.time(), 0.0

    def add(self, n: int) -> None:
        self.done += n
        now = time.time()
        if now - self.last < 0.6:
            return
        self.last = now
        el = now - self.t0
        pct = self.done * 100 / self.total if self.total else 0
        rate = self.done / el / 1048576 if el else 0
        eta = (self.total - self.done) / (self.done / el) / 60 if self.done and el else 0
        sys.stdout.write(
            f"\r  [{self.label}] {pct:5.1f}%  {self.done/1073741824:5.2f}/{self.total/1073741824:.2f} GB"
            f"  {rate:4.1f} MB/s  ETA {eta:5.1f} min"
        )
        sys.stdout.flush()


def download_segment(url: str, seg_path: Path, start: int, end: int, prog: Progress,
                     stop: threading.Event) -> int:
    """下载 [start, end] 到 seg_path（追加）。返回本段新增字节数。"""
    have = seg_path.stat().st_size if seg_path.exists() else 0
    if start + have > end:
        return 0
    s = start + have
    if s > end:
        return 0
    for attempt in range(6):
        if stop.is_set():
            return 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes={s}-{end}"})
            with urllib.request.urlopen(req, timeout=60) as r, open(seg_path, "ab") as f:
                while True:
                    if stop.is_set():
                        return 0
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    prog.add(len(chunk))
                    s += len(chunk)
            return s - (start + have)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt == 5:
                raise
            time.sleep(1.5 * (attempt + 1))          # 退避重试
            have = seg_path.stat().st_size if seg_path.exists() else 0
            s = start + have
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="多源多线程分段下载器（大文件加速）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="hf:ORG/REPO 或 ms:ORG/REPO（会同时尝试另一源）")
    ap.add_argument("--file", required=True, help="仓库内相对路径")
    ap.add_argument("--out", required=True, help="本地输出路径")
    ap.add_argument("--threads", type=int, default=8, help="分段并发数（默认 8）")
    ap.add_argument("--source", action="append", default=[], help="额外候选源 URL，可多次")
    ap.add_argument("--url", help="直接给完整 URL（优先于 --repo/--source）")
    ap.add_argument("--keep-parts", action="store_true", help="合并后保留分段文件")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    urls = [args.url] if args.url else build_sources(args.repo, args.file, args.source)
    if not urls:
        print("至少要给 --url 或 --repo", file=sys.stderr)
        return 2

    # 选源：拿到能报出大小的那个
    url, total = None, 0
    for u in urls:
        try:
            size, _ = http_size(u)
            print(f"[src] {u}\n      大小 {size/1073741824:.2f} GB")
            if size > 0:
                url, total = u, size
                break
        except Exception as e:                       # noqa: BLE001
            print(f"[skip] {u[:80]}… {e}")
    if not url:
        print("所有候选源都取不到文件大小", file=sys.stderr)
        return 2

    # 接管已有部分文件（连续前缀假设）
    done_prefix = out.stat().st_size if out.exists() else 0
    if done_prefix > total:
        print(f"本地文件比远程还大（{done_prefix} > {total}），疑似损坏，删掉重下", file=sys.stderr)
        out.unlink()
        done_prefix = 0

    threads = max(1, min(args.threads, 32))
    seg_size = (total - done_prefix + threads - 1) // threads
    parts_dir = out.with_suffix(out.suffix + ".parts")
    parts_dir.mkdir(exist_ok=True)

    print(f"[dl ] 已有 {done_prefix/1073741824:.2f} GB，剩 {(total-done_prefix)/1073741824:.2f} GB"
          f"，{threads} 线程分段")
    prog = Progress(total - done_prefix, out.name)
    stop = threading.Event()
    results: list[Path] = []
    errors: list[Exception] = []

    def worker(i: int) -> None:
        start = done_prefix + i * seg_size
        if start >= total:
            return
        end = min(start + seg_size - 1, total - 1)
        seg = parts_dir / f"seg{i:03d}"
        try:
            download_segment(url, seg, start, end, prog, stop)
            results.append(seg)
        except Exception as e:                       # noqa: BLE001
            errors.append(e)
            stop.set()

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(threads)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    sys.stdout.write("\n")

    if errors:
        print(f"[fail] {errors[0]}（已下部分保留，重跑本命令即可续）", file=sys.stderr)
        return 1

    # 校验每段大小后按序合并
    ok = True
    for i in range(threads):
        start = done_prefix + i * seg_size
        if start >= total:
            break
        end = min(start + seg_size - 1, total - 1)
        seg = parts_dir / f"seg{i:03d}"
        want = end - start + 1
        got = seg.stat().st_size if seg.exists() else 0
        if got != want:
            print(f"[bad ] seg{i:03d} 期望 {want} 实际 {got}", file=sys.stderr)
            ok = False
    if not ok:
        print("[fail] 分段大小校验不过，重跑会只补缺的部分", file=sys.stderr)
        return 1

    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "wb") as dst:
        if done_prefix:
            with open(out, "rb") as src:
                shutil.copyfileobj(src, dst, 1 << 20)
        for i in range(threads):
            seg = parts_dir / f"seg{i:03d}"
            if not seg.exists():
                continue
            with open(seg, "rb") as src:
                shutil.copyfileobj(src, dst, 1 << 20)
    tmp.replace(out)
    if not args.keep_parts:
        shutil.rmtree(parts_dir, ignore_errors=True)

    el = time.time() - t0
    print(f"[ok ] {out}  {out.stat().st_size/1073741824:.2f} GB  用时 {el/60:.1f} 分钟"
          f"  平均 {(total-done_prefix)/max(el,1)/1048576:.1f} MB/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
