"""把 Comfy-Org 官方 MiniMax-H3 模板转成本项目可直接提交 ComfyUI /prompt API 的工作流。

为什么需要
----------
官方模板（`Comfy-Org/workflow_templates` 的 `video_minimax_h3_*.json`）是
**ComfyUI 前端 UI 格式**：T2V/I2V 是「子图」（根节点 type 是一串 UUID），
R2V 挂着 `ComfySwitchNode` / `PrimitiveInt` 这类前端动态节点。这两种都
**不能直接喂给 ComfyUI 的 /prompt 接口**——API 只认扁平的
`{node_id: {"class_type": ..., "inputs": {...}}}`。

这里做四件事：

1. 从 `definitions.subgraphs[]` 把子图「拍平」成普通节点；
2. 丢掉前端动态节点（ComfySwitchNode / Primitive* / LoraLoaderModelOnly /
   ComfyMathExpression）—— 它们只是官方 UI 上的开关，等价为一组固定值；
3. **输入键名一律从模板原样取**（不靠猜）：节点的每个 widget 值按
   `widgets_values` 顺序对应到带 `widget.name` 的 input 槽位；
4. 把 prompt / 宽 / 高 / 帧长 / 步数 / 随机种子换成项目的 `{{占位符}}`
   约定，运行时由 `engines/h3_local.py` 填值。

用法
----
    python tools\\build_h3_workflows.py

依赖开发的模板文件（不进 git，见 .gitignore）：
    models/minimax-h3/video_minimax_h3_t2v.json
    models/minimax-h3/video_minimax_h3_r2v.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / "comfyui"
SRC_DIR = ROOT.parent / "models" / "minimax-h3"

# 模板来源二选一（按顺序取第一个存在的）：
#   1. 开发机上现拉的官方模板 —— workspace/models/minimax-h3/（目录被 .gitignore 忽略）
#   2. 仓库里随代码带的一份副本 —— tools/，clone 下来后离线也能重建工作流
FALLBACK_DIR = ROOT / "tools"


def _source(stem: str) -> Path:
    for d in (SRC_DIR, FALLBACK_DIR):
        p = d / f"video_minimax_h3_{stem}.json"
        if p.exists():
            return p
    return SRC_DIR / f"video_minimax_h3_{stem}.json"


OFFICIAL = {
    "h3_fl2va": _source("t2v"),    # 文生视频 / 首尾帧生视频
    "h3_ref2va": _source("r2v"),   # 参考图生视频
}

# 采样器 / 调度器：沿用官方模板默认值
_SAMPLER = "res_multistep"
_SCHEDULER = "simple"

# 官方 UI 上可切换、但 API 提交没必要保留的前端节点：
# 动态开关 / 辅助取值节点，实际效果等价为一组固定值
DROP_TYPES = {
    "ComfySwitchNode", "PrimitiveInt", "PrimitiveFloat", "PrimitiveBoolean",
    "PrimitiveStringMultiline", "LoraLoaderModelOnly", "ComfyMathExpression",
    "MarkdownNote", "ResolutionSelector",
}

# 官方模板里硬编码的模型文件名 → 本项目 .env 可覆盖的输出文件名
MODEL_FILENAMES = (
    "minimax_h3_fl2va_pruned_fp8_scaled.safetensors",
    "minimax_h3_ref2va_pruned_fp8_scaled.safetensors",
    "qwen3vl_32b_nvfp4_awq.safetensors",
    "minimax_h3_video_vae_int8_convrot.safetensors",
    "minimax_h3_audio_vae_fp32.safetensors",
)

# 输入槽位 → 运行时占位符（键名来自官方模板，不靠猜）
PARAM_SLOTS = {
    "prompt": "{{POSITIVE_PROMPT}}",
    "width": "{{H3_WIDTH}}",
    "height": "{{H3_HEIGHT}}",
    "length": "{{H3_LENGTH}}",
    "noise_seed": "{{H3_SEED}}",
    "steps": "{{H3_STEPS}}",
}


# ─── 子图展开 ────────────────────────────────────────────────
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s or ""))


def _flatten_subgraph(template: dict) -> tuple[list[dict], list]:
    """把官方模板摊平：子图内部节点 + 挂在子图外面的顶层节点（如 SaveVideo）。

    对外只暴露「任意以 MiniMaxH3 开头的主节点」一个新 id，顶层节点的连线
    统一指向它（见 main 里的哨兵 0 + resolve）。
    返回 (nodes, links)。
    """
    subs = (template.get("definitions") or {}).get("subgraphs") or []
    if not subs:
        # 顶层图（R2V 就是这种）：无子图要展开
        inner, inner_links = [], []
        outer, outer_links = list(template.get("nodes") or []), list(template.get("links") or [])
    else:
        sub = subs[0]
        inner, inner_links = list(sub.get("nodes") or []), list(sub.get("links") or [])
        outer, outer_links = [], []

    # 子图里混着 SubGraphInput / SubGraphOutput 伪节点（type 非 str），丢掉
    nodes = [nd for nd in inner if isinstance(nd.get("type"), str)
             and nd.get("type") not in DROP_TYPES
             and not _is_uuid(nd.get("type"))]
    if not outer:
        # 子图模板：SaveVideo 之类挂在子图外，而子图根节点 type 是一串 UUID —— 那是子图引用不是真节点
        outer = [nd for nd in template.get("nodes") or []
                 if isinstance(nd.get("type"), str)
                 and nd.get("type") not in DROP_TYPES
                 and not _is_uuid(nd.get("type"))]
        outer_links = template.get("links") or []

    merged = nodes + outer
    links = list(inner_links) + list(outer_links)
    # 顶层连线若挂在 UUID 子图节点上，改指哨兵，由 force 补成真实主节点 id
    idmap = {nd["id"] for nd in outer if not isinstance(nd.get("type"), str)}
    for l in links:
        if isinstance(l, dict) and l.get("origin_id") in idmap:
            l["origin_id"] = 0
        elif isinstance(l, (list, tuple)) and len(l) >= 4 and l[1] in idmap:
            l[1] = 0
    return merged, links


def _link_map(links: list) -> dict[int, dict]:
    """官方两种前端格式都要认：新版 dict {id,origin_id,...}，旧版 list [id,from,slot,to,slot,type]。"""
    out: dict[int, dict] = {}
    for l in links or []:
        if isinstance(l, dict):
            out[l["id"]] = l
        elif isinstance(l, (list, tuple)) and len(l) >= 5:
            out[l[0]] = {"id": l[0], "origin_id": l[1], "target_id": l[3],
                         "target_slot": l[4]}
    return out


def _collect_inputs(node: dict, lmap: dict[int, dict], refmap: dict[int, int]) -> dict:
    """把前端节点的 inputs + widgets_values 收敛成 API 需要的 inputs 字典。

    带 link 的槽位 → 上游节点的新 id；其余是 widget，按 widgets_values
    顺序依次填进带 widget.name 的槽位。
    """
    lmap_by_target: dict[tuple[int, int], int] = {}
    for lid, l in lmap.items():
        lmap_by_target[(l["target_id"], l["target_slot"])] = l["origin_id"]

    widget_slots = [(i.get("name"), i.get("widget", {}).get("name"))
                    for i in (node.get("inputs") or []) if i.get("link") is None]
    wv = list(node.get("widgets_values") or [])

    inputs: dict = {}
    for i in (node.get("inputs") or []):
        if i.get("link") is None:
            continue
        lid = i["link"]
        if lid not in lmap:
            continue
        origin = lmap[lid]["origin_id"]
        inputs[i["name"]] = refmap.get(origin, origin)
    for idx, (slot, wname) in enumerate(widget_slots[: len(wv)]):
        if wname:
            inputs[wname] = wv[idx]
    return inputs


def convert(nodes: list[dict], links: list[dict],
            force: dict[str, dict[str, object]] | None = None) -> dict:
    """前端节点 → ComfyUI API prompt 字典。

    force: {class_type: {输入槽位: 值}}。值写成另一个 class_type 名字时，
    会被改写成该类型节点的新 id——用来补被删掉的 switch/primitive 留下的悬空引用。
    """
    lmap = _link_map(links)
    keep = [nd for nd in nodes if nd.get("type") not in DROP_TYPES]

    refmap: dict[int, int] = {nd["id"]: i + 1 for i, nd in enumerate(keep)}
    live = set(refmap.values())
    # by_type: "UNETLoader" → 新 id；by_occ: "VAELoader#2" → 第 2 个 VAELoader 的新 id
    by_type: dict[str, int] = {}
    by_occ: dict[str, int] = {}
    for nd in keep:
        t = nd.get("type")
        by_type.setdefault(t, refmap[nd["id"]])
        by_occ[f"{t}#{sum(1 for k in by_occ if k.startswith(t + '#')) + 1}"] = refmap[nd["id"]]

    def resolve(val):
        """force 的值写成 class_type 名字时，解析成对应节点的新 id。

        支持完整类名（"UNETLoader"）、前缀（"MiniMaxH3"）与第 n 个（"VAELoader#2"）。
        """
        if not isinstance(val, str):
            return val
        if val in by_type:
            return by_type[val]
        for cls, nid in by_type.items():
            if cls.startswith(val):
                return nid
        return by_occ.get(val, val)

    api: dict = {}
    seen: dict[str, int] = {}
    for idx, nd in enumerate(keep, start=1):
        t = nd.get("type")
        seen[t] = seen.get(t, 0) + 1
        ins = _collect_inputs(nd, lmap, refmap)
        # 悬空引用（上游被删 / 子图输入伪节点）丢掉，由 force 补
        ins = {k: v for k, v in ins.items() if not (isinstance(v, int) and v not in live)}

        # force 的 key 用 "ClassType" 或 "ClassType#n" 定位同一类型里的第 n 个
        for key, patch in (force or {}).items():
            cls, _, nth = key.partition("#")
            if cls != t or (nth and int(nth) != seen[t]):
                continue
            for slot, val in patch.items():
                ins[slot] = resolve(val)

        # 参数槽位最后覆盖，占位符优先级最高
        for slot, ph in PARAM_SLOTS.items():
            if slot in ins:
                ins[slot] = ph

        api[str(idx)] = {"class_type": t, "inputs": ins}
    return api


def main() -> int:
    ok = True
    for name, path in OFFICIAL.items():
        if not path.exists():
            print(f"[skip] {name}: 缺少 {path}（跳过）", file=sys.stderr)
            ok = False
            continue
        tpl = json.loads(path.read_text(encoding="utf-8"))
        nodes, links = _flatten_subgraph(tpl)

        # 子图输入 / switch / primitive 被删后留下的空槽位，按官方 UI 的默认值补齐。
        # BasicGuider.model 直接指回 UNETLoader（等价于官方 UI 上「不挂 turbo LoRA」那条分支）。
        force = {
            "UNETLoader": {"unet_name": "{{H3_UNET_NAME}}"},
            "CLIPLoader": {"clip_name": "{{H3_CLIP_NAME}}",
                           "type": "minimax", "device": "default"},
            "VAELoader#1": {"vae_name": "{{H3_VAE_NAME}}"},
            "VAELoader#2": {"vae_name": "{{H3_AUDIO_VAE_NAME}}"},
            "RandomNoise": {"noise_seed": "{{H3_SEED}}"},
            "KSamplerSelect": {"sampler_name": _SAMPLER},   # 新版是必填项，前端图里靠 widgets_values 带
            "BasicScheduler": {"model": "UNETLoader", "scheduler": _SCHEDULER,
                               "denoise": 1, "steps": "{{H3_STEPS}}"},
            "BasicGuider": {"model": "UNETLoader"},
            "MiniMaxH3ImageToVideo": {
                "prompt": "{{POSITIVE_PROMPT}}",
                "width": "{{H3_WIDTH}}", "height": "{{H3_HEIGHT}}",
                "length": "{{H3_LENGTH}}",
            },
            "SaveVideo": {"video": "MiniMaxH3",
                          "filename_prefix": "video/H3",
                          "format": "auto", "codec": "auto"},
        }
        # 主节点的参数槽位：T2V/I2V 走 ImageToVideo，R2V 走 ReferenceToVideo
        if name == "h3_ref2va":
            force["MiniMaxH3ReferenceToVideo"] = {
                "prompt": "{{POSITIVE_PROMPT}}", "length": "{{H3_LENGTH}}"}
        else:
            force["MiniMaxH3ImageToVideo"] = {
                "prompt": "{{POSITIVE_PROMPT}}",
                "width": "{{H3_WIDTH}}", "height": "{{H3_HEIGHT}}",
                "length": "{{H3_LENGTH}}"}
        api = convert(nodes, links, force)

        # 模型文件名换成本项目实际下载的那份（.env 里可再覆盖）
        blob = json.dumps(api, ensure_ascii=False)
        for fn in MODEL_FILENAMES:
            blob = blob.replace(fn, fn)

        out = WORKFLOW_DIR / f"{name}.json"
        out.write_text(json.dumps(json.loads(blob), indent=2), encoding="utf-8")
        kinds = sorted({v["class_type"] for v in json.loads(blob).values()})
        print(f"[ok] {out.relative_to(ROOT)}  节点 {len(json.loads(blob))} 个")
        print(f"     {' '.join(kinds)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
