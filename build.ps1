#requires -Version 5.1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$buildEnvironment = Join-Path $projectDir '.build-venv'
$buildPython = Join-Path $buildEnvironment 'Scripts\python.exe'
$workDir = Join-Path $projectDir '.build'

$outputPath = Join-Path $projectDir 'release'

if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    throw 'Python launcher (py.exe) is required only to build the application.'
}

if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
    Write-Host 'Creating isolated build environment...'
    & py.exe -3.14 -m venv $buildEnvironment
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the build environment.'
    }
}

New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

Write-Host 'Installing build dependencies...'
& $buildPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw 'Could not update pip in the build environment.'
}
& $buildPython -m pip install -r (Join-Path $projectDir 'requirements.txt') 'pyinstaller>=6.14,<7'
if ($LASTEXITCODE -ne 0) {
    throw 'Could not install application dependencies.'
}

Write-Host "Building standalone executable in $outputPath ..."
& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name MoetPriceMonitor `
    --distpath $outputPath `
    --workpath (Join-Path $workDir 'pyinstaller-work') `
    --specpath $workDir `
    (Join-Path $projectDir 'run_monitor.py')
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller failed to build the executable.'
}

$executable = Join-Path $outputPath 'MoetPriceMonitor.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Build completed without creating $executable"
}

& $executable --help | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'The built executable failed its smoke test.'
}

Write-Host ''
Write-Host 'Build complete. Run the application using the root run_monitor.bat only.'
Write-Host "Executable: $executable"
