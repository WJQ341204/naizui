# -*- coding: utf-8 -*-
"""阶跃星辰 (StepFun) TTS 接入测试 — test_stepfun_tts.py (v12.3)

用法:
  1. 在 .env 填入 STEPFUN_API_KEY 后运行:
       python test_stepfun_tts.py            # 含真实云端合成（消耗额度）
  2. 未配 Key 也可运行:
       python test_stepfun_tts.py --dry      # 仅验证映射/分段/引擎选择逻辑

测试项:
  A. 长文本按句分段（官方 input ≤1000 字符限制）
  B. Edge 音色 → 阶跃星辰官方音色 双向映射
  C. 情绪 → instruction 指令生成
  D. 引擎自动选择（stepfun > cosyvoice > edge）
  E. 真实短文本合成（含情绪指导）
  F. 长文本分段合成 + ffmpeg 拼接
  G. 失败回退链（假 Key → 401 → 自动回退 Edge 出声）
"""
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

import main  # noqa: E402  (复用 main.py 中的 TTS 函数与常量)

OUT = ROOT / "output" / "_stepfun_test"
OUT.mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}  {detail}")
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1


async def run(dry: bool):
    print("=" * 62)
    print("阶跃星辰 TTS 接入测试 (test_stepfun_tts)")
    print("=" * 62)

    # A. 文本分段
    long_text = "夜色如墨，寒风呼啸，他独自走在青石巷中。" * 60  # ~810 字
    chunks = main._split_text_for_stepfun(long_text)
    check("A1 长文本分段不超限",
          all(len(c) <= main.STEPFUN_TTS_MAX_INPUT for c in chunks),
          f"{len(long_text)}字 -> {len(chunks)}段")
    check("A2 分段不丢字", sum(len(c) for c in chunks) >= len(long_text) - len(chunks))
    check("A3 短文本不分段", main._split_text_for_stepfun("短文本测试。") == ["短文本测试。"])

    # B. 音色映射
    check("B1 晓晓->邻家姐姐",
          main._translate_voice_for_stepfun("zh-CN-XiaoxiaoNeural") == "linjiajiejie")
    check("B2 云健->磁性男声",
          main._translate_voice_for_stepfun("zh-CN-YunjianNeural") == "cixingnansheng")
    check("B3 云扬->播音男声",
          main._translate_voice_for_stepfun("zh-CN-YunyangNeural") == "boyinnansheng")
    check("B4 阶跃音色原样返回",
          main._translate_voice_for_stepfun("cixingnansheng") == "cixingnansheng")
    check("B5 未知女声兜底气质温婉",
          main._translate_voice_for_stepfun("zh-CN-AbcFemaleNeural") == "elegantgentle-female")
    check("B6 真实男声云野兜底温柔男声",
          main._translate_voice_for_stepfun("zh-CN-YunyeNeural") == "wenrounansheng")
    check("B10 无法判别性别时落女声默认",
          main._translate_voice_for_stepfun("zh-CN-UnknownNeural") == "elegantgentle-female")
    check("B7 反向翻译 邻家姐姐->晓晓",
          main._translate_voice_from_stepfun("linjiajiejie") == "zh-CN-XiaoxiaoNeural")
    check("B8 反向翻译 磁性男声->云健",
          main._translate_voice_from_stepfun("cixingnansheng") == "zh-CN-YunjianNeural")
    check("B9 反向翻译 无映射按性别",
          main._translate_voice_from_stepfun("lengyanyujie") == "zh-CN-XiaoxiaoNeural")

    # C. 情绪指令
    ins = main._stepfun_instruction_from_mood("紧张", 9, True)
    check("C1 情绪指令生成", "紧张" in ins and "浓烈" in ins and "旁白" in ins)
    ins2 = main._stepfun_instruction_from_mood("悲伤", 2, False)
    check("C2 低强度克制", "悲伤" in ins2 and "克制" in ins2)
    check("C3 指令≤200字符", len(ins) <= 200)
    check("C4 未知情绪返回空/旁白", "紧张" not in main._stepfun_instruction_from_mood("未知情绪", 5, False))

    # D. 引擎选择
    has_key = bool(main._get_stepfun_api_key())
    print(f"[INFO] STEPFUN_API_KEY {'已配置' if has_key else '未配置'}")
    engine = main._select_tts_engine()
    print(f"[INFO] 引擎自动选择 -> {engine}")
    if has_key:
        check("D1 配Key后引擎=stepfun", engine == "stepfun")
    else:
        check("D2 无Key引擎!=stepfun", engine != "stepfun")

    if dry:
        print("\n(--dry 模式: 跳过真实合成)")
        _summary()
        return

    # E. 真实短文本合成（含情绪指导）
    print("\n[E] 真实合成测试（消耗阶跃星辰额度）...")
    t0 = time.time()
    out1 = str(OUT / "stepfun_mood.mp3")
    try:
        dur = await main.generate_tts(
            "他推开门，屋内的烛火忽然熄灭。黑暗中，有人低声唤他的名字。",
            out1, voice="zh-CN-XiaoxiaoNeural", engine="stepfun",
            mood="紧张", intensity=8, is_narration=False)
        size1 = Path(out1).stat().st_size if Path(out1).exists() else 0
        check("E1 短文本真实合成", size1 > 1000,
              f"{dur:.1f}s {size1}B {time.time()-t0:.1f}s")
    except Exception as e:
        check("E1 短文本真实合成", False, f"{type(e).__name__}: {str(e)[:120]}")

    # F. 长文本分段合成 + 拼接
    t0 = time.time()
    out2 = str(OUT / "stepfun_long.mp3")
    long_script = ("夜色如墨，寒风呼啸，他独自走在青石巷中。远处传来更鼓声，三更天了。"
                   "他裹紧外衣，加快脚步。巷子深处，一盏灯笼忽明忽暗。" * 8)  # ~430字
    try:
        dur2 = await main.generate_tts(long_script, out2, voice="cixingnansheng",
                                        engine="stepfun", mood="神秘", intensity=5,
                                        is_narration=True)
        size2 = Path(out2).stat().st_size if Path(out2).exists() else 0
        check("F1 长文本分段合成", size2 > 10000,
              f"{len(long_script)}字 {dur2:.1f}s {time.time()-t0:.1f}s")
    except Exception as e:
        check("F1 长文本分段合成", False, f"{type(e).__name__}: {str(e)[:120]}")

    # G. 失败回退链（假 Key -> 401 -> 回退 Edge）
    real_key = os.environ.get("STEPFUN_API_KEY", "")
    os.environ["STEPFUN_API_KEY"] = "sk-fake-key-for-fallback-test"
    try:
        t0 = time.time()
        out3 = str(OUT / "fallback_edge.mp3")
        dur3 = await main.generate_tts("回退链测试：假 Key 应触发 401 并回退 Edge。",
                                        out3, voice="linjiajiejie", engine="stepfun")
        size3 = Path(out3).stat().st_size if Path(out3).exists() else 0
        check("G1 假Key回退Edge出声", size3 > 1000, f"{dur3:.1f}s {size3}B")
    except Exception as e:
        check("G1 假Key回退Edge出声", False, f"{type(e).__name__}: {str(e)[:120]}")
    finally:
        if real_key:
            os.environ["STEPFUN_API_KEY"] = real_key
        else:
            os.environ.pop("STEPFUN_API_KEY", None)

    _summary()


def _summary():
    print("\n" + "=" * 62)
    print(f"结果: {PASS} 通过, {FAIL} 失败" + ("  <<< 存在失败项" if FAIL else "  全部通过"))
    print("=" * 62)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run("--dry" in sys.argv))
