@echo off
setlocal

cd /d "%~dp0"

set "SCHEDULED="
if /I "%~1"=="--scheduled" set "SCHEDULED=1"
if not "%~1"=="" if /I not "%~1"=="--scheduled" (
    echo ERROR: Unsupported argument: %~1
    if not defined SCHEDULED pause
    exit /b 2
)

set "APP=%~dp0release\MoetPriceMonitor.exe"
set "CONFIG=%~dp0config.toml"

if not exist "%APP%" (
    echo ERROR: The standalone monitor was not found:
    echo   "%APP%"
    echo Build it first with: powershell -ExecutionPolicy Bypass -File build.ps1
    echo.
    if not defined SCHEDULED pause
    exit /b 2
)

if not exist "%CONFIG%" (
    echo ERROR: Configuration file was not found:
    echo   "%CONFIG%"
    echo.
    if not defined SCHEDULED pause
    exit /b 2
)

echo Starting the MOET price collection...
echo.
"%APP%" --config "%CONFIG%"
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" (
    echo Collection completed successfully.
) else (
    echo Collection failed with exit code %RESULT%.
    echo Check the log at: "%~dp0logs\monitor.log"
)

if not defined SCHEDULED (
    echo.
    pause
)
exit /b %RESULT%
