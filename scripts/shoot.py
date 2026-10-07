"""**出片入口（零门槛）**：小说/对白 → 配音 → 画面 → 成片 mp4。

与 pipeline 的区别：本脚本不强制要求 API key 或显卡，
`--engine quickcut` 下只用「Edge-TTS + ffmpeg + PIL」出片，
在任何 Windows 机器上都能跑完；把 key 接上后可自动升级为 AIGC 画质。

用法：
    # 用包内示例故事出片（免输入）
    python scripts/shoot.py --demo --out output/film_demo.mp4

    # 用自己的小说正文
    python scripts/shoot.py --novel 我的小说.txt --out output/film.mp4

    # 用包内既有的分镜对白（story2/dialogue.json）
    python scripts/shoot.py --dialogue story2/dialogue.json --out output/film.mp4

    # 强制走云端（需 DEEPSEEK/ARK/KLING 等 key）
    python scripts/shoot.py --demo --engine cloud

引擎链（auto）：
    cloud(填 key) → comfyui(有 N 卡) → **quickcut(永远可用)**
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engines.base import GenerateRequest, resolve_ffmpeg            # noqa: E402
from engines.quickcut import QuickCutEngine, probe_duration  # noqa: E402
from engines.tts import cache_key, synthesize, default_voice  # noqa: E402
from post.compose import burn_subtitles, concat_xfade  # noqa: E402
from post.subtitles import Timing, to_ass           # noqa: E402

DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 1080, 1920, 30
XFADE_DUR = 1.0          # 与 post/compose._xfade_chain 硬编码的过渡时长对齐
NARRATION_PREFIX = "旁白："   # 仅为字幕可读，不进 TTS


# ─── 分镜规划 ────────────────────────────────────────────────
def plan_from_dialogue(path: Path) -> list[dict]:
    """story2/dialogue.json → 场景列表（每场景 = 若干句台词）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    scenes = []
    for sid, lines in (data.get("scenes") or {}).items():
        texts, styles = [], []
        for line in lines:
            text = str(line.get("text", "")).strip()
            if not text:
                continue
            texts.append({"text": text, "style": "Narr" if line.get("speaker") == "narration" else "Default"})
        if not texts:
            continue
        # 配音正文：对白 + 旁白（旁白在后，叙事收尾）
        story = " ".join(t["text"] for t in texts)
        # 画面提示：从 shots.json 取该场景的 emotion（若无则留空走程序化底图）
        scenes.append({
            "id": int(sid),
            "narration": story,
            "lines": texts,
            "prompt": "",
        })
    return scenes


# 章节/标题行：被 TTS 念出来会很难听，切句前直接剔除
_CHAPTER_RE = re.compile(
    r"^\s*(?:第[0-9一二三四五六七八九十百千零两]+[章節节卷回话集篇幕]"
    r"|序[章言]?|序言|楔子|引子|尾声|后记|番外|番外篇|前言|结尾)[^\n]{0,12}$")


LONG_SENTENCE = 70      # 超过这个长度的长句才按逗号拆
MIN_FRAGMENT = 14       # 拆出来的碎片短于这个就并回上一句


def _merge_short(parts: list[str], min_len: int = MIN_FRAGMENT) -> list[str]:
    """把过短的碎片并回上一句。

    直接按逗号切会产生「同样的台灯，」「唯一的区别是，」这类 6~8 字碎片，
    单独配音念出来非常零碎、没有语流。这里从第二片起，短于 min_len 的一律
    并进前一片，保证每个镜头至少是一个能连读的意群。
    """
    out: list[str] = []
    for p in parts:
        if out and len(p) < min_len:
            out[-1] = out[-1] + p
        else:
            out.append(p)
    # 首片过短时并到下一片：「外面不是楼层。」这种开场短语单独成一个镜头太碎
    if len(out) >= 2 and len(out[0]) < min_len:
        out = [out[0] + out[1]] + out[2:]
    return out


