param([int]$Port = 0, [switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run install.ps1 first.' }
$bootstrap = Join-Path $projectRoot 'app\bootstrap.py'
$pathsJson = & $pythonExe -X utf8 $bootstrap paths
if ($LASTEXITCODE -ne 0) { throw 'Unable to read personal configuration.' }
$paths = $pathsJson | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $paths.config) -or -not (Test-Path -LiteralPath $paths.database)) { throw 'Run install.ps1 first.' }
$database = [string]$paths.database
$serverFile = Join-Path $projectRoot 'app\server.py'
$runDir = Join-Path (Split-Path -Parent $database) 'workbench-runtime'
New-Item -ItemType Directory -Path $runDir -ErrorAction SilentlyContinue | Out-Null
$runtimeFile = Join-Path $runDir 'runtime.json'
if (Test-Path -LiteralPath $runtimeFile) {
    $prior = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $priorUri = $null
    $validPrior = [uri]::TryCreate([string]$prior.url, [UriKind]::Absolute, [ref]$priorUri) -and $priorUri.Scheme -eq 'http' -and $priorUri.Host -eq '127.0.0.1'
    if ($validPrior -and $prior.database -eq $database) {
        if ($Port -eq 0) { $Port = $priorUri.Port }
        $owned = $false
        try {
            $health = Invoke-RestMethod -Uri ($prior.url + '/api/health') -TimeoutSec 2
            $running = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$prior.pid)
            $owned = $running -and $running.CommandLine -and $running.CommandLine.Contains($serverFile) -and $running.CommandLine.Contains($database) -and $health.pid -eq $prior.pid -and $health.application -eq 'work-knowledge'
        } catch { }
        if ($owned) {
            $sourceFiles = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'app') -File -Recurse) + @(Get-Item -LiteralPath $paths.config)
            $changes = @($sourceFiles | Where-Object { $_.Extension -in @('.py','.js','.css','.html','.json') -and $_.LastWriteTimeUtc -gt $running.CreationDate.ToUniversalTime() })
            if ($changes.Count -eq 0) {
                if ($OpenBrowser) { Start-Process $prior.url }
                Write-Output ($prior | ConvertTo-Json -Compress)
                exit 0
            }
            Stop-Process -Id $running.ProcessId -ErrorAction Stop
        }
    }
}
$arguments = @('-X','utf8',('"' + $serverFile + '"'),'--db',('"' + $database + '"'),'--port',[string]$Port,'--runtime',('"' + $runtimeFile + '"'))
$process = Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runDir 'server.log') -RedirectStandardError (Join-Path $runDir 'server-error.log')
for ($attempt = 0; $attempt -lt 50; $attempt++) {
    Start-Sleep -Milliseconds 200
    if ($process.HasExited) { throw 'Workbench failed to start. Inspect workbench-runtime/server-error.log in your personal data directory.' }
    if (Test-Path -LiteralPath $runtimeFile) {
        try {
            $info = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
            # Windows venv Python may be a redirector whose child owns the server.
            $serverProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$info.pid)
            $launchedHere = $serverProcess -and ($serverProcess.ProcessId -eq $process.Id -or $serverProcess.ParentProcessId -eq $process.Id)
            $sameCommand = $serverProcess.CommandLine -and $serverProcess.CommandLine.Contains($serverFile) -and $serverProcess.CommandLine.Contains($database)
            if (-not $launchedHere -or -not $sameCommand) { continue }
            $health = Invoke-RestMethod -Uri ($info.url + '/api/health') -TimeoutSec 2
            if ($health.ok -and $health.pid -eq $info.pid -and $health.application -eq 'work-knowledge') {
                if ($OpenBrowser) { Start-Process $info.url }
                Write-Output ($info | ConvertTo-Json -Compress)
                exit 0
            }
        } catch { }
    }
}
throw 'Workbench readiness was not confirmed; inspect the personal runtime logs.'
