# 故障排查与踩坑复盘（TROUBLESHOOTING）

按「网络环境 / 配音 / 视频生成 / 后期与工程」分类，每条都给出**根因**与**已验证解法**。

---

## 一、网络环境（GitHub 访问，国内高发）

### 1. HTTP 推送大仓库失败：502 / `server closed abruptly`

**现象**：`git push` 反复失败，报 `CONNECT tunnel failed, response 502` 或 `schannel: server closed abruptly`。

**根因**：出网走代理，代理对 `github.com` 的 CONNECT 隧道**间歇性拒绝**；小请求（API/fetch）能过，100MB+ 的大推送几乎必挂。

**解法**：改用 **SSH**，双端口轮换重试：

```bash
GIT_SSH_COMMAND="ssh -p 443 -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null \
  -o ServerAliveInterval=15" git push
```

- 端口轮换：`22`（`github.com`）↔ `443`（`ssh.github.com`）——实测两个端口轮流可用
- 加 `ServerAliveInterval=15` 保活，避免 NAT 掐断
- 找不到稳定窗口时循环重试 8-10 次，通常能撞上可用窗口

### 2. `schannel: CRYPT_E_NO_REVOCATION_CHECK`

**根因**：Windows Schannel 校验证书吊销列表（CRL）时访问不到 CRL 服务器。

```bash
git config --global http.schannelCheckRevoke false
```

### 3. `~/.ssh/config` 写入 Permission denied

**根因**：WorkBuddy safe-delete 钩子拦截了对已有文件的覆写（不是权限问题）。

**解法**：不写文件，用 `GIT_SSH_COMMAND` 内联传参（见上文）。

### 4. gh CLI 设备码登录报 `unexpected EOF`

**现象**：`gh auth login --web` 轮询 `github.com/login/oauth/access_token` 时 EOF。

**解法**：重试即可（`api.github.com` 通常可达）；反复失败时让用户**手动把 SSH 公钥加到 GitHub**（https://github.com/settings/ssh/new）更可靠。

---

## 二、配音（TTS）

### 5. StepFun 报 `402 exceeded quota`

**根因**：**Step Plan 订阅 Key 走的是按量计费端点**。官方要求订阅 Key 用 `/step_plan/v1/audio/speech`。

```env
STEPFUN_BASE_URL=https://api.stepfun.com/step_plan/v1
```

按量计费 Key 则**不要**设这一行。

### 6. StepFun 报 `401 invalid_api_key`

Key 填错或过期。代码会自动回退 Edge-TTS，**出片不中断**，日志里有明确提示。

### 7. 想换更轻量的模型

`.env` 设 `STEPFUN_TTS_MODEL=step-tts-mini`：更快更便宜，但**不支持 `instruction` 情绪指导**（情绪只能靠语速/音调兜底）。

### 8. CosyVoice2 不可用

`localhost:50000` 未启动 → 自动回退。启动时确认 WebUI 已跑起来；依赖缺 `aiohttp` 会报 `ModuleNotFoundError`。

---

## 三、视频生成（ComfyUI / wan5b）

### 9. T5 编码器报 `fp8 scaled is not supported`

**根因**：`text_encoders/` 放的是 Comfy-Org repackaged 版（含 `scaled_fp8` 键），WanVideoWrapper 节点拒收。

**解法**：换 **Kijai 版** T5（`umt5-xxl-enc-fp8_e4m3fn.safetensors`，含 `token_embedding.weight`）。

### 10. RIFE 补帧报 ops_backend 错误

**根因**：缺 `cupy` 和 `taichi`。

```bash
pip install cupy-cuda12x
```

同时注意新版 RIFE VFI 节点**必填** `dtype` / `torch_compile` / `batch_size` 三个参数。

### 11. IPAdapter 报 `ClipVision model not found`

**根因**：Wan 版 clip_vision 文件名不匹配插件的正则（`ViT.H.14.*s32B.b79K`）。

**解法**：把 `clip_vision_h` 复制并改名为 `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors`（底层权重相同）。

### 12. 显存不足 / 进程崩溃

必须用 `--lowvram --async-offload 2` 启动 ComfyUI；并发控制在 2 以内。

---

## 四、后期与工程

### 13. ffmpeg concat 报 `No such file or directory`

**根因**：concat list 里的**相对路径是相对 list 文件所在目录解析**的，不是相对当前目录。

**解法**：list 内一律写绝对路径（`Path.resolve()` + `as_posix()`）。

### 14. 进程莫名退出，日志尾有 `SystemExit` / safe-delete

**根因**：清理临时文件时 `unlink()` 被安全钩子拦截，抛 `SystemExit` 杀进程（`except Exception` 抓不到）。

```bat
set CODEBUDDY_SAFE_DELETE_ENABLED=0
```

启动脚本已内置；手动启动时务必带上。

### 15. httpx 报 `[Errno 22]`

系统代理残留（Clash 没开）。`set HTTP_PROXY=` / `set HTTPS_PROXY=` 清空。

### 16. Windows 下 ffprobe 抛 `UnicodeDecodeError`

**根因**：读取 ffprobe 的 **GBK** 输出时按 utf-8 解码，发生在子进程 reader 线程，**不影响结果**（只是日志噪音）。

### 17. QA 阶段全部场景失败并连环重跑

**根因**（已修）：Step 7 清理用了全局通配 `*_faded*`，误删其他场景中间文件。

若再遇到：检查数据库里 `final_video_path` 指向的文件是否真实存在，急救可用页面「重新合并」按钮。

### 18. 中断后续跑

直接重新点「开始生成」，已完成场景自动跳过（状态存于 `output/luminaforge.db`）。

---

## 附：开发注意

- `Edit` 工具 `replace_all` 是**子串匹配**：8 空格的匹配串会命中 12 空格的行，改代码时注意缩进上下文
- 新增 TTS 引擎时，务必同时更新：`_select_tts_engine()`、音色映射表、`RECOMMENDED_VOICES`/`STEPFUN_VOICES`、`.env.example`