def split_sentences(text: str) -> list[str]:
    """小说正文 → 句子（保留标点，剔除章节标题，合并过短句，长句按逗号断）。"""
    text = re.sub(r"\s*#[^\n]*", "", text)              # 去 Markdown 标题行#
    text = re.sub(r"\n{2,}", "\n", text)
    raw: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if _CHAPTER_RE.match(para) or para.startswith("#"):
            continue
        raw += [s for s in re.split(r"(?<=[。！？])", para) if s.strip()]
    merged: list[str] = []
    for s in raw:
        if len(s) < 8 and merged:
            merged[-1] = merged[-1] + s
        else:
            merged.append(s)
    out: list[str] = []
    for s in merged:
        if len(s) > LONG_SENTENCE:
            parts = _merge_short([p for p in re.split(r"(?<=，)", s) if p])
            # 每片都以句读收尾：逗号结尾让 TTS 念到一半就停，听感像话没说完。
            # 同时保证 split_sentences 的不变式「每句以 。！？ 结尾」始终成立。
            out += [p[:-1] + "。" if p.endswith("，") else p for p in parts]
        else:
            out.append(s)
    return [s for s in (x.strip() for x in out) if s]


def _load_keyframes(spec: str) -> list[Path]:
    """`--images` 支持目录或单文件，按名称排序返回图片列表。

    目录里图片多于场景时会循环复用，少于场景则最后一���反复用。
    """
    p = Path(spec)
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise SystemExit(f"--images 指向的路径不存在：{p}")
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    imgs = sorted([f for f in p.iterdir() if f.suffix.lower() in exts])
    if not imgs:
        raise SystemExit(f"目录里没有图片：{p}")
    return imgs


def plan_from_text(text: str, max_scenes: int = 0) -> list[dict]:
    """小说正文 → 场景（每句一场景）。"""
    sents = split_sentences(text)
    if max_scenes and len(sents) > max_scenes:
        # 等分合并成 max_scenes 段
        step = math.ceil(len(sents) / max_scenes)
        sents = ["".join(sents[i:i + step]) for i in range(0, len(sents), step)]
    return [
        {"id": i + 1, "narration": s.strip(), "lines": [{"text": s.strip(), "style": "Narr"}], "prompt": ""}
        for i, s in enumerate(sents)
    ]


def load_demo() -> list[dict]:
    """包内示例故事（《被辞退那天，我写的代码卖了八千万》）。"""
    for cand in ("story2/dialogue.json", "story2/shots.json"):
        p = PROJECT_ROOT / cand
        if p.exists():
            return plan_from_dialogue(p)
    return []


# ─── 引擎构建 ────────────────────────────────────────────────
def build_engine(kind: str, voice: str | None, zoom: float):
    if kind == "quickcut":
        return QuickCutEngine(voice=voice, zoom=zoom)
    if kind == "cloud":
        from engines.cloud import CloudEngine
        return CloudEngine()
    if kind == "ltx":
        # 本地视频模型：LTX-Video 2B distilled fp8（CPU 推理，出真画面）
        from engines.video_local import LTXVideoEngine
        return LTXVideoEngine()
    if kind in ("h3", "h3ref"):
        # MiniMax-H3（30B + Qwen3-VL-32B，需 GPU，ComfyUI 管线）
        # h3ref 走参考图链路 R2V，h3 走首尾帧链路 FL2VA
        from engines.h3_local import H3Engine
        return H3Engine(reference=(kind == "h3ref"))
    # auto：走统一路由（云端 → 本地 → QuickCut 兜底）
    from engines.registry import EngineRouter
    return EngineRouter(video_mode="auto", local_kind="quickcut")


