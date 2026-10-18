# 依赖和格式参考

本项目使用独立安装的第三方依赖，不把维护者的账号、令牌、数据库或运行环境打进发行包。

- 飞书 CLI：[`larksuite/cli`](https://github.com/larksuite/cli)，固定版本见 `.tools/package-lock.json`，MIT。通过 npm 获取官方分发，许可证保留在安装依赖中。
- APSW：[`rogerbinns/apsw`](https://github.com/rogerbinns/apsw)，Python SQLite 包装器，其许可证随依赖分发。
- python-zstandard：[`indygreg/python-zstandard`](https://github.com/indygreg/python-zstandard)，许可证随依赖分发。
- cryptography：[`pyca/cryptography`](https://github.com/pyca/cryptography)，许可证随依赖分发。
- 微信内存中的 WCDB 配置结构参考：[`fanyuantaier/wechatauto-replica`](https://github.com/fanyuantaier/wechatauto-replica)，提交 `62503d76a0db069171cb4e42e87090ad285db489`，Apache-2.0。许可证全文随本包保存在 `licenses/wechat-layout-Apache-2.0.txt`。本项目的适配代码已改为校验本人配置的程序和版本、限时只读、密钥仅在内存保留，不包含上游消息发送、界面注入、媒体下载或整库导出功能。

整个项目目前未选定开源许可证；GitHub 仓库用于获授权的试用分发，不因公开可见而自动改变许可。第三方部分继续遵循各自许可证。
