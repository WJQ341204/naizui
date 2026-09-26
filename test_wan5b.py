"""wan5b 引擎单链路验证脚本：PIL 生成测试首帧 → Wan 2.2 TI2V-5B I2V → RIFE+超分。

用法: D:/ComfyUI/venv/Scripts/python.exe test_wan5b.py
不依赖 SDXL，直接验证 ComfyUI WanVideoWrapper 工作流。
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw

# ── 1. 生成 704x1280 竖版测试首帧（模拟场景1：雨夜江南石板路 + 人物剪影）──
img = Image.new("RGB", (704, 1280))
d = ImageDraw.Draw(img)
# 冷蓝夜空渐变
for y in range(1280):
    t = y / 1280
    d.line([(0, y), (704, y)], fill=(int(18+30*t), int(24+36*t), int(48+52*t)))
# 远处暖黄灯笼光斑
for cx, cy, r in [(120, 420, 26), (300, 380, 18), (560, 450, 30)]:
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(212, 160, 64))
# 湿润石板路（下半部）
d.rectangle([0, 820, 704, 1280], fill=(42, 50, 66))
for i in range(14):
    y0 = 830 + i * 32
    for j in range(5):
        x0 = 10 + j * 140 + (i % 2) * 60
        d.rectangle([x0, y0, x0 + 120, y0 + 26], fill=(52, 62, 80))
# 人物剪影（中景背影）
d.ellipse([320, 560, 384, 624], fill=(12, 14, 20))          # 头
d.polygon([(300, 640), (404, 640), (392, 900), (312, 900)], fill=(14, 16, 24))  # 身体
d.line([(322, 660), (310, 800)], fill=(14, 16, 24), width=14)   # 左臂
d.line([(382, 660), (394, 800)], fill=(14, 16, 24), width=14)   # 右臂
# 雨丝
import random
random.seed(7)
for _ in range(220):
    x, y = random.randint(0, 700), random.randint(0, 1270)
    d.line([(x, y), (x - 4, y + 22)], fill=(170, 190, 215), width=1)

first_frame = Path(__file__).parent / "test_first_frame.png"
img.save(first_frame)
print(f"[测试] 首帧已生成: {first_frame}")

# ── 2. 调用 wan5b 引擎 ──
from engines.wan5b import Wan5BEngine
from engines.base import GenerateRequest

out_dir = Path(__file__).parent / "output" / "wan5b_test"
out_dir.mkdir(parents=True, exist_ok=True)

req = GenerateRequest(
    prompt="A lone traveler in ancient Chinese robes walks slowly through a rainy Jiangnan water town at night, warm lanterns glowing in the distance, rain streaks falling diagonally, puddles reflecting lantern light, cinematic, subtle motion",
    negative_prompt="",
    duration_seconds=2.0,       # 48 帧，先小规模验证
    width=704, height=1280,
    first_frame=first_frame,
    output_dir=out_dir,
    output_name="wan5b_chain_test",
    timeout_seconds=2700,
)

engine = Wan5BEngine(base_url="http://127.0.0.1:8188")

async def main():
    print("[测试] 提交 wan5b 两段式工作流…")
    result = await engine.generate(req)
    print(f"[测试] 完成! 引擎={result.engine} 时长={result.duration_seconds}s seed={result.seed}")
    print(f"[测试] 视频路径: {result.video_path}")

if __name__ == "__main__":
    asyncio.run(main())
