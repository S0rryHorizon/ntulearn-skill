# 贡献指南

[English](CONTRIBUTING.md) | 简体中文

感谢你帮助改进 `ntulearn-skill`。项目仍处于预发布阶段，公开发布仍须由人明确决定。贡献必须遵守 [AGENTS.md（英文）](AGENTS.md)及已接受架构中的本地优先、只读和隐私边界。

## 开发环境设置

软件包声明支持 Python 3.11 及以上版本，当前 CI 支持矩阵覆盖 CPython 3.11 至 3.14。在本地检出的仓库中安装项目及开发依赖：

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

如需使用 CI 所用的锁定开发环境：

```console
uv sync --frozen --extra dev
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src
uv run --no-sync python -m pytest -q
uv run --no-sync python -m build --no-isolation
uv run --no-sync python scripts/check_public_artifacts.py --tracked --artifacts dist
```

仓库锁文件和 CI 工作流是依赖及所支持 Python 版本检查的依据。当前已执行的验证证据及限制见[打包验证记录（英文）](docs/development/packaging-validation.md)。

## 修改代码之前

请阅读[架构概览（英文）](docs/architecture/overview.md)、[数据边界（英文）](docs/privacy/data-boundaries.md)、[实施计划（英文）](docs/architecture/testing-and-implementation.md)和[能力台账（英文）](docs/development/capability-ledger.md)。

独立 Core 必须保持与 Codex、ChatGPT 及其他 LLM 产品无关。产品特有行为应置于稳定 Core API 之上的轻量适配器中。不得将 `UNKNOWN`、`HYPOTHESIS` 或有范围限制的 `OBSERVED` 来源行为提升为受支持的契约。在获得授权的证据验证该行为之前，应添加能力门控、安全回退或明确限制。

## 测试数据与私人状态

仅使用虚构数据。`PH0000 — Example Physics Course` 是可接受的合成标签。绝不将真实课程记录复制、稍作修改或匿名化后用作测试 fixture。

不得提交以下任何内容：

- 凭据、cookie、token、会话数据、CSRF 值或认证请求头。
- 真实姓名、学生标识符、课程标识符、课程元数据或课件。
- 带认证信息的 URL、请求／响应 capture、私人日志或错误正文。
- 运行数据库、索引、缓存、下载文件或生成的私人导出文件。

测试应使用仓库检出目录之外的临时目录。如果开发运行数据必须放在源码旁，只能放在被 ignore 的 `.local/` 目录下，并显式传入路径。运行时校验要求 Git 确认实际生效的 ignore 保护，并拒绝已跟踪的运行数据；将目录命名为 `.local` 并不能建立这种保护。应用不会修改 ignore 规则。`tests/fixtures/synthetic/` 下对已跟踪数据库的有限例外，仅适用于经过审查、可证明为合成数据的 fixture。

## 修改要求

- 远端操作保持只读，在发送前拒绝未知请求结构。
- 保留带类型的来源身份、来源追溯信息、明确的覆盖范围和历史数据。
- 不从不完整或失败的遍历推断删除或完全不存在。
- 除非调用方明确要求，否则机器输出中不得包含本地路径。
- 将失败映射为有限且不泄露私人信息的错误类别；绝不回显来源载荷、私人查询文本、请求头、URL 或异常细节。
- 持久化 schema 发生变化时添加迁移，不要重写已接受的迁移。
- 稳定接口、限制或门控发生变化时，更新公开文档。

测试应在不访问网络、不使用凭据的条件下覆盖有意义的边界和失败情形。运行上述命令及相关专项测试。完整合成测试套件、lint、格式检查、严格类型检查、构建及公开产物审计是必要基线。

## 审查清单

将修改交给维护者之前：

- 检查完整 diff 和每个暂存文件。
- 确认每个标识符、URL、时间戳、文件名和文本示例均为合成内容。
- 确认测试没有发起网络请求，也未使用浏览器或账户状态。
- 对照 `--help` 和实际签名核验新增命令及 Python 示例。
- 对已跟踪文件和构建产物运行仓库的发布／隐私审计。
- 只记录实际运行的检查；明确保留被阻塞或未经验证的门槛。

未经维护者另行明确授权，不得创建 remote、push、发布软件包或宣称项目已具备发布条件。

提交贡献即表示你同意贡献内容遵循本仓库的 [MIT License（英文）](LICENSE)。
