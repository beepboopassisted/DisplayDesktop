@echo off
rem Runs DisplayDesktop from this folder with the Python on PATH (py launcher if "python" does not run:
rem not found, or only Windows' Store alias, which runs no Python).
rem Usage: run [start, stop, restart, status, recovery, enable-startup or disable-startup]   (default: start)
setlocal
set "ARGS=%*"
if "%~1"=="" set "ARGS=start"
rem (call for the probe only: a "python" that is a batch file, e.g. a version manager's shim, returns
rem here; the run itself is the last step, without call: a path with ^ or % stays as it is)
call python -c "" >nul 2>nul
if errorlevel 1 (
    py -3 -B "%~dp0DisplayDesktop.pyw" %ARGS%
) else (
    python -B "%~dp0DisplayDesktop.pyw" %ARGS%
)
exit /b %errorlevel%
