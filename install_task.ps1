#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'MOET Essential Goods Price Monitor',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ManagedTaskDescriptionPrefix = 'MOET_PRICE_MONITOR_'
$TaskDescription = 'MOET_PRICE_MONITOR_V4 - Standalone executable; runs daily at 10:00 and catches up when available; collects one snapshot per publication date.'

function Resolve-MoetApplication {
    [CmdletBinding()]
    param()

    $root = $PSScriptRoot
    $rootExecutable = Join-Path $root 'MoetPriceMonitor.exe'
    $releaseDirectory = Join-Path $root 'release'
    $releaseExecutable = Join-Path $releaseDirectory 'MoetPriceMonitor.exe'

    if (Test-Path -LiteralPath $rootExecutable -PathType Leaf) {
        $executablePath = $rootExecutable
        $configPath = Join-Path $root 'config.toml'
    }
    elseif (Test-Path -LiteralPath $releaseExecutable -PathType Leaf) {
        $executablePath = $releaseExecutable
        $configPath = Join-Path $root 'config.toml'
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            $configPath = Join-Path $releaseDirectory 'config.toml'
        }
    }
    else {
        throw "Standalone executable not found. Run .\build.ps1 first. Expected: $rootExecutable or $releaseExecutable"
    }

    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Configuration file not found: $configPath. Copy config.example.toml to config.toml first."
    }

    [pscustomobject]@{
        Executable = (Resolve-Path -LiteralPath $executablePath).Path
        Config = (Resolve-Path -LiteralPath $configPath).Path
        WorkingDirectory = Split-Path -Parent (Resolve-Path -LiteralPath $configPath).Path
    }
}

function Install-MoetPriceMonitorTask {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [switch]$AllowReplace
    )

    $application = Resolve-MoetApplication
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    $isManagedTask = (
        $null -ne $existing -and
        $null -ne $existing.Description -and
        $existing.Description.StartsWith($ManagedTaskDescriptionPrefix)
    )
    if ($null -ne $existing -and -not $isManagedTask -and -not $AllowReplace) {
        throw "An unrelated scheduled task named '$Name' already exists. Re-run with -Force only if you intend to replace it."
    }

    $arguments = ('--config "{0}"' -f $application.Config)
    $action = New-ScheduledTaskAction `
        -Execute $application.Executable `
        -Argument $arguments `
        -WorkingDirectory $application.WorkingDirectory
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
        Write-Host "Executable: $($application.Executable)"
    }
}

Install-MoetPriceMonitorTask -Name $TaskName -AllowReplace:$Force -WhatIf:$WhatIfPreference
