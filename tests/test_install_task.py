from pathlib import Path


def test_installer_uses_daily_ten_am_catchup_standalone_command() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "install_task.ps1").read_text(encoding="utf-8")

    required_fragments = (
        "SupportsShouldProcess",
        "Get-ScheduledTask",
        "New-ScheduledTaskAction",
        "New-ScheduledTaskTrigger",
        "-Daily",
        "AddHours(10)",
        "-StartWhenAvailable",
        "New-ScheduledTaskSettingsSet",
        "-MultipleInstances IgnoreNew",
        "-AllowStartIfOnBatteries",
        "-DontStopIfGoingOnBatteries",
        "MoetPriceMonitor.exe",
        "Resolve-MoetApplication",
        "build.ps1",
        "-WorkingDirectory",
        "Register-ScheduledTask",
    )
    for fragment in required_fragments:
        assert fragment in script
    assert ".venv" not in script
    assert "-AtLogOn" not in script
    assert "foreach ($hour in 0..23)" not in script
