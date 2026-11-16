$ErrorActionPreference = 'Stop'
$file = Join-Path (Split-Path $PSScriptRoot -Parent) '.local\agent-platform-processes.json'
if (!(Test-Path -LiteralPath $file)) { Write-Host 'No recorded local services.'; return }
foreach ($entry in @((Get-Content -LiteralPath $file -Raw | ConvertFrom-Json) | Sort-Object startedTicks -Descending)) {
    $process = Get-Process -Id $entry.id -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $entry.executable -and $process.StartTime.ToUniversalTime().Ticks -eq [long]$entry.startedTicks) {
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        Wait-Process -Id $entry.id -Timeout 10 -ErrorAction SilentlyContinue
    }
}
Remove-Item -LiteralPath $file
Write-Host 'Recorded local services stopped.'
