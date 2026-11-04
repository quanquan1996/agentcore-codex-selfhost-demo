# DEVELOPMENT

面向要修改这个仓库的你。只想跑起来看效果，请看 [README.md](README.md)。

## 部署路径：只有一条

本仓库只有一条部署路径：`deploy.sh` → `scripts/deploy.py`，直接调用 boto3，没有 CloudFormation 或 CDK 版本。

这样做是为了让每一步的 API 调用和 IAM 权限都摆在明面上，和 [博客](blog/agentcore-codex-selfhost-zh.md) 逐段对应。本仓库没有验证 Amazon S3 Files、AgentCore Policy、AgentCore Identity API key 凭证提供方这几类资源的 CloudFormation 支持情况。你要改成 IaC 的话，先用 `aws cloudformation describe-type` 逐个确认。

`deploy.py` 把每一步的结果写进 `.state.json`。某一步失败后修好再执行 `./deploy.sh`，已完成的步骤会跳过。`cleanup.py` 也读这个文件，所以**不要手动删除 `.state.json`**，否则清理脚本不知道要删什么。

## 代码结构

```
deploy.sh / ask.sh / exec.sh / test.sh / cleanup.sh   薄封装：准备 .venv 后转发给 scripts/
scripts/_env.sh       建 .venv；探测 boto3 是否认识 s3files 和 Runtime 的 filesystemConfigurations，不认识就升级
scripts/deploy.py     全部资源的创建与更新（幂等，状态在 .state.json）
scripts/invoke.py     ask / exec / stop 三个子命令，演示脚本和端到端测试共用
scripts/e2e_test.py   五步端到端验证，每步打印 PASS/FAIL，退出码 = 失败数
scripts/cleanup.py    按依赖顺序删除全部资源
agent/                容器：Dockerfile、server.py、Codex 的 config.toml 与 AGENTS.md、gateway-mcp.sh
repo_api/             模拟的代码仓库 API（Lambda 处理函数 + OpenAPI 描述）
docs/                 架构图及其生成脚本（architecture_src.py，matplotlib）
blog/                 对外博客
```

## 镜像怎么构建

`deploy.py` 不调用本地 Docker：

1. 把 `agent/` 下的文件按文件名排序、固定时间戳打成 zip。zip 内容的 SHA-256 前 12 位作为镜像标签的后缀（`0.166.0-<hash>`）。
2. 标签和 `.state.json` 里记录的一样，说明 `agent/` 没改，跳过构建。
3. 否则上传 zip 到 `codex-demo-build-<账号>-<区域>` 桶，启动 ARM 架构的 CodeBuild 项目 `codex-demo-image`（`amazonlinux-aarch64-standard:3.0`，特权模式）执行 `docker build` 并推送到 ECR。
4. 最后一步 `sync_runtime` 发现 Runtime 用的镜像和当前标签不一致，就调用 `UpdateAgentRuntime`。

注意第 4 步会产生一个新的 Runtime 版本，**所有会话的 `/mnt/workspace` 都会被重置**。rollout 会从 S3 Files 归档恢复，但项目文件不会。

要升级 Codex，改 `deploy.py` 里的 `CODEX_VERSION`。它通过 `--build-arg` 传入 Dockerfile，同时也是镜像标签的一部分。

## 常见修改

| 你想做的事 | 改哪里 |
| --- | --- |
| 换模型 | 部署前设置环境变量 `CODEX_MODEL`（默认 `global.openai.gpt-6.1-sol`）。已部署的 Runtime 需要在控制台或用 `UpdateAgentRuntime` 修改环境变量 |
| 接真实的 GitHub 等 API | 换掉 `repo_api/openapi.json`（`servers.url` 直接写真实地址，`gateway` 步骤里对 `${API_URL}` 的替换就不再起作用），把 `identity` 步骤里的 token 换成你的 token；模拟 API 那一步可以保留，也可以从步骤列表里去掉，但要同时去掉 `gateway` 步骤对 `state["api_url"]` 的引用 |
| 放行更多工具 | 改 `deploy.py` 里 `policy` 步骤的 Cedar 语句。工具名格式是 `<target 名>___<operationId>` |
| 改 agent 的行为约束 | 改 `agent/AGENTS.md`，重新执行 `./deploy.sh` |
| 加 MCP 工具 | 在 Gateway 上加 target，不要往镜像里放凭证 |

