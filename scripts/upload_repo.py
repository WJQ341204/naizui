#!/usr/bin/env python3
"""把本项目推送到 GitHub 上一个【新建的空仓库】。

三步走：建仓（GitHub API）→ 提交本地改动 → 推 main。

凭据来源（优先级从高到低）：
  1. 环境变量  GITHUB_TOKEN
  2. 项目根目录 ./.github_token          （已被 .gitignore 忽略，可放心写）
  3. 命令行交互输入（不回显）

Token 需要 repo 权限，例如 GitHub 网页 → Settings → Developer settings →
Personal access tokens → Tokens (classic) → Generate new token (classic) →
勾选 repo / workflow。

用法：
    set GITHUB_TOKEN=ghp_xxxxxxxxxxxx      # PowerShell
    python scripts/upload_repo.py --repo novel-to-video-codex --private

    # 或者不建新仓，直接推到已有的空仓库
    python scripts/upload_repo.py --remote https://github.com/<你的账号>/<仓库>.git

    # 或者推到现在的 naizui 仓库（会往 main 上叠提交）
    python scripts/upload_repo.py --remote origin --no-create
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
GITHUB_API = "https://api.github.com"


def log(msg: str) -> None:
    print(f"[upload] {msg}", flush=True)


def git(*args: str, capture: bool = True) -> str:
    """跑一条 git 命令，返回 stdout。"""
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if capture:
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败：\n{proc.stderr.strip()}")
        return proc.stdout.strip()
    return ""


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True).returncode == 0


def read_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env
    token_file = ROOT / ".github_token"
    if token_file.exists():
        val = token_file.read_text(encoding="utf-8").strip()
        if val:
            return val
    if not sys.stdin.isatty():
        return ""
    return getpass.getpass("GitHub Personal Access Token（不回显）: ").strip()


def github_get(path: str, token: str) -> dict:
    req = request.Request(
        GITHUB_API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "novel-to-video-codex-upload",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:  # 401/403/404 都要给出可读原因
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {path} 返回 {e.code}：{body[:400]}") from e


def github_post(path: str, payload: dict, token: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        GITHUB_API + path,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "novel-to-video-codex-upload",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API POST {path} 返回 {e.code}：{body[:400]}") from e


def push_with_token(remote_url: str, branch: str) -> None:
    """用 token 当密码走 HTTPS，避免 SSH key / 弹窗凭据的问题。"""
    git("remote", "remove", "origin")
    git("remote", "add", "origin", remote_url)
    if not git_ok("rev-parse", "--verify", branch):
        git("checkout", "-b", branch)
    git("push", "-u", "origin", branch)


def main() -> int:
    ap = argparse.ArgumentParser(description="新建 GitHub 仓库并推送本项目")
    ap.add_argument("--repo", default=None, help="要新建的仓库名（不填则复用当前 remote 的仓库名）")
    ap.add_argument("--private", action="store_true", default=True, help="新建为私有仓库（默认）")
    ap.add_argument("--public", dest="private", action="store_false", help="新建为公开仓库")
    ap.add_argument("--remote", default=None, help="已有仓库地址或现有 remote 名（如 origin）")
    ap.add_argument("--no-create", action="store_true", help="跳过建仓，直接推送")
    ap.add_argument("--token", default=None, help="不指定则读环境变量 / .github_token / 交互输入")
    ap.add_argument("--message", default="chore: 提交小说转视频管线（含 MiniMax-H3 引擎）", help="提交信息")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    token = read_token(args)
    if not token:
        log("没拿到 token。请 set GITHUB_TOKEN=ghp_xxx 或把 token 写进 ./.github_token")
        return 2

    try:
        me = github_get("/user", token)
    except RuntimeError as e:
        log(f"鉴权失败，token 可能无效或过期：{e}")
        return 2
    owner = me["login"]
    log(f"已识别 GitHub 账号：{owner}")

    if args.no_create:
        remote = args.remote or "origin"
        url = git("remote", "get-url", remote)
        log(f"跳过建仓，使用已有 remote {remote}：{url}")
    else:
        name = args.repo or git("rev-parse", "--abbrev-ref", "HEAD").split("/")[-1] or "novel-to-video-codex"
        try:
            repo = github_post(
                "/user/repos",
                {
                    "name": name,
                    "private": args.private,
                    "description": "小说转视频（漫剧成片）管线：分镜 → 文本/图生视频 → 配音字幕 → 合成",
                    "auto_init": False,
                },
                token,
            )
            log(f"已创建仓库：{repo['html_url']}")
        except RuntimeError as e:
            if "422" in str(e) or "409" in str(e) or "already exists" in str(e):
                log(f"仓库 {name} 已存在，直接复用：https://github.com/{owner}/{name}")
                repo_url = f"https://github.com/{owner}/{name}.git"
            else:
                log(f"建仓失败：{e}")
                return 2
        else:
            repo_url = repo["clone_url"]
        # 把 ssh/老 remote 换成带 token 的 https 地址
        remote = args.remote or "origin"
        url = repo_url.replace("https://", f"https://x-access-token:{token}@", 1)
        push_with_token(url, args.branch)
        log(f"推送完成 → {repo_url}")
        return 0

    # 走已有 remote 的分支
    if args.remote in (None, "origin"):
        cur = git("remote", "get-url", "origin")
        if cur.startswith("git@") or cur.startswith("ssh://"):
            log("当前 remote 是 SSH 且本机没有 key，临时切到 token+HTTPS 推送")
            target = args.repo or git("remote", "get-url", "origin")
            push_with_token(
                f"https://x-access-token:{token}@{target.removeprefix('git@').replace(':', '/').removesuffix('.git')}.git",
                args.branch,
            )
            git("remote", "set-url", "origin", cur)
            log(f"恢复 remote 为 {cur}")
            return 0
    push_with_token(git("remote", "get-url", args.remote or "origin"), args.branch)
    log("推送完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
