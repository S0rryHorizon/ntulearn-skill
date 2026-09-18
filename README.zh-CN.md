# ntulearn-skill

[English](README.md) | **简体中文**

`ntulearn-skill` 是一个本地优先（local-first）的 Python 库和命令行工具，
用于建立私有的 NTULearn 课程镜像。课程元数据、下载的资源、解析后的 PDF/DOCX 文本、
确定性的 FTS5 检索数据、公告、考核信息、事件证据、冲突及同步状态都保存在私人运行目录中。

> **当前状态：能力有限的实验性检索候选版。** 当前版本支持通过已安装的 Skill
> 进行私人本地日常试用，以及在受支持宿主协助下进行有范围限制的实时采集。
> 实验源码已在 GitHub 公开，尚未发布软件包版本。事件提取仍属于预览功能：
> 回答必须保留来源链接和不确定字段；重要日期、要求及地点仍需核对原文或人工确认。
> 仓库与发行包检查结论仅适用于各自记录的验证范围；软件包发布需要另行决定。
> 详见[日常资料库验证记录（英文）](docs/development/daily-library-validation.md)
> 和[发布检查清单（英文）](docs/development/public-release-checklist.md)。

## 已实现的能力

- 独立于 LLM 的 `CoreService` 和 `ntulearn` CLI。
- 私有 SQLite 元数据、FTS5 索引及按内容寻址的资源存储。
- 带来源定位的 PDF、DOCX 解析；明确标记不支持的格式。
- 确定性的词法检索，并按限定范围读取相邻文本片段。
- 有来源支撑的公告、考核信息及候选事件。
- 以证据溯源为先的事件协调处理，保留未解决的冲突和本地人工决定。
- 通过注入 `SessionProvider`、`SourceProvider` 实现有范围限制的增量同步。
- 带版本的 JSON 结果封装，包含新鲜度、覆盖范围、冲突、来源、警告和脱敏错误。
- 基于同一 Core API 的轻量 Python `CodexToolDispatcher`。
- 接入正常 Core/CLI 流程的宿主浏览器采集 provider，搭配用于授权采集的
  [Codex 浏览器 Skill（英文指令）](skills/ntulearn-browser/SKILL.md)。
- 独立的[本地问答 Skill（英文指令）](skills/ntulearn/SKILL.md)，通过同一组
  cache-only Core 接口回答限定范围内的中文或英文日常问题。

Codex dispatcher 是 Python 集成接口。浏览器 Skill 需要已连接且受支持的宿主；
本仓库不会安装浏览器插件、运行 LLM agent，也不提供 ChatGPT adapter。

## 当前限制

已验证的实时采集路径使用 macOS 上的 Codex、已连接的 Chrome、正常可见的页面读取和下载，
以及已安装的浏览器 Skill。独立 CLI 和日常问答 Skill 只读取私人本地资料库，
不会登录或操控浏览器。本项目不包含自动 SSO、浏览器凭据提取、Cookie 复制或会话续期。
详见[浏览器使用指南](docs/usage-browser.zh-CN.md)。

独立的 raw API 集成仍未验证，但它不是上述有限浏览器辅助试用的前提；自动 SSO
和未支持格式也是如此。这些延后能力限制了兼容性声明的范围，不妨碍评估已记录的本地检索路径。

检索采用词法匹配，不是语义检索。内置解析器支持 PDF 和 DOCX，尚不支持 PPTX 文本提取。
普通页面正文、嵌入附件、外部模块、扫描文档回退处理质量、复杂 DOCX 表格、大列表分页，
以及若干远端新鲜度机制，仍处于部分支持、未验证或延后状态。
已记录的私人试用保存了 66 份原件，并解析了其中 65 份受支持的 PDF/DOCX。
58 个片段被标记为可能需要视觉复核，其中 2 个有有限补充，56 个尚未复核。
这些数字表示处理覆盖情况，不代表课程资料完整。

两组冻结的事件评估说明了为何提取仍是预览功能：原始来源内样本找回 8/8 个事件提及，
26/27 个评分字段正确，27/27 项来源溯源检查通过；另一组独立选取的样本找回 13/16 个提及，
没有额外事件提及，但 16 个提及中没有一个所有字段都正确。
事件类型、时间、地点和身份识别证据不足等问题仍存在。
解释见[日常资料库验证记录（英文）](docs/development/daily-library-validation.md)，
更完整的证据边界见[能力记录（英文）](docs/development/capability-ledger.md)。

## 从本地源码安装

