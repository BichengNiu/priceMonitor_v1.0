#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'MOET Essential Goods Price Monitor',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$TaskDescription = 'MOET_PRICE_MONITOR_V5 - Runs the root run_monitor.bat daily at 10:00; collects one snapshot per publication date.'

function Get-MoetTaskPaths {
    $root = $PSScriptRoot
    $batchPath = Join-Path $root 'run_monitor.bat'
    $executablePath = Join-Path $root 'release\MoetPriceMonitor.exe'
    $configPath = Join-Path $root 'config.toml'
    if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
        throw "The sole run entry was not found: $batchPath"
    }
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "Standalone worker not found. Run .\build.ps1 first: $executablePath"
    }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Configuration file not found: $configPath. Copy config.example.toml to config.toml first."
    }

    [pscustomobject]@{
        Batch = (Resolve-Path -LiteralPath $batchPath).Path
        WorkingDirectory = (Resolve-Path -LiteralPath $root).Path
    }
}

function Install-MoetPriceMonitorTask {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [switch]$AllowReplace
    )

    $paths = Get-MoetTaskPaths
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    $isManagedTask = (
        $null -ne $existing -and
        $null -ne $existing.Description -and
        $existing.Description -eq $TaskDescription
    )
    if ($null -ne $existing -and -not $isManagedTask -and -not $AllowReplace) {
        throw "An unrelated scheduled task named '$Name' already exists. Re-run with -Force only if you intend to replace it."
    }

    $arguments = ('/d /c ""{0}" --scheduled"' -f $paths.Batch)
    $action = New-ScheduledTaskAction `
        -Execute $env:ComSpec `
        -Argument $arguments `
        -WorkingDirectory $paths.WorkingDirectory
    $triggers = @(
        New-ScheduledTaskTrigger `
            -Daily `
            -At ([datetime]::Today.AddHours(10))
    )
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    if ($PSCmdlet.ShouldProcess($Name, 'Register daily MOET price monitor task')) {
        Register-ScheduledTask `
            -TaskName $Name `
            -Description $TaskDescription `
            -Action $action `
            -Trigger $triggers `
            -Settings $settings `
            -Force | Out-Null
        Write-Host "Scheduled task installed: $Name"
        Write-Host "Run entry: $($paths.Batch)"
    }
}

Install-MoetPriceMonitorTask -Name $TaskName -AllowReplace:$Force -WhatIf:$WhatIfPreference