## 踩过的坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `codex exec` 在容器里一直挂起 | stdin 没关，Codex 在等输入 | `subprocess.run(..., stdin=DEVNULL)` |
| Codex 返回 401，重试 5 次后 `turn.failed` | Bedrock 的 OpenAI 兼容端点还要对 `arn:aws:bedrock:<region>:<account>:project/default` 授权 `bedrock:InvokeModel` | 执行角色加这一条 |
| `server.py` 只返回 `exit_code: 1`，没有任何信息 | 错误在 JSONL 的 `turn.failed` 事件里 | 解析 `turn.failed` 并返回 `error` |
| Codex 说"没有可用的 MCP 工具"，代理手动运行却正常 | Codex 以精简环境启动 stdio MCP 子进程，`GATEWAY_URL` 和 `AWS_REGION` 不在其中 | `config.toml` 的 `[mcp_servers.gateway]` 加 `env_vars = ["GATEWAY_URL", "AWS_REGION"]`。凭证来自 Runtime 元数据端点，不需要转发 |
| `CreateAgentRuntime` 报缺少 `s3files:GetAccessPoint` / `s3files:ListMountTargets` | 创建时以不带访问点条件键的方式校验这两个动作 | 单独写一条不带 `Condition` 的语句 |
| `UpdateGateway` 关联策略引擎报 `Access denied while calling GetPolicyEngine` | Gateway 服务角色缺策略引擎权限 | 加 `GetPolicyEngine`、`AuthorizeAction`、`PartiallyAuthorizeActions` |
| `UpdateAgentRuntime` 报 `cannot modify requireServiceS3Endpoint` | 原样回传了 `GetAgentRuntime` 的 `networkConfiguration` | 更新前删掉 `networkModeConfig.requireServiceS3Endpoint` |
| `InvokeAgentRuntimeCommand` 报 `cd: too many arguments` | 命令不经过 shell 解析 | 用 `/bin/sh -c '<命令>'` 包装（见 `invoke.run`） |
| `InvokeAgentRuntimeCommand` 的报错"看起来像空输出" | 流里的异常是 stream 成员，不会抛出 | 显式检查以 `Exception` 结尾的键 |
| `cleanup.sh` 删除 VPC 时等很久，最后提示稍后重跑 | AgentCore Runtime（类型 `agentic_ai`）和 S3 Files 的网络接口在资源删除后才异步释放，实测超过 15 分钟 | 计费的 NAT 网关已先删掉，剩下的 VPC 不收费；`.state.json` 会保留，稍后再执行一次 `./cleanup.sh` |
| 在非 CloudShell 的 Ubuntu 上 `deploy.sh` 报 `ensurepip is not available` | 系统没装 `python3-venv` | `apt install python3-venv`，或先用 `uv venv --seed .venv` 建好 venv |

## 行为细节（改代码前先知道）

- **Policy 会过滤工具列表**。挂上策略引擎后，`tools/list` 只返回被允许的工具，Codex 根本看不到合并、删分支这类工具。直接 `tools/call` 被拒的工具返回 `-32002 Tool Execution Denied`。被拒的请求不会到达后端，可以在 `/aws/lambda/codex-demo-repo-api` 日志里核对。
- **续接只依赖 rollout 文件**。`CODEX_HOME` 留在本地盘，只有 `CODEX_HOME/sessions` 用符号链接指向 `/mnt/workspace/.codex-sessions`。SQLite（WAL 模式）放到会话存储上有风险：会话存储的文件锁不跨 stop/resume 保留。
- **S3 Files 归档按 `runtimeSessionId` 分目录**（`/mnt/archive/rollouts/<会话 ID>/`），会话 ID 取自请求头 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`。S3 Files 是跨会话共享的，多租户场景请为每个租户建独立的访问点。
- **`/ping` 的 `time_of_last_update` 只在状态变化时更新**。每次都写当前时间会让空闲超时永不触发。
- **同一个 `thread_id` 串行处理**（`server.py` 里按 thread 加锁）。
