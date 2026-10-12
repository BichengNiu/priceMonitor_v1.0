from pathlib import Path


def test_installer_routes_daily_task_through_the_single_batch_entry() -> None:
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
        "run_monitor.bat",
        "--scheduled",
        "/d /c",
        "$env:ComSpec",
        "build.ps1",
        "-WorkingDirectory",
        "Register-ScheduledTask",
    )
    for fragment in required_fragments:
        assert fragment in script
    assert ".venv" not in script
    assert "-AtLogOn" not in script
    assert "foreach ($hour in 0..23)" not in script
    assert "-Execute $application.Executable" not in script
    assert "StartsWith($ManagedTaskDescriptionPrefix)" not in script
    assert "$existing.Description -eq $TaskDescription" in script
