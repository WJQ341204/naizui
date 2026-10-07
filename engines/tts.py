"""Edge-TTS 中文配音（本地免费、无需 key、无需显卡）。

设计要点：
- 同步 execute 用 asyncio.run 包一层，方便 to_thread 调用；
- 按文本 sha1 缓存，重复场景不重复发声；
- 音色可从环境变量 TTS_VOICE / TTS_RATE / TTS_PITCH 覆盖，默认云健（叙事感强）。

用法：
    from engines.tts import synthesize
    mp3 = await synthesize("叶玄机推开药铺的门，一股苦香扑面而来。", Path("a.mp3"))
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

# 常用中文音色（edge-tts 内置）
VOICES = {
    "云健": "zh-CN-YunjianNeural",      # 叙事/纪录片
    "云扬": "zh-CN-YunyangNeural",      # 新闻播报
    "晓晓": "zh-CN-XiaoxiaoNeural",     # 女声温柔
    "晓伊": "zh-CN-XiaoyiNeural",       # 女声活泼
    "云希": "zh-CN-YunxiNeural",        # 男声少年
}


def resolve_voice(voice: str | None = None) -> str:
    """把「晓晓」这类中文别名映射成 edge-tts 要的完整音色名。

    edge-tts 只认 `zh-CN-XiaoxiaoNeural` 这种全名，传「晓晓」会直接
    ValueError。别名表原先只在读环境变量 TTS_VOICE 时生效，导致
    `--voice 晓晓`（README 里就是这么写的）一跑就崩。这里统一收口：
    显式参数 > 环境变量 > 默认云健；已是全名则原样返回。
    """
    v = (voice or os.environ.get("TTS_VOICE", "")).strip()
    if not v:
        return VOICES["云健"]
    return VOICES.get(v, v)


def default_voice() -> str:
    """未显式指定音色时的默认值（环境变量优先）。"""
    return resolve_voice(None)


async def synthesize(text: str, out_path: Path,
                     voice: str | None = None,
                     rate: str | None = None,
                     pitch: str | None = None,
                     cache_dir: "Path | None" = None) -> Path:
    """把文本合成为 mp3。返回落盘路径。

    cache_dir: 给定时走内容寻址缓存（同名文本复用同一音频），
    让「CLI 预探测时长」与「引擎内渲染」共用一次网络请求。
    """
    import edge_tts
    import re

    def _clean(s: str) -> str:
        # edge-tts 对括号里的舞台提示会照读，去掉避免杂音
        s = re.sub(r"[（(][^）)]*[）)]", "", s)
        return s.strip()

    text = _clean(text)
    if not text:
        raise ValueError("TTS 文本为空")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 若目标已存在且内容一致则跳过（断点续跑）
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    # 内容寻址缓存命中（长片场景：同一句不重复请求云端 TTS）
    if cache_dir is not None:
        cached = Path(cache_dir) / f"{cache_key(text, voice)}.mp3"
        cached.parent.mkdir(parents=True, exist_ok=True)
        if cached.exists() and cached.stat().st_size > 0:
            import shutil
            shutil.copyfile(cached, out_path)
            return out_path

    v = resolve_voice(voice)
    cmd = edge_tts.Communicate(text, v)
    kw = {}
    if rate:
        kw["rate"] = rate
    if pitch:
        kw["pitch"] = pitch
    if kw:
        cmd = edge_tts.Communicate(text, v, **kw)
    await cmd.save(str(out_path))
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"TTS 生成失败：{out_path}")
    return out_path


def cache_key(text: str, voice: str | None = None) -> str:
    # 与 synthesize 用同一套别名解析，否则「晓晓」和 zh-CN-XiaoxiaoNeural
    # 会算出两个不同的缓存 key，同一句话重复请求云端 TTS。
    v = resolve_voice(voice)
    return hashlib.sha1(f"{v}|{text}".encode("utf-8")).hexdigest()[:16]
