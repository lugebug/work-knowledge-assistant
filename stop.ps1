$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$paths = (& $pythonExe -X utf8 (Join-Path $projectRoot 'app\bootstrap.py') paths) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Unable to read personal configuration.' }
$runtimeFile = Join-Path (Split-Path -Parent $paths.database) 'workbench-runtime\runtime.json'
if (-not (Test-Path -LiteralPath $runtimeFile)) { Write-Output 'No running installation recorded.'; exit 0 }
$prior = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
$serverFile = Join-Path $projectRoot 'app\server.py'
$running = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$prior.pid)
if (-not $running) { Write-Output 'Workbench is stopped.'; exit 0 }
if (-not $running.CommandLine -or -not $running.CommandLine.Contains($serverFile) -or -not $running.CommandLine.Contains([string]$paths.database)) { throw 'Recorded process is not owned by this installation; it was left running.' }
$address = [uri]$prior.url
if ($address.Scheme -ne 'http' -or $address.Host -ne '127.0.0.1') { throw 'Unexpected runtime URL.' }
$health = Invoke-RestMethod -Uri ($prior.url + '/api/health') -TimeoutSec 3
if ($health.pid -ne $prior.pid -or $health.application -ne 'work-knowledge') { throw 'Process identity could not be confirmed.' }
Stop-Process -Id $running.ProcessId -ErrorAction Stop
Write-Output 'Workbench stopped. Personal data and configured cloud reminders were preserved.'
