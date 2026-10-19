# 增量整理机制（V44）

每轮判断上轮之后发生了什么，再更新真正受影响的工作和项目。已确认内容持续有效；日期过去、再次保存同一判断、项目一般说明或回执时间改变，都不要求全量重算。新的业务事实、完成条件、人工决定和来源授权变化仍会触发相应检查。

采集保存原文，Codex 判断业务，程序验证写回与回执。这三步分别记录。新修订必须逐版判断，但未改变事实的修订不自动推翻已确认的分析或优先级。相关提醒会等待修订判断完成；真实到点提醒、完成取消和用户暂停改时仍正常执行。

## 操作入口

以下命令均使用项目虚拟环境，文件放在个人数据目录。`prepare` 每批最多 200 版，保留完整新增原文和精简导航索引；`pending` 只列真正待处理的对象身份。

```powershell
.venv\Scripts\python.exe -X utf8 app\incremental.py prepare --output '本轮差异.json'
.venv\Scripts\python.exe -X utf8 app\incremental.py pending
.venv\Scripts\python.exe -X utf8 app\periodic_update.py review-check --summary --output '本轮核对.json'
.venv\Scripts\python.exe -X utf8 app\incremental.py settle 'prepare返回的run_id'
.venv\Scripts\python.exe -X utf8 app\incremental.py checkpoint '本轮完成回执.json'
```

`settle` 仅读取，返回 `snapshot_token`、`changed_entities` 和 `changed_decisions`。完成回执包含 `run_id`、原样返回的 `snapshot_token`、`acknowledgments` 数组。每项确认包含 `kind`、`id`、`reason`，准确覆盖全部变化主体和答复，不多报、不漏报、不重复；答复的 `kind` 为 `decisions`。理由至少 8 字，应说明实际落实结果，不用占位文字。

无变化时 `acknowledgments` 为空。原文 `finish` 回执与本轮检查点各有用途，不能互相代替。并发修改、遗漏答复、未整理新版本或尚未完成的业务检查会阻止检查点前移；完全相同的完成回执重复提交不会重复处理。

只有新证据实际改变优先级依据时，才提交 `priority-impact 文件.json`。输入包含 `kind`（`tasks` 或 `projects`）、`id`、具体 `reason` 和原文版本身份数组 `sources`；随后只复核该对象。登记影响不直接修改排序，人工设定始终优先。不要对本轮所有资料或所有工作批量登记影响。

## 升级行为

新安装默认开启；旧版重跑安装入口时先核对配置与数据库属于同一安装，再建立 SQLite 一致备份，最后迁移检查记录。备份位置由安装输出给出。已确认且仍有效的判断继续沿用，原来确有未落实内容的记录继续待处理；人员、工作、目标、人工决定、业务修订、审计和提醒节点身份均保留。已开启时重复安装不再迁移或重复备份。

详见 [更新指南](update.md)。Codex 既有定期任务的旧正文也需更新，不能只升级程序却保留“每次全量复核”的任务指令。