# ─── 主流程 ──────────────────────────────────────────────────
async def shoot(scenes: list[dict], out: Path, engine_kind: str, voice: str | None,
                zoom: float, width: int, height: int, fps: int,
                transition: str, images: str | None = None) -> dict:
    work = out.parent / ".work"
    work.mkdir(parents=True, exist_ok=True)
    tts_cache = work / ".tts_cache"
    engine = build_engine(engine_kind, voice, zoom)
    if not engine.is_available():
        raise SystemExit(f"引擎不可用：{engine_kind}（QuickCut 需要 ffmpeg 在 PATH 或 FFMPEG_PATH）")

    clips: list[Path] = []
    subtitle_events: list[tuple[Timing, str, str]] = []
    cursor = 0.0
    engine_used = ""
    keyframes = _load_keyframes(images) if images else []
    if keyframes:
        print(f"关键帧素材：{len(keyframes)} 张（按顺序分配给各场景）")

    for i, scene in enumerate(scenes, 1):
        text = (scene.get("narration") or "").strip()
        if not text:
            continue
        # 1) 预生成音频（带缓存）以锁定本段时长
        audio = tts_cache / f"{cache_key(text, voice)}.mp3"
        await synthesize(text, audio, voice=voice, cache_dir=tts_cache)
        audio_dur = probe_duration(audio) or 4.0
        duration = max(3.0, round(audio_dur + 0.45, 2))

        req = GenerateRequest(
            prompt=scene.get("prompt") or text,
            narration=text,
            width=width, height=height, fps=fps,
            duration_seconds=duration,
            output_dir=work, output_name=f"scene_{scene['id']:02d}",
            task_id=f"s{scene['id']}",
            first_frame=keyframes[(i - 1) % len(keyframes)] if keyframes else None,
        )
        print(f"[{i}/{len(scenes)}] 场景 {scene['id']}: 配音 {audio_dur:.1f}s → 出片 {duration:.1f}s"
              + (f"（首帧 {req.first_frame.name}）" if req.first_frame else ""),
              flush=True)
        clip = await engine.generate(req)
        engine_used = clip.engine or engine_used
        clips.append(clip.video_path)

        # 2) 字幕事件：句内按字符权重分摊该段时长
        n = len(scene["lines"]) or 1
        weight_total = max(1, sum(len(l["text"]) for l in scene["lines"]))
        pos = cursor
        for line in scene["lines"]:
            w = len(line["text"]) / weight_total
            start = pos
            # 注意：end 必须基于「本句起始 + 本句时长」，不能基于段起点 cursor，
            # 否则一句内第 2 句之后的 end 会小于 start（ASS 直接吞掉该行）。
            end = pos + duration * w
            subtitle_events.append((Timing(start, end), line["text"], line.get("style", "Default")))
            pos = end
        # xfade 过渡会吞掉下一段开头 1s，字幕起点顺延
        cursor += duration - (XFADE_DUR if i < len(scenes) else 0)

    if not clips:
        raise SystemExit("没有可出片的场景")

    # 3) 拼接
    concat = work / "_concat.mp4"
    print("[compose] 拼接转场…", flush=True)
    concat = await concat_xfade(clips, concat, transition=transition)

    # 4) 字幕
    total = probe_duration(concat)
    texts = [t for _, t, _ in subtitle_events]
    timings = []
    # 窗口封顶：下一条字幕开始即本条结束，否则 xfade 重叠区会出现两行同屏
    for idx, (tim, _, _style) in enumerate(subtitle_events):
        limit = total
        if idx + 1 < len(subtitle_events):
            limit = subtitle_events[idx + 1][0].start
        end = min(tim.end + 0.15, limit)
        timings.append(Timing(tim.start, max(end, tim.start + 0.3)))
    styles = [s for _, _, s in subtitle_events]
    ass = to_ass(timings, texts, styles, width=width, height=height)
    ass_path = out.parent / f"{out.stem}.ass"
    ass_path.write_text(ass, encoding="utf-8")

    print("[compose] 烧录字幕…", flush=True)
    final = await burn_subtitles(concat, out, texts, timings, styles)

    manifest = {
        "out": str(final),
        "engine": engine_used,
        "scenes": len(clips),
        "subtitle_events": len(texts),
        "duration_seconds": round(probe_duration(final), 2),
        "subtitle_file": str(ass_path),
        "clips": [str(c) for c in clips],
        "timings": [[round(t.start, 2), round(t.end, 2)] for t in timings],
        "texts": texts,
    }
    (out.parent / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="小说 → 成片 mp4（QuickCut 本地兜底 / 云端 AIGC）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--novel", help="小说正文 .txt/.md 文件")
    src.add_argument("--dialogue", help="对白 JSON（story2/dialogue.json 格式）")
    src.add_argument("--demo", action="store_true", help="用包内示例故事")
    ap.add_argument("--out", default="output/film.mp4", help="成片输出路径")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "quickcut", "ltx", "h3", "h3ref", "cloud"])
    ap.add_argument("--max-scenes", type=int, default=0, help="最多出几个镜头（默认全量）")
    ap.add_argument("--voice", default=None, help="TTS 音色：云健/云扬/晓晓/晓伊/云希 或 edge-tts 音色名")
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--zoom", type=float, default=0.0006, help="推镜速度")
    ap.add_argument("--images", default=None,
                    help="关键帧素材目录或单张图片：按顺序作为各场景首帧，"
                         "quickcut 做推镜、ltx/wan5b 做图生视频")
    ap.add_argument("--transition", default="fade", help="转场：fade / wipeleft / slideup …")
    args = ap.parse_args()

    import os

    # 护栏：默认 1080x1920 是给 QuickCut（程序化底图，很快）设的。
    # 用扩散模型（ltx/h3）还跑这个尺寸的话，CPU 上要几小时、8GB 显存直接爆。
    # 只在用户没显式指定尺寸时才自动降到 512x768，并提示一句。
    if args.engine in ("ltx", "h3", "h3ref") and args.width > 768 and args.height > 1024:
        print(f"[提示] --engine {args.engine} 在 {args.width}x{args.height} 下会非常慢"
              f"（CPU 数小时 / 8GB 显存会 OOM），自动降为 512x768。"
              f"想要原尺寸请显式指定 --width/--height。", flush=True)
        args.width, args.height = 512, 768

    # ffmpeg 统一走 engines.base.resolve_ffmpeg()：FFMPEG_PATH 可写 exe 路径或所在目录。
    # 解析成功后把 exe 所在目录补进 PATH —— 这样脚本里其它裸调 ffmpeg 的地方也能工作。
    _ff = resolve_ffmpeg()
    if _ff is None:
        print(
            "未找到 ffmpeg。请安装后加入 PATH，或设置 FFMPEG_PATH"
            "（可写 ffmpeg.exe 全路径，也可写它所在的目录）",
            file=sys.stderr,
        )
        return 2
    _ff_dir = str(Path(_ff).parent)
    if _ff_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{_ff_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    if args.demo:
        scenes = load_demo()
        title = "示例故事：被辞退那天，我写的代码卖了八千万"
    elif args.dialogue:
        scenes = plan_from_dialogue(Path(args.dialogue))
        title = Path(args.dialogue).stem
    else:
        raw = Path(args.novel).read_text(encoding="utf-8", errors="replace")
        scenes = plan_from_text(raw, args.max_scenes or 0)
        title = Path(args.novel).stem
    if not scenes:
        print("没有可出片的内容", file=sys.stderr)
        return 2
    if args.max_scenes:
        scenes = scenes[: args.max_scenes]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"分镜：{len(scenes)} 个场景 | 引擎：{args.engine} | 音色：{args.voice or default_voice()}")
    t0 = time.time()
    manifest = asyncio.run(shoot(scenes, out, args.engine, args.voice, args.zoom,
                                 args.width, args.height, args.fps, args.transition,
                                 args.images))
    print(f"\n成片：{manifest['out']}")
    print(f"时长：{manifest['duration_seconds']}s | 引擎：{manifest['engine']} | 耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
