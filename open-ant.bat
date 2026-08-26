@echo off
rem ────────────────────────────────────────────────────────────
rem  open-ant launcher (Windows) — zero-install entry point.
rem  Works straight from a source copy: run `open-ant init`
rem  in this directory (cmd searches the current dir first).
rem  Requires Python 3.11+ with the dependencies installed:
rem    pip install .        (once, in any environment)
rem ────────────────────────────────────────────────────────────
setlocal
set "ROOT=%~dp0"

rem ── Preflight: is the `ant` package importable? ──
python -c "import sys; sys.path.insert(0, r'%ROOT%'); import ant" 2>nul
if errorlevel 1 (
    echo.
    echo [open-ant] Dependencies are not installed in this Python environment.
    echo   Fix:  cd /d "%ROOT%"  ^&^&  pip install .
    echo.
    exit /b 1
)

set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
python -m ant.cli.main %*
exit /b %errorlevel%
