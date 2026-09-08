#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$OutputDir = 'release'
)

$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$buildEnvironment = Join-Path $projectDir '.build-venv'
$buildPython = Join-Path $buildEnvironment 'Scripts\python.exe'
$workDir = Join-Path $projectDir '.build'

if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $outputPath = [System.IO.Path]::GetFullPath($OutputDir)
}
else {
    $outputPath = [System.IO.Path]::GetFullPath((Join-Path $projectDir $OutputDir))
}

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

$configDestination = Join-Path $outputPath 'config.toml'
if (-not (Test-Path -LiteralPath $configDestination -PathType Leaf)) {
    $configSource = Join-Path $projectDir 'config.toml'
    if (-not (Test-Path -LiteralPath $configSource -PathType Leaf)) {
        $configSource = Join-Path $projectDir 'config.example.toml'
    }
    Copy-Item -LiteralPath $configSource -Destination $configDestination
}

Copy-Item -LiteralPath (Join-Path $projectDir 'install_task.ps1') `
    -Destination (Join-Path $outputPath 'install_task.ps1') -Force

$executable = Join-Path $outputPath 'MoetPriceMonitor.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Build completed without creating $executable"
}

& $executable --help | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'The built executable failed its smoke test.'
}

Write-Host ''
Write-Host 'Standalone build complete.'
Write-Host "Executable: $executable"
Write-Host "Config:     $configDestination"
Write-Host 'Install the scheduled task from this directory with .\install_task.ps1.'
