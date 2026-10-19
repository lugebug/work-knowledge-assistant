# 个人工作知识库

把自己的工作聊天、文档和任务整理成人员、项目、工作进展、下一步和待判断问题。数据保存在本人 Windows 电脑，Codex 负责理解资料和推进工作，浏览器工作台负责查看和维护。

当前为 **0.1.0-alpha.2，Windows + Codex 试用版**。安装包是带安装入口的源码 ZIP，需要联网获取依赖；不是免登录、全版本微信通用的离线 EXE。

本版采用[真正增量整理](docs/incremental.md)：只处理新资料、修订和人工答复，先更新相关工作再汇总项目；内部复核日期不再触发全量重算。旧版升级会先备份再迁移，已有定期任务按[更新指南](docs/update.md)同步新规则。

## 给 Codex 的安装指令

下载 GitHub 仓库或 Release ZIP，解压到一个固定目录，在 Codex 中打开该目录，然后发送：

> 请按 AGENTS.md 和 docs/first-run.md 帮我安装这套个人工作助手。自动处理环境和初始化，指导我完成自己的微信、飞书授权及工作范围选择，验证首次采集和整理结果，再帮我设置定期更新。能确定的直接处理，需要我操作的登录和业务选择集中告诉我。

## 安装入口

```powershell
.\install.ps1 -Name '你的称呼' -Since '2026-10-01'
```

日期是示例，换成本人希望归档的起点。安装器检查 Python、建立独立环境、安装锁定的读取依赖和官方飞书 CLI、创建空库并启动工作台。缺少 Python/Node.js 时尝试 Windows 的 winget；不可用时明确提示需要补装的环境。安装不会自动登录、默认读取聊天或注册定期任务。

仅安装工作台可用 `-CoreOnly`。验证安装使用 `doctor.ps1`，启动使用 `start.ps1`，停止本机工作台使用 `stop.ps1`。地址以启动输出为准。

个人数据库默认保存在 `%LOCALAPPDATA%\WorkKnowledgeAssistant\default`，也可用 `-DataDirectory` 指定新的空目录。`config.local.json` 是本机配置，Git 默认排除。重复安装保留现有个人库、范围及决定。

## 可以做什么

- 读取已批准的微信文字、飞书聊天、可访问文档、妙记文字和任务，保留来源、版本、时间范围与缺口。
- 按证据更新已有工作，核对原完成条件，明确结果直接推进或结束，独立后续另外承接。
- 展示当前情况和下一步，分析工作及项目优先级，让用户处理真正影响行动的歧义。
- 在用户的 Codex 中设置周期采集与整理；按本人要求配置飞书提醒。

微信首版只开放已验证的 Windows **4.1.15.55** 读取适配，其他版本显示待适配。只读取本机已有历史，不能凭安装恢复手机独有或本机缺失的消息。飞书通过本人登录的官方 CLI 接入，可读范围取决于本人和应用权限。

安装完成、账号接通、首次采集、语义整理和到点提醒分别检查。当前电脑的验证不能代替朋友新电脑的实际验收。详见[接入说明](docs/connectors.md)、[首次运行](docs/first-run.md)、[日常工作流](docs/daily-workflow.md)和[更新与备份](docs/update.md)。

## 开发与打包

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 scripts/make_release.py
```

只按明确文件清单打包，产物在 `dist`，包含 ZIP 和 SHA-256。仓库不包含个人资料；依赖许可证及微信格式参考见 [NOTICE.md](NOTICE.md)。当前未选择整个项目的开源许可证。

官方资料：[Codex 项目指引](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[定期任务](https://learn.chatgpt.com/docs/automations?surface=app)、[飞书 CLI](https://github.com/larksuite/cli)。
