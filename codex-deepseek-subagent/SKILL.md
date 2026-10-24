---
name: codex-deepseek-subagent
description: 仅在用户要求配置、检查、测试、修复、停用或卸载 Codex 的 DeepSeek 原生子 Agent 时使用；普通 DeepSeek API 问题和已配置后的日常编码任务不要触发。
---

# Codex DeepSeek 子 Agent

本 Skill 只维护配置，不承接日常用户任务。确定性的文件、模型目录和凭据操作交给 `scripts/codex_deepseek.py`；不要手动改 TOML、JSON、Agent 文件或系统凭据库。

## 关键契约

- 只使用桌面应用内置的 Codex 运行时；版本仅用于诊断，兼容性以真实派发验收为准。
- 固定使用 `deepseek-flash` 和 `max` 思考程度；`status`、`setup`、`repair` 与实时验收必须按同一目标判断。
- Provider 的 Base URL 默认 `https://api.deepseek.com`；需要 OpenAI 兼容网关时用 `setup`/`repair --base-url <url>` 指定。只接受 http/https，禁止在 URL 中携带用户名、密码、查询串或片段，且只有本机回环地址允许 http。省略 `--base-url` 时保留已配置的合法地址；显式地址只改本 Skill 托管的 Provider 块，用户自建且不兼容的配置仍返回 `conflict`，不强行覆盖。
- Windows Store 安装版需要完整的 `codex.exe` 与同目录 `codex-code-mode-host.exe`、`codex-windows-sandbox-setup.exe`、`codex-command-runner.exe`。由管理程序在 `setup`、`repair` 或 `test` 时缓存完整验收运行时，不要只复制单个 `codex.exe`。
- 从桌面配置读取父模型，并由管理程序应用 v1 明文派发设置；不要硬编码父模型或手改配置。技术原因见 [references/compatibility.md](references/compatibility.md)。
- 父模型变化后必须运行 `repair`，再重新验收。
- DeepSeek-V4.1-Flash 原生支持图片和截图；视频仍由父 Agent 先识别再传入文字事实。
- 日常 DeepSeek 执行段统一走受管执行面：由 `codex-deepseek-subagent` 管理的 `codex exec` 子进程（`model=deepseek-flash`、`model_provider="deepseek"`、`model_reasoning_effort="max"`），具体派发约定见 prewalk skill 的执行器契约。不要为日常任务运行本 Skill 的管理命令。
- 桌面 CLI ≥0.146 把 collab/spawn_agent 限定在 ChatGPT 后端，原生 `spawn_agent(agent_type="DeepSeek")` 无法把子线程路由到 DeepSeek provider（会得到 model-unsupported 400）。原生路由不可用时不再提示重启或开新任务，而是直接使用上述受管执行面；若执行面验收失败，运行 `repair`。

## 触发后的流程

1. 运行 `status --json`，根据结构化状态继续，不靠文件名猜测。
2. 配置请求运行 `setup --json`；父模型已变化或配置损坏时运行 `repair --json`。默认使用 DeepSeek 官方地址；只有用户要求自建/代理网关时才追加 `--base-url <url>`。
3. 缺少凭据时简洁索要 API Key。收到后不要复述、回显或写入临时文件，只通过 `--api-key-stdin` 的标准输入传递。密钥只进入操作系统凭据库。
4. `setup` 或 `test` 使用桌面内置运行时创建隔离验收会话。若返回 `new_task_required` 或 `restart_required`，提示用户重启桌面应用并打开新任务。
5. 验收执行面：受管 `codex exec` 进程必须按固定口令 `NATIVE_DEEPSEEK_OK` 返回（只允许忽略口令末尾的中英文句号、问号或感叹号），且 Provider 必须为 `deepseek`、模型必须为 `deepseek-flash`、思考程度必须为 `max`。不得放宽任何一项，也不能以进程自述代替口令。
6. 最终只汇报状态、实际 Provider、模型、思考程度、角色和备份位置；不要输出密钥或原始事件日志。

## 管理命令

入口。需要 Python 3.11+。macOS 使用 `python3`；Windows 优先使用 Codex 桌面依赖运行时中的 Python 3.12，若不存在再使用可确认版本为 3.11+ 的 `py` 或 `python`：

```text
python3 <skill-dir>/scripts/codex_deepseek.py <command> --json
```

Windows 桌面依赖运行时的常见入口：

```text
%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe <skill-dir>\scripts\codex_deepseek.py <command> --json
```

- `status`：只读检查桌面内置运行时、配置、模型目录、凭据和客户端能力；`provider_base_url_valid` 按结构校验已配置地址（不要求必须是官方域名）。
- `setup`：写入配置并验收；缺少密钥时返回 `credential_missing`。可选 `--base-url`。
- `test`：通过桌面内置运行时执行一次直连测试和一次执行面验收（受管 `codex exec` 按 `NATIVE_DEEPSEEK_OK` 口令返回）。
- `repair`：按当前父模型重新应用配置并验收，保留既有合法 Base URL，除非显式传 `--base-url`。
- `disable`：停用本 Skill 创建的角色，保留 Provider、模型目录和凭据。
- `uninstall`：移除本 Skill 管理的配置；只有用户明确要求删除凭据时才传 `--remove-credential`。

默认使用当前 `CODEX_HOME`；仅在用户明确指定其他 Codex Home 时传 `--codex-home`。

## 状态处理

- `ready`：直连测试与执行面验收均通过（`codex exec` 以 `deepseek` provider、`deepseek-flash` 模型、`max` 思考程度按口令返回）。
- `configured`：静态配置完整，但尚未完成实时验收。
- `credential_missing`：索要 API Key 后继续原流程。
- `invalid_base_url`：地址不是合法 http/https、携带凭据/查询串/片段，或对非回环地址使用了 http；请用户改地址后重试，不要手改 config.toml。
- `operation_in_progress`：已有配置操作正在运行，稍后重试，不并发修改。
- `conflict`：报告冲突文件和字段，等待用户决定是否替换。
- `unsupported`：报告缺少的系统能力，不按固定版本号猜测兼容性，也不手工绕过。
- `failed`：读取结构化 `errors`；若程序已回滚，明确说明，不再手改配置。

更详细的路径、版本和安全边界见 [references/compatibility.md](references/compatibility.md)。
