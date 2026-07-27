@echo off
cd /d "%~dp0"
echo ============================================
echo  WorkBuddy MCP 桌面桥接器 — EXE 打包
echo ============================================
echo.

REM ========== 目标 ==========
set "OUTDIR=%~dp0dist"
set "EXENAME=WorkBuddy_MCP"

REM ========== 查找 pandoc.exe ==========
set "PANDOC_PATH="
if exist "C:\tools\pandoc\pandoc.exe" set "PANDOC_PATH=C:\tools\pandoc\pandoc.exe"
if exist "%~dp0pandoc\pandoc.exe" set "PANDOC_PATH=%~dp0pandoc\pandoc.exe"
REM 从 pypandoc 目录查找
for /d %%d in ("C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python3*\Lib\site-packages\pypandoc\files\pandoc") do (
    if exist "%%d\pandoc.exe" set "PANDOC_PATH=%%d\pandoc.exe"
)

echo [1/3] 清理旧输出...
if exist "%OUTDIR%" rd /s /q "%OUTDIR%"
if exist "build" rd /s /q "build"
if exist "*.spec" del /q "*.spec"

echo [2/3] 打包（单文件，无控制台）...

REM 构建 --add-binary 参数
set "ADD_BINARY="
if not "%PANDOC_PATH%"=="" (
    echo   pandoc 路径: %PANDOC_PATH%
    set "ADD_BINARY=--add-binary=%PANDOC_PATH%;."
)

pyinstaller --onefile ^
    --noconsole ^
    --name "%EXENAME%" ^
    --hidden-import mcp ^
    --hidden-import mcp.server.fastmcp ^
    --hidden-import websockets ^
    --hidden-import asyncio ^
    --hidden-import pypandoc ^
    --collect-all mcp ^
    %ADD_BINARY% ^
    mcp_gui.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo [3/3] 复制 pandoc.exe 到 dist...
    if not "%PANDOC_PATH%"=="" (
        if exist "%PANDOC_PATH%" (
            copy /Y "%PANDOC_PATH%" "%OUTDIR%\pandoc.exe" >nul 2>&1
            echo   pandoc.exe 已复制到 dist\
        )
    )
    echo.
    echo ============================================
    echo  打包成功！
    echo  输出: %OUTDIR%\%EXENAME%.exe
    echo ============================================
) else (
    echo.
    echo ============================================
    echo  打包失败，请查看上方错误信息。
    echo ============================================
)

pause
