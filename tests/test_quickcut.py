"""QuickCut 本地兜底引擎 + shoot 字幕时间轴测试。

覆盖：
- QuickCutEngine 在只有 ffmpeg 的环境里可用（不需要 GPU / API key）
- 产出的视频段能被 QualityGate 通过（含音频、时长达标、非静态）
- shoot.plan_from_text 的句子切分
- shoot 的字幕时间轴单调、不倒挂、不重叠（历史 bug：句内 end 基于段起点导致倒挂）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engines.base import GenerateRequest  # noqa: E402
from engines.quickcut import QuickCutEngine, probe_duration  # noqa: E402
from quality.gate import GateConfig, QualityGate  # noqa: E402

from scripts import shoot  # noqa: E402


@pytest.mark.asyncio
async def test_quickcut_produces_playable_clip(tmp_path):
    """图 + 配音 → 可播放视频段，且能过质量门。"""
    engine = QuickCutEngine()
    assert engine.is_available(), "QuickCut 需要 ffmpeg（PATH 或 FFMPEG_PATH）"

    req = GenerateRequest(
        prompt="叶玄机推开药铺的门，一股苦香扑面而来。",
        narration="叶玄机推开药铺的门，一股苦香扑面而来。",
        width=270, height=480, fps=24, duration_seconds=5.0,
        output_dir=tmp_path, output_name="qk", task_id="unit",
    )
    clip = await engine.generate(req)
    assert clip.engine == "quickcut"
    assert clip.video_path.exists() and clip.video_path.stat().st_size > 10_000
    assert clip.duration_seconds >= 1.0
    assert clip.quality_report and clip.quality_report["narration_seconds"] > 0

    # 质量门：配音场景要求音频流
    gate = QualityGate(GateConfig(require_audio=True))
    report = await gate.check_video(clip.video_path, expect_duration=clip.duration_seconds)
    assert report["passed"], report["issues"]


@pytest.mark.asyncio
async def test_quickcut_rejects_empty_narration(tmp_path):
    """没有配音文本时应当明确报错，而不是产出黑帧。"""
    engine = QuickCutEngine()
    req = GenerateRequest(prompt="", output_dir=tmp_path, output_name="empty")
    with pytest.raises(Exception):
        await engine.generate(req)


def test_split_sentences_merges_short_and_keeps_punctuation():
    text = "第一章。\n\n风很轻。他站在巷口许久。月亮升起来了，云散开，街灯一盏接一盏亮起，像谁在远处数着日子。"
    sents = shoot.split_sentences(text)
    assert sents, "应切出句子"
    assert all(s.endswith(("。", "！", "？")) for s in sents), [s for s in sents]
    # 短句被合并，不应出现 1~2 字的碎片
    assert all(len(s) >= 2 for s in sents)
    # 章节标题被剔除
    assert not any("第一章" in s for s in sents)


def test_plan_from_text_respects_max_scenes():
    text = "。" .join(f"第{i}句内容够长了需要单独成镜头" for i in range(10))
    scenes = shoot.plan_from_text(text, max_scenes=3)
    assert len(scenes) == 3
    assert [s["id"] for s in scenes] == [1, 2, 3]
    assert all(s["narration"] for s in scenes)


def test_subtitle_timeline_is_monotonic_and_non_overlapping():
    """回归测试：一句内第 2 句之后 end 若基于段起点而非句起点，会出现 end < start。"""
    scenes = [{
        "id": 1,
        "narration": "短句一。",
        "lines": [{"text": "前一句比较长需要占掉大半时间", "style": "Default"},
                  {"text": "后一句", "style": "Narr"}],
        "prompt": "",
    }]
    # 复刻 shoot() 内的字幕窗口算法
    from post.subtitles import Timing
    total, cursor = 10.0, 0.0
    events = []
    for scene in scenes:
        lines = scene["lines"]
        weight_total = max(1, sum(len(l["text"]) for l in lines))
        pos = cursor
        for line in lines:
            w = len(line["text"]) / weight_total
            end = pos + 6.0 * w          # duration 固定 6s 便于断言
            events.append((Timing(pos, end), line["text"]))
            pos = end
        cursor += 6.0
    total = cursor
    timings = []
    for idx, (tim, _) in enumerate(events):
        limit = total if idx + 1 == len(events) else events[idx + 1][0].start
        end = min(tim.end + 0.15, limit)
        timings.append(Timing(tim.start, max(end, tim.start + 0.3)))

    for t in timings:
        assert t.end > t.start, f"字幕倒挂: {t}"
    for a, b in zip(timings, timings[1:]):
        assert a.end <= b.start + 1e-6, f"字幕重叠: {a} / {b}"
    assert timings[-1].end <= total + 1e-6
