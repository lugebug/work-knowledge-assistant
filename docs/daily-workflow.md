# Codex 日常整理顺序

所有命令使用当前项目 `.venv\Scripts\python.exe -X utf8`。省略数据库参数时读取本安装的 `config.local.json`。输入、批次和报告放在个人数据目录，使用新文件名，不重放旧判断覆盖新情况。

1. **读当前状态。** 运行 `app/raw_manage.py status`、`app/periodic_update.py status`、`app/manage.py review-status`。确认本人账号、来源授权和失败缺口。提醒已启用时先运行 `app/reminders.py cloud-sync`，未启用时跳过。
2. **采集范围内资料。** 运行 `app/periodic_update.py collect`。飞书全范围发现依赖本人的范围确认；微信只处理已批准的会话。失败保留原成功位置，不把零条或本机缺历史当覆盖完成。
3. **取待整理版本。** 用 `app/periodic_update.py packet --limit 100 --output 新批次.json`。逐条读原文，结合完整工作、相关人员、项目及较新用户决定判断。即使工作刚分析过，也检查新证据是否影响它。
4. **先明确原目标。** 用 `app/manage.py work-goal-packet 工作身份 --output 文件`，必要时 `work-goal-save --input 文件` 保存带依据的完成条件。逐条核对已达成、未达成或未知；独立后续不阻塞已经达成的原工作，原要求也不能被旧标题缩小。
5. **实际更新业务。** 用 `work-append` 更新明确进展、结果、状态和时间。先复用已有事项，只有独立新工作才 `work-create`；仅是建议用 `work-propose`。每版证据都用 `work-evidence-review --input 文件` 留下判断，无变化也说明原因。有重要歧义时 `judgment-propose --input 文件` 建立具体可见问题。
6. **落实用户答复。** 无新原文也查看最新已处理卡片、补充说明及修订。把明确内容归入人员关系、工作、项目及下一步，保留原话。按问题身份与答复修订去重，不能只看卡片已确认。
7. **保存分析和后续承接。** `work-analysis-packet` → `work-analyze`；`follow-through-packet` → `follow-through-review`。默认两段式分析。按原目标判断，已完成事项停止原推进建议；遗留的独立行动要有对应工作或明确理由。
8. **重新核对排序与提醒。** `work-priority-packet` → `work-priority-review`；`project-priority-packet` → `project-priority-review`。有明确临近约定、关键阻塞或重大影响才提升；不知道不等于高优先级。提醒已启用时取 `app/reminders.py packet`，保存当前计划并同步云端，核对实际队列。
9. **读回后登记完成。** 读取受影响记录、问题和实际维护流水，再 `app/periodic_update.py finish --input 回执.json`。不得只写完成回执而不实际更新业务。最后 `judgment-reconcile`、`review-status`、`project-review-status`、`app/periodic_update.py review-check`。

命令的必填参数使用 `--help` 查询，包中各模块的校验逻辑及批次中的 `instructions` 是准确输入契约。输入必须引用现有原文身份和能匹配的摘录，使用最新修订号及上下文凭据；冲突时重新阅读变化，不能只替换版本强行提交。

报告至少区分：来源覆盖和缺口、原文待整理、工作待核对、业务待用户判断、提醒计划及实际同步状态。程序校验通过不证明所有语义判断一定正确；以原文和实际写回核对。
