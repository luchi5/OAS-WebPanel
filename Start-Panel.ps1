param(
    [switch]$Public,
    [switch]$NoBrowser,
    [string]$PythonPath,
    [string]$CaddyPath,
    [string]$SettingsPath
)
$ErrorActionPreference = 'Stop'
$panelRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $SettingsPath) { $SettingsPath = Join-Path $panelRoot 'settings.json' }
$SettingsPath = [IO.Path]::GetFullPath($SettingsPath)
if (-not (Test-Path -LiteralPath $SettingsPath)) { throw 'Create settings.json from settings.example.json and configure backend_root first.' }
if (-not $PythonPath) {
    $PythonPath = Join-Path $panelRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        $runtime = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $runtime) { throw 'Python is missing. Install Python and the locked requirements first.' }
        $PythonPath = $runtime.Source
    }
}
$PythonPath = (Get-Item -LiteralPath $PythonPath -ErrorAction Stop).FullName
$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $panelRoot
    $validated = & $PythonPath -B -c 'import json,sys; from pathlib import Path; from app import load_settings; print(json.dumps(load_settings(Path(sys.argv[1]))))' $SettingsPath
    if ($LASTEXITCODE -ne 0) { throw 'Panel settings validation failed.' }
    $settings = $validated | ConvertFrom-Json
} finally { Set-Location -LiteralPath $previousLocation.Path }
$hash = [Security.Cryptography.SHA256]::Create()
try { $rootKey = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($panelRoot.ToLowerInvariant()))).Replace('-', '').Substring(0, 16) } finally { $hash.Dispose() }
$operationMutex = [Threading.Mutex]::new($false, ('Local\OASWebPanelOperation-' + $rootKey))
$operationAcquired = $false
try {
    try { $operationAcquired = $operationMutex.WaitOne(30000) } catch [Threading.AbandonedMutexException] { $operationAcquired = $true }
    if (-not $operationAcquired) { throw 'Another panel operation is still in progress.' }
    $logs = Join-Path $panelRoot 'logs'
    $state = $settings.state_dir
    foreach ($directory in @($logs, $state)) {
        if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory | Out-Null }
        $item = Get-Item -LiteralPath $directory -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Panel runtime directories must be ordinary directories.' }
    }
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $state '/inheritance:r' '/grant:r' ('*' + $userSid + ':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not protect panel credentials directory.' }
    $appPath = Join-Path $panelRoot 'app.py'
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $settings.port -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $owners = @($listeners.OwningProcess | Select-Object -Unique)
        if ($owners.Count -ne 1) { throw 'Panel port has unexpected listener owners.' }
        $owner = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owners[0])
        if (-not $owner.CommandLine -or -not $owner.CommandLine.Contains($appPath)) { throw 'Panel port is already used by another process.' }
        [IO.File]::WriteAllText((Join-Path $state 'panel.pid'), [string]$owner.ProcessId)
        Write-Host 'Panel already running.'
    } else {
        $arguments = @('-B', ('"' + $appPath + '"'), '--settings', ('"' + $SettingsPath + '"'))
        $process = Start-Process -FilePath $PythonPath -ArgumentList $arguments -WorkingDirectory $panelRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'panel.stdout.log') -RedirectStandardError (Join-Path $logs 'panel.stderr.log') -PassThru
        $ready = $false
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            Start-Sleep -Milliseconds 250
            if ($process.HasExited) { throw 'Panel failed to start. See logs/panel.stderr.log.' }
            try {
                $null = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $settings.port + '/api/session') -TimeoutSec 2
                $ready = $true
                break
            } catch { }
        }
        if (-not $ready) { throw 'Panel readiness timed out. See logs/panel.stderr.log.' }
        $listener = Get-NetTCPConnection -State Listen -LocalPort $settings.port -ErrorAction Stop | Select-Object -First 1
        $owner = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener.OwningProcess)
        if (-not $owner.CommandLine -or -not $owner.CommandLine.Contains($appPath)) { throw 'Unexpected process owns the panel port.' }
        [IO.File]::WriteAllText((Join-Path $state 'panel.pid'), [string]$owner.ProcessId)
        Write-Host 'Panel started.'
    }
    if ($Public) {
        $publicUri = [Uri]$settings.public_origin
        if ($publicUri.Scheme -ne 'https') { throw 'Configure public_origin as your HTTPS origin before using -Public.' }
        if (-not $CaddyPath) {
            $gateway = Get-Command caddy.exe -ErrorAction SilentlyContinue
            if (-not $gateway) { throw 'Install Caddy separately, then use -CaddyPath or add it to PATH.' }
            $CaddyPath = $gateway.Source
        }
        $CaddyPath = (Get-Item -LiteralPath $CaddyPath -ErrorAction Stop).FullName
        $caddyConfig = Join-Path $panelRoot 'Caddyfile'
        if (-not (Test-Path -LiteralPath $caddyConfig)) { throw 'Copy Caddyfile.example to Caddyfile first.' }
        $pidFile = Join-Path $state 'caddy.pid'
        $existing = $null
        if (Test-Path -LiteralPath $pidFile) {
            $gatewayPid = 0
            if ([int]::TryParse((Get-Content -Raw -LiteralPath $pidFile).Trim(), [ref]$gatewayPid)) {
                $existing = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $gatewayPid)
            }
            if ($existing -and ($existing.ExecutablePath -ne $CaddyPath -or -not $existing.CommandLine.Contains($caddyConfig))) { throw 'HTTPS PID record belongs to another process.' }
        }
        if (-not $existing) {
            $oldOrigin = $env:OAS_PANEL_PUBLIC_ORIGIN
            $oldUpstream = $env:OAS_PANEL_UPSTREAM
            try {
                $env:OAS_PANEL_PUBLIC_ORIGIN = $settings.public_origin
                $env:OAS_PANEL_UPSTREAM = '127.0.0.1:' + $settings.port
                & $CaddyPath validate --config $caddyConfig --adapter caddyfile *> $null
                if ($LASTEXITCODE -ne 0) { throw 'Caddy configuration validation failed.' }
                $gatewayArguments = @('run', '--config', ('"' + $caddyConfig + '"'), '--adapter', 'caddyfile')
                $gatewayProcess = Start-Process -FilePath $CaddyPath -ArgumentList $gatewayArguments -WorkingDirectory $panelRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'https.stdout.log') -RedirectStandardError (Join-Path $logs 'https.stderr.log') -PassThru
                Start-Sleep -Milliseconds 750
                if ($gatewayProcess.HasExited) { throw 'HTTPS gateway failed to start. See logs/https.stderr.log.' }
                [IO.File]::WriteAllText($pidFile, [string]$gatewayProcess.Id)
                [IO.File]::WriteAllText((Join-Path $state 'caddy-runtime.txt'), $CaddyPath)
            } finally {
                $env:OAS_PANEL_PUBLIC_ORIGIN = $oldOrigin
                $env:OAS_PANEL_UPSTREAM = $oldUpstream
            }
        }
        Write-Host 'HTTPS gateway running with your configured origin.'
    }
    $localUrl = 'http://127.0.0.1:' + $settings.port
    if (-not $NoBrowser) { Start-Process $localUrl }
    Write-Host ('Local URL: ' + $localUrl)
} finally {
    if ($operationAcquired) { $operationMutex.ReleaseMutex() }
    $operationMutex.Dispose()
}
