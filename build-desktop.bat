@echo off
rem ============================================================
rem  OpenMinis Desktop - build the Windows desktop app
rem
rem    build-desktop.bat            -> dist\OpenMinisDesktop\OpenMinisDesktop.exe
rem    build-desktop.bat clean      remove build\ and dist\
rem
rem  只出便携版（onedir）。onefile 那版已删掉：它每次启动都要把载荷解到 %TEMP%，
rem  而两个形态并存只会让人不知道该下哪个。
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
"%PYTHON_PATH%" -m pip install "pywebview>=5.0" "playwright>=1.63" "pyinstaller>=6.6" || goto :fail

rem --- icon ---------------------------------------------------------------
if not exist "desktop\assets\icon.ico" (
    echo [setup] generating icon
    "%PYTHON_PATH%" scripts\make_icon.py
)

rem --- build --------------------------------------------------------------
echo [build] onedir -> dist\OpenMinisDesktop\OpenMinisDesktop.exe
"%PYTHON_PATH%" -m PyInstaller --noconfirm --clean packaging\OpenMinisDesktop.spec || goto :fail

rem --- 可覆盖载荷 ---------------------------------------------------------
rem 内核与界面单独装成一个可以整体替换的目录，排在 sys.path 最前面（见
rem desktop\paths.py 的 payload_root）。已经装好的实例更新时只需要换它。
set PAYLOAD_OUT=dist\OpenMinisDesktop\payload
echo [build] assembling payload -> !PAYLOAD_OUT!
"%PYTHON_PATH%" scripts\make_payload.py --out "!PAYLOAD_OUT!" --zip dist\OpenMinisDesktop-update.zip || goto :fail

echo.
echo ============================================================
echo  Build finished.
echo  Run: dist\OpenMinisDesktop\OpenMinisDesktop.exe
echo ============================================================
if not "%OPENMINIS_NOPAUSE%"=="1" pause
exit /b 0

:fail
echo.
echo [FAILED] build did not complete - see the messages above.
if not "%OPENMINIS_NOPAUSE%"=="1" pause
exit /b 1
