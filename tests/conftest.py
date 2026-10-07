"""pytest 全局夹具：把 ffmpeg 路径带进测试进程。

为什么需要
----------
`pytest` **不会**自动读项目 `.env`，而 `engines/base.py::resolve_ffmpeg()` 靠
`FFMPEG_PATH` 环境变量定位 ffmpeg。于是同一个仓库：

    python -m pytest tests            # → 4 failed（找不到 ffmpeg）
    shoot.bat / start_local.bat       # → 正常（.bat 里把 .env 导出了）

这里在**测试收集前**做一次极简 `.env` 解析（不引 python-dotenv 依赖），
并在 `.env` 缺失时自动探测工作区布局，让测试不依赖调用者的环境。

CI 上既没有 `.env` 也没有 ffmpeg 时，涉及真实 ffmpeg 的用例会在
`is_available()` 断言处失败——这是**有意的**：宁可显式失败，也不要静默跳过
让人以为「出片链路验过了」。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(env_file: Path) -> None:
    """把 .env 里的键值塞进 os.environ（已存在的优先，不覆盖调用者环境）。"""
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


def _autodetect_ffmpeg_dir() -> None:
    """没配 FFMPEG_PATH 时，按本工作区布局探测（项目内 → workspace 根）。"""
    if os.environ.get("FFMPEG_PATH"):
        return
    for cand in (ROOT / "tools" / "ffmpeg" / "bin", ROOT.parent / "tools" / "ffmpeg" / "bin"):
        if (cand / "ffmpeg.exe").exists() or (cand / "ffmpeg").exists():
            os.environ["FFMPEG_PATH"] = str(cand)
            return


_load_dotenv(ROOT / ".env")
_autodetect_ffmpeg_dir()
