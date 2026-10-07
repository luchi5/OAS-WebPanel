param([switch]$OnlyIfOasStopped, [string]$SettingsPath)
$ErrorActionPreference = 'Stop'
$panelRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $SettingsPath) { $SettingsPath = Join-Path $panelRoot 'settings.json' }
$settings = Get-Content -Raw -LiteralPath $SettingsPath -Encoding UTF8 | ConvertFrom-Json
$state = if ($settings.state_dir) { $settings.state_dir } else { 'state' }
if (-not [IO.Path]::IsPathRooted($state)) { $state = Join-Path $panelRoot $state }
$appPath = Join-Path $panelRoot 'app.py'
$hash = [Security.Cryptography.SHA256]::Create()
try { $rootKey = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($panelRoot.ToLowerInvariant()))).Replace('-', '').Substring(0, 16) } finally { $hash.Dispose() }
$operationMutex = [Threading.Mutex]::new($false, ('Local\OASWebPanelOperation-' + $rootKey))
$operationAcquired = $false
try {
    try { $operationAcquired = $operationMutex.WaitOne(30000) } catch [Threading.AbandonedMutexException] { $operationAcquired = $true }
    if (-not $operationAcquired) { throw 'Another panel operation is still in progress.' }
    if ($OnlyIfOasStopped) {
        $backendRoot = [IO.Path]::GetFullPath($settings.backend_root)
        $backendUri = if ($settings.backend_url) { [Uri]$settings.backend_url } else { [Uri]'http://127.0.0.1:22289' }
        $runtimePaths = if ($settings.backend_python) { @($settings.backend_python) } else { @((Join-Path $backendRoot 'toolkit\python.exe'), (Join-Path $backendRoot 'toolkit\pythonw.exe')) }
        $backendListeners = @(Get-NetTCPConnection -State Listen -LocalPort $backendUri.Port -ErrorAction SilentlyContinue)
        foreach ($backendListener in $backendListeners) {
            $backendOwner = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $backendListener.OwningProcess)
            if ($backendOwner -and $runtimePaths -contains $backendOwner.ExecutablePath -and $backendOwner.CommandLine -match '(?i)(?:^|[\s"])(?:[^\s"]*[\\/])?server\.py(?:$|[\s"])') {
                Write-Host 'OAS backend is running; panel shutdown cancelled.'
                return
            }
        }
    }
    foreach ($name in @('caddy', 'panel')) {
        $pidFile = Join-Path $state ($name + '.pid')
        if (-not (Test-Path -LiteralPath $pidFile)) { continue }
        $targetPid = 0
        if (-not [int]::TryParse((Get-Content -Raw -LiteralPath $pidFile).Trim(), [ref]$targetPid)) { throw 'Invalid PID record.' }
        $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $targetPid)
        if (-not $process) { continue }
        if ($name -eq 'caddy') {
            $runtimeFile = Join-Path $state 'caddy-runtime.txt'
            if (-not (Test-Path -LiteralPath $runtimeFile)) { throw 'HTTPS runtime identity record is missing.' }
            $runtimePath = (Get-Content -Raw -LiteralPath $runtimeFile).Trim()
            $matches = $process.ExecutablePath -eq $runtimePath -and $process.CommandLine -and $process.CommandLine.Contains((Join-Path $panelRoot 'Caddyfile'))
        } else { $matches = $process.CommandLine -and $process.CommandLine.Contains($appPath) }
        if (-not $matches) { throw 'PID record now belongs to another process.' }
        Stop-Process -Id $targetPid -ErrorAction Stop
        Remove-Item -LiteralPath $pidFile
        Write-Host ('Stopped panel component: ' + $name)
    }
} finally {
    if ($operationAcquired) { $operationMutex.ReleaseMutex() }
    $operationMutex.Dispose()
}
