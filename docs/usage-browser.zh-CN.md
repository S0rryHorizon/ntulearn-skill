# 浏览器辅助的 NTULearn 使用方式

[English](usage-browser.md) | 简体中文

当前受支持的实验路径将实时采集与本地检索分开。实时采集由宿主通过已安装的浏览器 Skill 辅助完成。独立 `ntulearn` CLI 命令和日常问答 Skill 读取私人本地资料库；它们不会登录 NTULearn 或控制浏览器。

## 已验证的宿主环境

当前有限范围的试用使用 macOS 上的 Codex 和已连接的 Chrome 浏览器。请通过 Chrome 的正常界面登录并完成 MFA。代理会在采集前检查当前登录状态；上一次运行保存的登录状态不能证明当前会话可用。

按仓库的[安装说明](../README.zh-CN.md)安装 Python 项目。将 `skills/ntulearn-browser` 和 `skills/ntulearn` 安装到宿主的 Codex skills 位置（通常分别为 `~/.codex/skills/ntulearn-browser` 和 `~/.codex/skills/ntulearn`），然后按宿主要求重新加载 skills 或开始新任务。源码发行包包含这些目录，但单独安装 Python wheel 不会安装 Codex Skill 或浏览器连接。

## 日常使用流程

1. 按 README 配置私人运行根目录。
2. 在 Chrome 中打开 NTULearn，正常登录并完成 MFA。
3. 请 `ntulearn-browser` Skill 刷新一个明确指定的小范围课程。宿主使用可见页面和普通下载控件，再将范围受限的私人 capture 传入现有 Core 同步流程。
4. 向 `ntulearn` Skill 提出日常问题，或使用下面的本地 CLI 命令。
5. 对重要日期、要求、考试范围或地点，请检查返回的精确来源。将事件提取结果视为预览，保留不确定性和冲突。

正常 Skill 流程不要求用户编写连接对象、复制凭据或准备 capture 清单。如果 Chrome 不可用或已退出登录，代理会报告这一宿主状态，并请求用户正常交互式登录，而不会索要浏览器机密。

采集后，常用本地命令包括：

```text
ntulearn courses
ntulearn library-status --course COURSE_KEY --freshness cache-only
ntulearn materials COURSE_KEY --freshness cache-only
ntulearn recent-materials COURSE_KEY --days 14 --freshness cache-only
ntulearn search "search words" --course COURSE_KEY --freshness cache-only
ntulearn announcements COURSE_KEY --freshness cache-only
ntulearn assessments COURSE_KEY --freshness cache-only
ntulearn events --course COURSE_KEY --show-conflicts --freshness cache-only
ntulearn source LOCATOR_KEY
ntulearn resource RESOURCE_KEY
```

`COURSE_KEY`、`RESOURCE_KEY` 和 `LOCATOR_KEY` 是前面命令返回的不透明本地键。`source` 和 `resource` 本身就是本地操作，不接受 freshness 选项。本地查询可跨进程重启使用，无须连接浏览器。如果本地处理超过默认队列上限，浏览器辅助流程可用 `sync --max-jobs` 继续处理；应检查 `library-status`，不要假定第一次有限运行就处理完了所有任务。

部分覆盖或过期的查询使用退出码 2，但仍可能包含有用条目。覆盖失败时可能使用退出码 1，同时保留条目。请检查结果中的覆盖范围、新鲜度、冲突和警告，不要将所有非零退出码都视为空答案。

## 证据与覆盖边界

当前私人试用存储了 66 份原件，解析了 65 份受支持的 PDF/DOCX 文件。一份 PPTX 已存储，但尚不支持解析。58 个片段被标记为可能需要视觉审阅；其中两个已有有限范围的补充描述，56 个尚未审阅。普通页面正文、文件夹说明、嵌入式附件和外部交互模块仍覆盖不完整或不受支持。这些数字描述的是一个有限范围的资料库状态，不能代表 NTULearn 的普遍覆盖情况。

事件提取有两组冻结评估。原始来源内评估集找回了 8/8 条事件提及，得到 26/27 个字段评分通过和 27/27 项来源追溯检查通过。独立选取的评估集找回了 13/16 条事件提及，没有额外事件提及，但完全正确的提及数为 0/16。事件类型、时间、地点和弱身份标识错误仍然存在。除非来源字段支持，不得将检索到的事件表述为已确认的安排。详见[日常资料库验证（英文）](development/daily-library-validation.md)。

## Capture 契约与新鲜度

[带类型的 capture 契约（英文）](browser-capture-contract.md)是宿主采集与常规 Core 之间的私人集成边界。它携带范围受限的观察记录、来源路径、采集时间和下载文件，主要供维护者及宿主集成代码使用；日常用户应使用浏览器 Skill。

宿主集成代码必须把 capture 目录创建为仅所有者可访问的 `0700` 目录，并把 manifest 创建为 `0600` 的普通文件。集成代码应在保存 capture 内容前调用 `prepare_browser_capture_directory`，并用 `write_browser_capture_manifest` 写入最终 JSON；不得先用默认权限写入 manifest，再事后 chmod。导入器会拒绝符号链接、group/other 权限位以及过大的 manifest，并以不回显 capture 路径或正文的固定提示说明修复要求。

`UNKNOWN`：尚未在每一种受支持浏览器宿主上验证原生下载路径及其权限行为。宿主集成必须让生成的 manifest 通过常规 CLI 导入；如果无法安全创建，应报告该宿主的具体能力缺口。

Capture 代表其原始观察时间。重放它不能证明进行了新的网络读取。有限遍历仍是 PARTIAL，遗漏资源不构成删除证据。根据未变化的元数据复用资源，仅是一种内容未变的假设；哈希验证需要实际下载。绝不通过重命名旧 capture 或重新标记旧字节来制造新鲜证据。

如果 capture 过期，较早的元数据可能仍可导入，但资源流式读取会明确失败。缓存资料仍可通过本地查询访问。如果可见页面或工具拒绝某条访问路径，应停止该路径并报告缺口；不要改用终端请求、复制凭据或绕过浏览器控件。

原始 API 适配器和自动 SSO 续期仍是独立且未经验证的能力。这次有限范围的宿主辅助试用不依赖它们，也没有验证它们。只读访问不包括作答测验、提交作业、修改完成状态、访问名册、发送消息或任何站点编辑。Capture、下载文件和验证证据始终保存在所配置运行根目录下的私人区域。
