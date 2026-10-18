param(
    [string]$Name = '我',
    [string]$Since = '',
    [string]$DataDirectory = '',
    [switch]$CoreOnly,
    [switch]$SkipStart
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('Command failed with exit code ' + $LASTEXITCODE) }
}

function Find-Python {
    $candidates = @()
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($version in @('311','312','313','314')) {
        $candidates += Join-Path $env:LOCALAPPDATA ('Programs\Python\Python' + $version + '\python.exe')
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-Path -LiteralPath $candidate) {
            try {
                & $candidate -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,14) and sys.maxsize > 2**32 else 1)' 2>$null
                if ($LASTEXITCODE -eq 0) { return $candidate }
            } catch { }
        }
    }
    return $null
}

$pythonExe = Find-Python
if (-not $pythonExe) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'Python 3.11+ (64-bit) is required. Install Python from python.org, then rerun this installer.' }
    Write-Host 'Installing Python for this Windows user...'
    Invoke-Checked $winget.Source @('install','--id','Python.Python.3.11','--exact','--scope','user','--silent','--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
    $pythonExe = Find-Python
    if (-not $pythonExe) { throw 'Python was not found after installation. Reopen Codex/terminal and retry.' }
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-Checked $pythonExe @('-m','venv',(Join-Path $projectRoot '.venv'))
}
Invoke-Checked $venvPython @('-X','utf8','scripts\verify_release.py')

if (-not $CoreOnly) {
    Write-Host 'Installing pinned read-only connector dependencies...'
    Invoke-Checked $venvPython @('-X','utf8','-m','pip','install','--disable-pip-version-check','--only-binary=:all:','-r','requirements-connectors.txt')
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        $knownNode = Join-Path $env:ProgramFiles 'nodejs\node.exe'
        if (Test-Path -LiteralPath $knownNode) { $nodeCommand = Get-Item -LiteralPath $knownNode }
    }
    if (-not $nodeCommand) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'Node.js is required for Feishu. Install the current Node.js LTS and rerun.' }
        Invoke-Checked $winget.Source @('install','--id','OpenJS.NodeJS.LTS','--exact','--silent','--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
        $knownNode = Join-Path $env:ProgramFiles 'nodejs\node.exe'
        if (-not (Test-Path -LiteralPath $knownNode)) { throw 'Node.js setup needs to finish. Reopen the terminal and retry.' }
        $nodeCommand = Get-Item -LiteralPath $knownNode
    }
    $nodeExe = if ($nodeCommand.Source) { $nodeCommand.Source } else { $nodeCommand.FullName }
    & $nodeExe -e 'const [major,minor]=process.versions.node.split(/\./).map(Number); process.exit(major>20 || (major===20 && minor>=12) ? 0 : 1)'
    if ($LASTEXITCODE -ne 0) { throw 'This Feishu CLI needs Node.js 20.12 or newer. Update to a current Node.js LTS and rerun.' }
    $npmScript = Join-Path (Split-Path -Parent $nodeExe) 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $npmScript)) { throw 'npm was not found beside Node.js. Repair Node.js and retry.' }
    # Dependencies stay inside this installation; existing global CLIs are not replaced.
    Invoke-Checked $nodeExe @($npmScript,'ci','--prefix',(Join-Path $projectRoot '.tools'),'--omit=dev','--no-audit','--no-fund')
}

$initArgs = @('-X','utf8','app\bootstrap.py','init','--name',$Name)
if ($Since) { $initArgs += @('--since',$Since) }
if ($DataDirectory) { $initArgs += @('--data-dir',$DataDirectory) }
Invoke-Checked $venvPython $initArgs
if (-not $CoreOnly) {
    Invoke-Checked $venvPython @('-X','utf8','app\bootstrap.py','configure-tools','--node',$nodeExe,'--lark-script',(Join-Path $projectRoot '.tools\node_modules\@larksuite\cli\scripts\run.js'))
}
Invoke-Checked $venvPython @('-X','utf8','app\bootstrap.py','doctor')
if (-not $SkipStart) { & (Join-Path $projectRoot 'start.ps1') }
Write-Host 'Installation completed. Ask Codex to continue docs/first-run.md for your own account connections.'
