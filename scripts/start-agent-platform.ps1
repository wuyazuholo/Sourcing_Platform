param(
    [string]$AgentRoot = '',
    [ValidateSet('embedded','mysql')][string]$AuthStore = 'embedded',
    [int]$AgentPort = 8090,
    [int]$BackendPort = 8080,
    [int]$FrontendPort = 5173,
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
$platformRoot = Split-Path $PSScriptRoot -Parent
if (!$AgentRoot) {
    $bundledAgent = Join-Path $platformRoot 'agent'
    $AgentRoot = if (Test-Path -LiteralPath (Join-Path $bundledAgent 'pyproject.toml')) { $bundledAgent } else { Join-Path $platformRoot '..\sourcing-agent' }
}
$AgentRoot = (Resolve-Path -LiteralPath $AgentRoot).Path
$localRoot = Join-Path $platformRoot '.local'
New-Item -ItemType Directory -Force -Path $localRoot | Out-Null
$pythonPath = Join-Path $AgentRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Agent Python environment is missing. See docs/agent-integration.md.' }
$toolchainPath = Join-Path $localRoot 'toolchain.json'
$toolchain = if (Test-Path -LiteralPath $toolchainPath) { Get-Content -LiteralPath $toolchainPath -Raw | ConvertFrom-Json } else { $null }
$javaPath = if ($env:JAVA_HOME) { Join-Path $env:JAVA_HOME 'bin\java.exe' } elseif ($toolchain) { Join-Path $toolchain.jdk 'bin\java.exe' } else { (Get-Command java.exe -ErrorAction Stop).Source }
$mavenPath = if ($toolchain) { Join-Path $toolchain.maven 'bin\mvn.cmd' } else { 'mvn.cmd' }
& $pythonPath (Join-Path $PSScriptRoot 'configure-agent.py') --agent-root $AgentRoot
if ($LASTEXITCODE -ne 0) { throw 'Local service configuration failed.' }
$jar = Join-Path $platformRoot 'backend\target\sourcing-platform-0.1.0.jar'
if ($Build -or !(Test-Path -LiteralPath $jar)) {
    $env:JAVA_HOME = Split-Path (Split-Path $javaPath -Parent) -Parent
    Push-Location (Join-Path $platformRoot 'backend')
    try { & $mavenPath '-DskipTests' package; if ($LASTEXITCODE -ne 0) { throw 'Backend build failed.' } } finally { Pop-Location }
}
$vitePath = Join-Path $platformRoot 'frontend\node_modules\vite\bin\vite.js'
if (!(Test-Path -LiteralPath $vitePath)) {
    Push-Location (Join-Path $platformRoot 'frontend')
    try { & npm.cmd ci --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw 'Frontend installation failed.' } } finally { Pop-Location }
}
foreach ($port in @($AgentPort,$BackendPort,$FrontendPort)) {
    if ($port -lt 1 -or $port -gt 65535) { throw 'Ports must be between 1 and 65535.' }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "Port $port is already in use." }
}
if (@($AgentPort,$BackendPort,$FrontendPort | Select-Object -Unique).Count -ne 3) { throw 'Use three distinct ports.' }
function Quoted([string]$value) { return '"' + $value + '"' }
$processes = @()
try {
    $agentArgs = @('-B','-m','sourcing_agent','api','--env-file',(Quoted (Join-Path $AgentRoot '.env.database.local')),'--service-env-file',(Quoted (Join-Path $AgentRoot '.env.platform.local')),'--port',"$AgentPort")
    $processes += Start-Process -FilePath $pythonPath -ArgumentList $agentArgs -WorkingDirectory $AgentRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $localRoot 'agent.log') -RedirectStandardError (Join-Path $localRoot 'agent-error.log') -PassThru
    $profiles = if ($AuthStore -eq 'embedded') { 'local,embedded' } else { 'local' }
    $backendArgs = @('-Dfile.encoding=UTF-8','-jar',(Quoted $jar),"--spring.profiles.active=$profiles","--server.port=$BackendPort","--agent.base-url=http://127.0.0.1:$AgentPort")
    $processes += Start-Process -FilePath $javaPath -ArgumentList $backendArgs -WorkingDirectory (Join-Path $platformRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $localRoot 'backend.log') -RedirectStandardError (Join-Path $localRoot 'backend-error.log') -PassThru
    $env:SOURCING_API_TARGET = "http://127.0.0.1:$BackendPort"
    $processes += Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList @((Quoted $vitePath),'--host','127.0.0.1','--port',"$FrontendPort") -WorkingDirectory (Join-Path $platformRoot 'frontend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $localRoot 'frontend.log') -RedirectStandardError (Join-Path $localRoot 'frontend-error.log') -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (@($processes | Where-Object { $_.HasExited }).Count) { throw 'A service exited. Check .local/*-error.log.' }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$FrontendPort/api/auth/config" -TimeoutSec 2 -ErrorAction Stop
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (!$ready) { throw 'Startup timed out. Check .local/backend.log.' }
    $ownedProcesses = @($processes)
    # Windows venv python.exe is a launcher; record its actual interpreter child as well.
    foreach ($parentProcess in $processes) {
        if ($parentProcess.Path -eq $pythonPath) {
            foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($parentProcess.Id)")) {
                if ($child.Name -eq 'python.exe') { $ownedProcesses += Get-Process -Id $child.ProcessId }
            }
        }
    }
    $ownedProcesses | ForEach-Object { [PSCustomObject]@{ id=$_.Id; executable=$_.Path; startedTicks=$_.StartTime.ToUniversalTime().Ticks.ToString() } } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $localRoot 'agent-platform-processes.json') -Encoding utf8
    Write-Host "Ready: http://127.0.0.1:$FrontendPort/#/agent"
    Write-Host "Auth store: $AuthStore. Local SMS test code: 123456. Logs: $localRoot"
} catch {
    foreach ($process in $processes) { if (!$process.HasExited) { Stop-Process -Id $process.Id } }
    throw
}
