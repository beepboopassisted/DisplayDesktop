@echo off
rem Runs DisplayDesktop from this folder with the Python on PATH (py launcher if "python" does not run:
rem not found, or only Windows' Store alias, which runs no Python).
rem Usage: run [start, stop, restart, status, recovery, enable-startup or disable-startup]   (default: start)
setlocal
rem (an ERRORLEVEL variable in the environment would hide the real exit codes from %errorlevel%)
set "ERRORLEVEL="
set "ARGS=%*"
if "%~1"=="" set "ARGS=start"
rem (call for the probe only: a "python" that is a batch file, e.g. a version manager's shim, returns
rem here; the run itself is the last step, without call: a path with ^ or % stays as it is; the probe
rem asks for the version the app needs (3.10+ answers 0; no less-than, greater-than, semicolon or equals
rem sign in it: a shim passing it on unquoted stays safe): an older Python falls back to py too; any
rem exit code but 0, negative crash codes too, falls back; the block only picks the Python: the arguments
rem are expanded outside it, where a closing parenthesis in them ends nothing)
call python -I -S -c "__import__('sys').exit(not __import__('sys').hexversion // 0x030A0000)" >nul 2>nul
if not "%errorlevel%"=="0" (
    set "PY=py -3"
) else (
    set "PY=python"
)
%PY% -I -S -B "%~dp0DisplayDesktop.pyw" %ARGS%
exit /b %errorlevel%