项目声明支持 Python 3.11 及以上版本。已记录的集成后续验证在 Python 3.12 上运行了
556 项测试、静态检查及软件包构建。较早的 Phase 5 曾在 Python 3.11–3.14 上分别运行
463 项测试；这是历史证据，不是当前后续版本的兼容性矩阵。这些历史本地结果不表示
当前 [GitHub CI](https://github.com/S0rryHorizon/ntulearn-skill/actions) 已通过。
在本仓库目录中运行：

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
ntulearn --help
```

本项目尚未发布到软件包索引。可通过路径安装本地构建的 wheel；开发流程和具体验证命令见
[使用与集成指南（英文）](docs/usage.md)及[贡献指南](CONTRIBUTING.zh-CN.md)。

## 运行离线 synthetic 演示

激活本地虚拟环境后，安装包含 `reportlab` 的可选开发依赖，再运行独立示例：

```console
python -m pip install '.[dev]'
python examples/offline_demo.py
```

每次运行都会新建并删除临时私人目录。示例虚构一门课程，生成三页 PDF 和本地采集清单，
经现有来源 provider、Core 同步与检索、Codex dispatcher 展示第 2 物理页的命中、
资源版本及原文来源定位。重复同步后仍只有一个版本。未命中的词只表示本地没有找到；
示例会显示来源覆盖率为 `PARTIAL`、整体完整度为 `UNKNOWN`，因此不能推断远端课程没有该内容。
安装依赖后，运行不需要账号、网络、真实课程资料，也不读取现有的 `~/.ntulearn-skill/` 资料库。
它验证本地检索链路；真实实时采集仍需要[浏览器使用指南](docs/usage-browser.zh-CN.md)
所述的已授权受支持宿主流程。

## 查询私人本地资料库

默认运行目录为 `~/.ntulearn-skill/`。覆盖顺序依次为：显式 `--root PRIVATE_ROOT`、
`NTULEARN_DATA_DIR` 环境变量、私人宿主配置文件 `~/.ntulearn-skill/config.json`
中的绝对路径 `runtime_root`，最后才是默认目录。
Git 仓库内的路径默认被拒绝；只有显式指定到该仓库 `.local/` 下，且 Git 确认私人边界
被有效忽略、其中没有已跟踪的运行数据时才会放行。仅有 `.local` 目录名并不够。
无法确认 Git 边界时会返回脱敏错误。检查范围见[数据边界（英文）](docs/privacy/data-boundaries.md)。

```console
ntulearn --json courses
ntulearn library-status --course 1 --freshness cache-only
ntulearn materials 1 --freshness cache-only
ntulearn recent-materials 1 --days 14 --freshness cache-only
ntulearn search "synthetic optics" --course 1 --neighbors 1
ntulearn search "synthetic optics" --course 1 --current-only --freshness cache-only
ntulearn events --course 1 --show-conflicts
ntulearn source 7 --kind source_locator --context-window 1
ntulearn resource 3
```

`search --current-only` 只匹配本地资料库中资源的当前版本；默认搜索仍包含历史版本。
它不会刷新 NTULearn，也不能证明本地版本就是远端最新版本。Codex dispatcher 可传入
`{"query": "synthetic optics", "current_only": true}` 使用同一选项。

如果所选课程的本地处理超过默认每轮 64 个 job 的上限，已配置的浏览器 Skill 流程
可以用更大的 `--max-jobs` 重复同步，继续处理幂等队列。裸 CLI 没有实时来源引擎。
用 `library-status --course 1` 查看剩余及失败的本地任务。

命令中的整数是从前序结果取得的本地不透明键，不是 NTULearn ID 或课程代码。
例如虚构课程显示的代码可以是 `PH0000`，但 CLI 参数仍应使用返回的 `local_key`。

结果默认不包含本地路径。只有显式使用 `resource --include-local-path` 或
`visual prepare --include-local-path` 才会返回相应路径。
查询命令默认使用 `cache-only`，在该策略下不会访问远端来源。

CLI 退出码含义固定：`0` 表示完整结果，`1` 表示操作失败，`2` 表示部分、过期或未知结果，
`3` 表示完整但为空的结果，`64` 表示用法错误。自动化应同时检查退出码和带版本的 JSON 结果封装。

## Python API 与来源集成

公共 Python 入口是 `ntulearn_skill.core.api.CoreService`。
仅本地使用可以从 `CoreService.from_runtime()` 开始。
正常实时采集由受支持宿主上的浏览器 Skill 编排，再进入同一 Core/CLI 流程。
私人 browser-capture manifest 是集成边界，日常用户无需手工准备。
Raw API 或自定义集成也可提供由类型化 `SessionProvider`、`SourceProvider` 合同组成的
`SyncEngine`。独立 NTULearn API adapter 接受受约束的 `ReadOnlyTransport`，
但不会建立或恢复认证会话。

已验证的方法签名和最小组合示例见[使用与集成指南（英文）](docs/usage.md)。
来源集成必须遵守[认证与来源边界（英文）](docs/architecture/authentication-and-source-boundaries.md)
中规定的只读策略。

## 隐私与安全

仓库只允许包含源码、schema、文档和完全虚构的示例。
凭据、Cookie、token、认证后的 capture、真实课程标识与元数据、下载的资料、
私人数据库、索引、缓存和日志必须与 Git 隔离。
推荐运行目录为 `~/.ntulearn-skill/`；被有效忽略的 `.local/` 仅作为开发时的替代位置。

创建测试资料或分享诊断信息前，请先阅读[数据边界（英文）](docs/privacy/data-boundaries.md)。
安全问题报告请遵循[安全政策](SECURITY.zh-CN.md)。

## 项目文档

中文使用入口如下；尚未翻译的技术文档已注明为英文。

- [浏览器使用指南](docs/usage-browser.zh-CN.md)
- [使用与集成指南（英文）](docs/usage.md)
- [架构概览（英文）](docs/architecture/overview.md)
- [实施状态（英文）](docs/development/implementation-status.md)
- [打包验证（英文）](docs/development/packaging-validation.md)
- [项目阶段（英文）](docs/development/phases.md)
- [贡献指南](CONTRIBUTING.zh-CN.md)
- [安全政策](SECURITY.zh-CN.md)

源码使用 [MIT License](LICENSE) 授权；许可证原文保留英文。
