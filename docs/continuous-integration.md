# Windows 自动测试模板

本发行版已完成本机检查。当前发布凭据没有 GitHub 工作流管理权限，因此仓库尚未启用 GitHub Actions；本文件仅为维护者准备模板，不影响安装或用户自己的 Codex 定期任务。

维护者具备工作流管理权限后，可将以下内容保存为 `.github/workflows/portable.yml`，重新生成发行清单并提交，再以实际运行结果判断是否通过。

```yaml
name: Windows portable installation
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  clean-install:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: '3.11'
      - run: python -m pip install --only-binary=:all: -r requirements-connectors.txt
      - run: python -X utf8 scripts/verify_release.py
      - run: python -X utf8 -m unittest discover -s tests -v
      - name: Fresh personal profile
        shell: pwsh
        run: |
          $env:WORK_KNOWLEDGE_CONFIG = Join-Path $env:RUNNER_TEMP 'personal-profile/config.local.json'
          ./install.ps1 -CoreOnly -SkipStart -Name 'CI demo' -Since '2020-01-01' -DataDirectory (Join-Path $env:RUNNER_TEMP 'personal-data')
```

如需同时覆盖完整飞书 CLI 安装，可另加联网的全量安装场景。运行环境没有真实账号，不在持续集成中进行实际微信／飞书采集。
