@echo off
setlocal
cd /d "%~dp0"

set VENV=.venv

if exist "%VENV%\Scripts\python.exe" (
    "%VENV%\Scripts\python.exe" -c "import webview" >nul 2>&1
    if errorlevel 1 (
        echo Existing .venv doesn't have this project's dependencies importable, rebuilding it.
        rmdir /s /q "%VENV%"
    )
)

where uv >nul 2>&1
if %errorlevel%==0 (
    if not exist "%VENV%" (
        echo Setting up a virtual environment with uv ^(first run only^)...
        uv venv "%VENV%"
    )
    uv pip install -q -r requirements.txt --python "%VENV%\Scripts\python.exe"
    if errorlevel 1 goto :error
) else (
    if not exist "%VENV%" (
        echo Setting up a virtual environment ^(first run only^)...
        rem The official python.org installer always registers the "py"
        rem launcher on PATH, a separate, on-by-default installer
        rem component from "Add python.exe to PATH", which people skip
        rem often enough that "python" alone isn't a safe assumption on
        rem Windows. Prefer it, and only fall back to a bare "python" for
        rem installs that put that on PATH directly instead, the
        rem Microsoft Store package, some other package managers.
        where py >nul 2>&1
        if not errorlevel 1 (
            py -3 -m venv "%VENV%"
        ) else (
            where python >nul 2>&1
            if errorlevel 1 (
                echo Couldn't find "py" or "python" on PATH. Install Python from
                echo https://www.python.org/downloads/ and make sure "Install launcher
                echo for all users" is ticked, then run this again.
                goto :error
            )
            python -m venv "%VENV%"
        )
        if errorlevel 1 goto :error
    )
    "%VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
    "%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 goto :error
)

"%VENV%\Scripts\python.exe" loader\app.py
goto :eof

:error
echo.
echo Setup failed, see the messages above.
pause
exit /b 1
