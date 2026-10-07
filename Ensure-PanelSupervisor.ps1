param([string]$PythonPath, [string]$SettingsPath)
$ErrorActionPreference = 'Stop'
$panelRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $PythonPath) { $PythonPath = Join-Path $panelRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Create the Python environment first, or specify -PythonPath.' }
if (-not $SettingsPath) { $SettingsPath = Join-Path $panelRoot 'settings.json' }
$supervisor = Join-Path $panelRoot 'panel_supervisor.py'
Start-Process -FilePath $PythonPath -ArgumentList @('-B', ('"' + $supervisor + '"'), '--settings', ('"' + $SettingsPath + '"')) -WorkingDirectory $panelRoot -WindowStyle Hidden
