$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
try {
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv (Join-Path $projectRoot '.venv')
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv (Join-Path $projectRoot '.venv')
        } else {
            throw 'Install Python 3.10 or newer from python.org (include the Python launcher), then run Setup Windows.cmd again.'
        }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment. Check that Python 3.10 or newer is installed.' }
    }
    & $pythonPath -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'This environment needs Python 3.10 or newer. Ask your setup person to recreate .venv with a supported Python version.' }
    & $pythonPath -m pip install -r (Join-Path $projectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Check the connection and the messages above, then run setup again.' }
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    if (-not $desktopPath) { throw 'Windows could not locate your Desktop folder.' }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktopPath 'Member Intake.lnk'))
    $shortcut.TargetPath = Join-Path $projectRoot 'Start Member Intake.cmd'
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = "$pythonPath,0"
    $shortcut.Description = 'Open the local Member Intake browser app'
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Host 'Created the Member Intake desktop shortcut.'
    Write-Host 'The first launch opens credential setup. Existing settings and queue history are preserved.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
