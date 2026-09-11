# 安全政策

[English](SECURITY.md) | 简体中文

## 发布与支持状态

`ntulearn-skill` 尚无受支持的公开发行版。当前源码处于预发布阶段，公开发布门槛仍为 **NOT READY**。安全修复以当前维护的源码为评估对象；对尚未发布的版本，不作兼容性或响应时间承诺。

## 报告安全漏洞

[S0rryHorizon/ntulearn-skill](https://github.com/S0rryHorizon/ntulearn-skill) 已启用 GitHub **Private vulnerability reporting（私密漏洞报告）**。仓库已公开实验版源码，但尚未发布受支持的软件包发行版。启用这一报告渠道本身不构成软件发布。

对于普通缺陷、功能请求和不涉及敏感信息的修复，请提交公开 issue 或 pull request。

如果发现尚未修复的安全漏洞，请勿在公开 issue 或 pull request 中披露细节。请使用 GitHub 的 **Private vulnerability reporting**：打开 **Security and quality → Advisories → Report a vulnerability**，或直接使用[私密报告表单](https://github.com/S0rryHorizon/ntulearn-skill/security/advisories/new)。按提示登录 GitHub。[Advisories 页面](https://github.com/S0rryHorizon/ntulearn-skill/security/advisories)也提供同一报告入口。

请避免在报告中包含真实凭据、私人数据或其他不必要的敏感信息。绝不在公开 issue 或 pull request 中包含凭据、私人数据、带认证信息的 URL、私人课件、日志、数据库内容或真实 NTULearn 复现材料。

请提供简洁的影响说明、受影响的提交版本、使用合成数据的复现步骤，以及建议的缓解措施。将所有真实标识符和内容替换为虚构值。如果某份私人材料确实不可缺少，请先向维护者询问传递方式；不要将其附在 issue、commit 或 pull request 中。

## 安全边界

项目围绕以下措施设计：

- 运行数据默认存放在用户本地的 `~/.ntulearn-skill/` 目录树中。使用仓库内路径时，必须显式指定 `.local/` 下的路径，验证实际生效的 Git ignore 保护，并确认没有已跟踪的运行数据。无法验证 Git 边界时，程序会以不泄露私人信息的错误拒绝操作；仅凭目录名并不足以放行。这些检查不能防止后续修改 ignore 规则或强制暂存，因此仍须审查暂存内容的隐私边界。
- 远端访问通过带类型的会话／来源契约和采用允许列表的 `ReadOnlyTransport` 进行；不支持或格式错误的操作在发送前即失败。
- 签名 URL 和预览 URL 是临时传输细节，不得作为身份标识、元数据或日志持久化。
- 公开结果封装采用有限的错误类别，默认不包含本地文件系统路径。
- 当远端读取不完整、失败或遗漏某个条目时，保留本地原件、版本和来源追溯信息。

软件包不会提取浏览器凭据、复制 cookie、自动执行 SSO 或续期会话。它不会对运行数据库或下载资源进行静态加密，PDF/DOCX 解码器也未进行进程隔离。请据此保护主机账户和私人运行目录。合成测试通过，不代表真实 NTULearn 传输或每种文档格式均已验证安全性和兼容性。

## 意外泄露

如果私人材料进入工作树、构建产物或 Git 历史，请停止共享仓库，并私下通知维护者。在本地保留足以识别受影响路径和提交版本的证据，但不要将敏感值复制到新报告中。从最新文件树删除文件并不会将其从 Git 历史中删除；恢复发布前，必须同时审计历史和已生成的发行产物。
