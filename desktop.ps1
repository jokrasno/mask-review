param([int]$Port = 8791, [string]$Data, [string]$Private, [string]$Python, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$desktopPython = $null
$desktopCandidates = @()
if ($Python) { $desktopCandidates += $Python }
$desktopCandidates += (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
$desktopExistingPython = Get-Command python -ErrorAction SilentlyContinue
if ($desktopExistingPython -and $desktopExistingPython.Source -notlike '*\WindowsApps\*') { $desktopCandidates += $desktopExistingPython.Source }
foreach ($desktopCandidate in $desktopCandidates) {
    if (Test-Path -LiteralPath $desktopCandidate) {
        try {
            & $desktopCandidate -c 'import sys; assert sys.version_info >= (3, 10); from PIL import Image' 2>$null
            if ($LASTEXITCODE -eq 0) { $desktopPython = $desktopCandidate; break }
        } catch { continue }
    }
}
if (-not $desktopPython) {
    throw 'An existing Python 3 with Pillow is required. Nothing was installed. See DESKTOP_SETUP.md for an optional isolated local .venv setup, or pass -Python PATH.'
}
$desktopArgs = @((Join-Path $PSScriptRoot 'desktop_server.py'), '--port', $Port)
if ($Data) { $desktopArgs += @('--data', [System.IO.Path]::GetFullPath($Data)) }
if ($Private) { $desktopArgs += @('--private', [System.IO.Path]::GetFullPath($Private)) }
if (-not $NoBrowser) { $desktopArgs += '--open' }
& $desktopPython @desktopArgs
if ($LASTEXITCODE -ne 0) { throw 'The local editor could not start. Check the port, separate cache and DESKTOP_SETUP.md.' }
