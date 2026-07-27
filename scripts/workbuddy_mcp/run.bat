@echo off
REM 确保 System32 和 Python 在 PATH 中（双击启动时环境可能不完整）
set "PATH=C:\Windows\System32;C:\Windows\System32\wbem;%LOCALAPPDATA%\Programs\Python\Python310;%LOCALAPPDATA%\Programs\Python\Python310\Scripts;%PATH%"
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   WorkBuddy MCP 桌面桥接器
echo ========================================
echo.

echo [1/2] 检查 Python 环境...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)
python --version

echo.
echo [2/2] 安装依赖...
pip install -r requirements.txt -q 2>nul
if %errorlevel% neq 0 (
    echo [警告] 部分依赖安装失败，尝试继续...
)

echo.
echo 启动 GUI ...
echo.
echo 💡 提示：如需单文件 EXE，运行 build_exe.bat 打包后，
echo     直接双击 dist\WorkBuddy_MCP.exe 即可（不需要 Python）。
echo.
python mcp_gui.py

pause
