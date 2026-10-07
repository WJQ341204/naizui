"""wan5b 第二段验证：复用第一段基础视频，只跑 RIFE 补帧 + 2x 超分。"""
import asyncio, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engines.wan5b import Wan5BEngine, COMFY_INPUT
from engines.base import GenerateRequest

BASE_SRC = Path("D:/ComfyUI/output/engines/wan5b_chain_test_base_00001.mp4")
OUT_DIR = Path(__file__).parent / "output" / "wan5b_test"

async def main():
    # 1. 复制基础视频到 ComfyUI input（模拟引擎 generate 的第二段前置步骤）
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    base_input = COMFY_INPUT / "wan5b_chain_test_base.mp4"
    shutil.copyfile(BASE_SRC, base_input)
    print(f"[测试2] 基础视频已复制: {base_input}")

    # 2. 提交第二段工作流
    engine = Wan5BEngine(base_url="http://127.0.0.1:8188")
    req = GenerateRequest(prompt="", output_name="wan5b_chain_test",
                          output_dir=OUT_DIR, timeout_seconds=900)
    pwf = engine._build_post_workflow(req, str(base_input))
    pid = await engine._submit(pwf)
    print(f"[测试2] 第二段已提交 {pid[:12]}…")
    entry = await engine._wait_result(pid, timeout=900)
    found = engine.find_output(entry.get("outputs", {}))
    if not found:
        raise RuntimeError("第二段无输出")
    dest = OUT_DIR / "wan5b_chain_test.mp4"
    await engine._download(*found, dest)
    size = dest.stat().st_size if dest.exists() else 0
    print(f"[测试2] 完成! 最终视频: {dest} ({size/1e6:.2f}MB)")

asyncio.run(main())
