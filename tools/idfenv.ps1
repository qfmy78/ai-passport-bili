# ESP-IDF environment entry (dot-source this file)
#
# Usage:
#     . .\tools\idfenv.ps1
#     cd .\refs\ai-passport-tv\firmware
#     idf.py set-target esp32c3
#     idf.py build
#
# Three traps on this machine, all handled here:
#   1. MSYS python 3.14 hijacks `python` and breaks venv creation -> put native 3.13 first
#   2. github.com releases are unreachable -> point IDF_GITHUB_ASSETS at the China mirror
#   3. idf_tools.py refuses to run inside Git Bash (MSYS detected) -> always use PowerShell
#
# Keep this file ASCII-only: PowerShell 5.1 reads BOM-less scripts as ANSI/GBK.

$env:IDF_PATH           = 'C:\Espressif\frameworks\esp-idf-v5.5.3'
$env:IDF_GITHUB_ASSETS  = 'dl.espressif.cn/github_assets'
$env:IDF_TOOLS_PATH     = "$env:USERPROFILE\.espressif"
$env:PIP_DEFAULT_TIMEOUT = '120'
$env:PIP_RETRIES        = '10'

$nativePy = 'C:\Users\mm\.workbuddy\binaries\python\versions\3.13.12'
if (Test-Path $nativePy) {
    # Use the .NET API, never $env:PATH: assigning the upper-case name creates a
    # SECOND key next to the existing "Path", and child processes (cmake, ninja)
    # then inherit the stale one -- which looks exactly like "ninja not found".
    $procPath = [Environment]::GetEnvironmentVariable('Path', 'Process')
    if ($procPath -notlike "*$nativePy*") {
        [Environment]::SetEnvironmentVariable('Path', "$nativePy;$procPath", 'Process')
    }
}

Push-Location $env:IDF_PATH
try {
    & '.\export.ps1' | Out-Null
    $gcc = (Get-Command riscv32-esp-elf-gcc -ErrorAction SilentlyContinue)
    Write-Host "[ok] ESP-IDF ready:" (Split-Path $env:IDF_PATH -Leaf)
    if ($gcc) { Write-Host "[ok] gcc:" $gcc.Source } else { Write-Host "[!!] riscv32-esp-elf-gcc not on PATH" }
} finally {
    Pop-Location
}
