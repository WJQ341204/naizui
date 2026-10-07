"""生成成片的可视化预览：contact sheet（静态图）+ GIF（动图）。

用途：视频文件在部分预览器里打不开 / 不显示缩略图时，
用这两张图快速确认「画面有内容 + 字幕烧录正常 + 转场生效」。

用法：
    python tools/make_preview.py output/film_demo.mp4
    python tools/make_preview.py output/film_demo.mp4 --cols 3 --cell-w 360

产物（与输入同目录）：
    <stem>_sheet.jpg   网格拼图，每格标 t=mm:ss
    <stem>.gif         低分辨率动图预览
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def grab(video: Path, t: float, dst: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
         "-frames:v", "1", "-y", str(dst)],
        check=True,
    )
    return dst


def mmss(sec: float) -> str:
    return f"t={int(sec) // 60:02d}:{int(sec) % 60:02d}"


def build_sheet(video: Path, out: Path, cols: int, cell_w: int, n: int) -> Path:
    dur = probe_duration(video)
    # 均匀取 n 个时间点，首尾各留 5% 余量避免取到纯黑首帧 / 尾帧
    stamps = [dur * (0.05 + 0.90 * i / max(1, n - 1)) for i in range(n)]

    tmp = Path(tempfile.mkdtemp(prefix="preview_"))
    cells: list[tuple[Image.Image, str]] = []
    for i, t in enumerate(stamps, 1):
        grab(video, t, tmp / f"f{i:02d}.png")
        im = Image.open(tmp / f"f{i:02d}.png").convert("RGB")
        w, h = im.size
        cell_h = int(cell_w * h / w)
        cells.append((im.resize((cell_w, cell_h), Image.LANCZOS), mmss(t)))

    rows = (len(cells) + cols - 1) // cols
    cw, ch = cells[0][0].size
    pad, bar = 8, 26
    sheet = Image.new(
        "RGB",
        (cols * cw + (cols + 1) * pad, rows * (ch + bar) + (rows + 1) * pad),
        (24, 24, 28),
    )
    draw = ImageDraw.Draw(sheet)
    for idx, (im, label) in enumerate(cells):
        r, c = divmod(idx, cols)
        x = pad + c * (cw + pad)
        y = pad + r * (ch + bar + pad)
        sheet.paste(im, (x, y))
        draw.rectangle([x, y + ch, x + cw, y + ch + bar], fill=(38, 38, 44))
        draw.text((x + 6, y + ch + 6), f"#{idx + 1}  {label}", fill=(235, 235, 240))

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return out


def build_gif(video: Path, out: Path, width: int, fps: int, max_sec: float) -> Path:
    """低分辨率动图预览。用 palettegen/paletteuse 两遍法保证颜色不糊。"""
    dur = min(probe_duration(video), max_sec)
    pal = out.with_suffix(".palette.png")
    vf = f"fps={fps},scale={width}:-2:flags=lanczos"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-t", f"{dur:.2f}", "-i", str(video),
         "-vf", f"{vf},palettegen=max_colors=128:stats_mode=diff", "-y", str(pal)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-t", f"{dur:.2f}", "-i", str(video), "-i", str(pal),
         "-lavfi", f"{vf} [x]; [x][1:v] paletteuse=dither=bayer",
         "-loop", "0", "-y", str(out)],
        check=True,
    )
    pal.unlink(missing_ok=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="成片 → contact sheet + GIF 预览")
    ap.add_argument("video", help="输入视频路径")
    ap.add_argument("--cols", type=int, default=3, help="contact sheet 列数")
    ap.add_argument("--cell-w", type=int, default=340, help="单格宽度（像素）")
    ap.add_argument("--frames", type=int, default=6, help="contact sheet 取几帧")
    ap.add_argument("--gif-w", type=int, default=360, help="GIF 宽度")
    ap.add_argument("--gif-fps", type=int, default=8, help="GIF 帧率")
    ap.add_argument("--gif-sec", type=float, default=20.0, help="GIF 最长覆盖秒数")
    ap.add_argument("--no-gif", action="store_true", help="只出 contact sheet")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"找不到视频：{video}", file=sys.stderr)
        return 2

    stem = video.stem
    sheet = build_sheet(video, video.parent / f"{stem}_sheet.jpg",
                        args.cols, args.cell_w, args.frames)
    print(f"contact sheet: {sheet}  ({sheet.stat().st_size / 1024:.0f} KB)")

    if not args.no_gif:
        gif = build_gif(video, video.parent / f"{stem}.gif",
                        args.gif_w, args.gif_fps, args.gif_sec)
        print(f"gif preview : {gif}  ({gif.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
