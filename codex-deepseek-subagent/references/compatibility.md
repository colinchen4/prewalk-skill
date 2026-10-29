# 兼容性与安全边界

## 支持范围

- macOS
- Windows
- Python 3.11+
- ChatGPT/Codex 桌面应用至少启动过一次
- DeepSeek 官方 Responses API
- `deepseek-flash`
- 思考程度默认 `medium`（可用 `--effort` / `CODEX_DEEPSEEK_EFFORT` 配置；Prewalk 不推荐 Builder `max`）

## 配置位置

默认 `CODEX_HOME` 为 `~/.codex`：

- Codex 配置：`$CODEX_HOME/config.toml`
- 合并模型目录：`$CODEX_HOME/models-with-deepseek.json`
- 自定义角色：`$CODEX_HOME/agents/DeepSeek.toml`（默认即 `~/.codex/agents/DeepSeek.toml`）
- 管理状态与备份：`$CODEX_HOME/codex-deepseek-subagent/`
- 系统凭据目标：`codex-deepseek-api-key`
  - macOS：Keychain
  - Windows：Credential Manager

程序不修改顶层 `model` 或顶层 `model_provider`，主任务仍使用用户原来的模型和登录方式。

## 执行面兼容性

检查和验收只使用桌面应用内置运行时，不回退到 PATH 中可能版本不同的独立 CLI。Windows Store 安装版位于受保护的 `WindowsApps` 目录；`setup`、`repair` 或 `test` 会把 `codex.exe`、`codex-code-mode-host.exe`、`codex-windows-sandbox-setup.exe` 与 `codex-command-runner.exe` 一起缓存到 `$CODEX_HOME/codex-deepseek-subagent/desktop-runtime/`。通过 `CODEX_DESKTOP_BIN` 指定自定义路径时，同目录也必须包含这四个文件。版本号仅作诊断；兼容性由模型目录解析、DeepSeek 直连、执行面口令和工作链验收共同决定。

父模型从桌面当前配置动态读取。管理程序会把 `features.multi_agent_v2` 设为 `false`，并把该父模型的 `multi_agent_version` 固定为 `v1`；同时把 DeepSeek 目录条目的 `auto_review_model_override` 固定为 `deepseek-flash`，避免提权审批把 `codex-auto-review` 发给 DeepSeek provider（DeepSeek 只接受 deepseek 系列模型名）。父模型变化后运行 `repair`。

桌面 CLI ≥0.146 起，collab/spawn_agent 命名空间被限定在 ChatGPT 后端：原生 `spawn_agent(agent_type="DeepSeek")` 创建的子线程强制走 `openai` provider，自定义 `model_provider` 被忽略，非 OpenAI 模型会直接收到 model-unsupported 400。因此日常 DeepSeek 执行段统一走受管执行面——由本 Skill 管理的 `codex exec` 子进程（prewalk skill 中记录了确切命令）：

```text
codex exec --skip-git-repo-check -s workspace-write -C "<workspace>" \
  -m deepseek-flash -c model_provider="deepseek" -c model_reasoning_effort="medium" - < "<handoff-package>"
```

该形态在 0.144–0.154 均可用：它只依赖顶层 `model_provider` 覆盖，不经过 collab 命名空间。派发方（如 prewalk skill）从 `status` 获取缓存运行时路径并显式设置 `CODEX_HOME`。

### Windows 执行面组件（实测于 0.154.0-alpha.6.2）

沙箱命令的创建链要求以下四个文件同目录存在，缺一不可：

- `codex.exe`
- `codex-code-mode-host.exe`
- `codex-windows-sandbox-setup.exe`
- `codex-command-runner.exe` — 沙箱统一执行（unified exec）实际由它拉起；缓存运行时缺它时，一切沙箱命令（无论工作区路径是否含空格）都会以 `CreateProcessWithLogonW failed: 2` 失败。这是最隐蔽的一个，因为缺前两个文件会在启动阶段就报错，缺它要等到第一次执行命令才暴露。

`setup`/`repair`/`test` 会把四个文件一起缓存；`test`/`repair` 的工作链验收（临时工作区 `workspace-write`：写探针文件 + 跑命令 + 口令返回）正是为了覆盖这条链。组件齐全后，带空格的工作区路径（如 `G:\D\RK3568\Android video analyser international`）下 `exec_command` 与 `apply_patch` 均实测正常，无需 `danger-full-access`。

## API Key

API Key 可由用户在聊天中提供。管理程序从标准输入读取，不写入命令参数、临时文件、配置文件或测试结果。macOS 将密钥保存到 Keychain，Windows 将密钥保存到 Credential Manager。

启动 Codex 验收子进程时会从该子进程环境中移除大小写形式的 `CODEX_API_KEY`，防止它覆盖桌面 ChatGPT 登录并把父模型请求路由到错误的 Provider；不会修改用户级或系统级环境变量。

不要在最终回复、日志摘要、异常信息或测试夹具中重复 API Key。

## 配置事务

写入前创建带时间戳的备份。程序使用进程锁避免并发修改，先生成候选配置并用 TOML、JSON 解析验证，再原子替换目标文件。写入、卸载或实时测试失败时，恢复本次事务开始前的文件。

已存在但不属于本 Skill 的冲突配置不会被静默覆盖；完全兼容的现有配置可以被采用，并在结果中标记 `adopted_existing`。

## 视觉输入

DeepSeek-V4.1-Flash 原生接受图片和截图。视频仍由父 Agent 先转成文字任务包。
