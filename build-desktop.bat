@echo off
rem ============================================================
rem  OpenMinis Desktop - build the Windows desktop app
rem
rem    build-desktop.bat            onedir  -> dist\OpenMinisDesktop\OpenMinisDesktop.exe
rem    build-desktop.bat onefile    onefile -> dist\OpenMinisDesktop.exe
rem    build-desktop.bat clean      remove build\ and dist\
rem
rem  Run it from the repo root, on Windows, with Python 3.11+ available.
rem  Everything it needs is installed into a local .venv.
rem
rem  Don't want to install a toolchain? Push a tag and let
rem  .github/workflows/build-windows.yml produce the exe instead.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
title OpenMinis Desktop Build

set PYTHON_PATH=%~dp0.venv\Scripts\python.exe

if /i "%~1"=="clean" (
    echo [clean] removing build\ and dist\
    if exist build rmdir /s /q build
    if exist dist rmdir /s /q dist
    echo done.
    if not "%OPENMINIS_NOPAUSE%"=="1" pause
    exit /b 0
)

rem --- locate an interpreter ------------------------------------------------
if not exist "%PYTHON_PATH%" (
    where py >nul 2>&1
    if !errorlevel! equ 0 (
        echo [setup] creating .venv with py launcher
        py -3 -m venv .venv || goto :fail
    ) else (
        where python >nul 2>&1
        if !errorlevel! neq 0 (
            echo [ERROR] Python 3.11+ not found on PATH.
            echo         Install it from https://www.python.org/downloads/windows/
            goto :fail
        )
        echo [setup] creating .venv with python
        python -m venv .venv || goto :fail
    )
)

rem --- dependencies ---------------------------------------------------------
echo [setup] installing dependencies
"%PYTHON_PATH%" -m pip install --upgrade pip || goto :fail
"%PYTHON_PATH%" -m pip install -e . || goto :fail
rem pywebview brings WebView2 (EdgeChromium) support; PyInstaller does the packaging.
"%PYTHON_PATH%" -m pip install "pywebview>=5.0" "pyinstaller>=6.6" || goto :fail

rem --- icon ---------------------------------------------------------------
if not exist "desktop\assets\icon.ico" (
    echo [setup] generating icon
    "%PYTHON_PATH%" scripts\make_icon.py
)

rem --- build --------------------------------------------------------------
if /i "%~1"=="onefile" (
    set OPENMINIS_ONEFILE=1
    echo [build] onefile -> dist\OpenMinisDesktop.exe
) else (
    set OPENMINIS_ONEFILE=0
    echo [build] onedir  -> dist\OpenMinisDesktop\OpenMinisDesktop.exe
)

"%PYTHON_PATH%" -m PyInstaller --noconfirm --clean packaging\OpenMinisDesktop.spec || goto :fail

echo.
echo ============================================================
echo  Build finished.
if /i "%~1"=="onefile" (
    echo  Run: dist\OpenMinisDesktop.exe
) else (
    echo  Run: dist\OpenMinisDesktop\OpenMinisDesktop.exe
)
echo ============================================================
if not "%OPENMINIS_NOPAUSE%"=="1" pause
exit /b 0

:fail
echo.
echo [FAILED] build did not complete - see the messages above.
if not "%OPENMINIS_NOPAUSE%"=="1" pause
exit /b 1
