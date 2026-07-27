@echo off
set "PATH=D:\Espressif\python_env\idf5.5_py3.11_env\Scripts;%PATH%"
for %%p in ("C:\Program Files\Git\cmd" "C:\Program Files (x86)\Git\cmd") do (
    if exist %%p set "PATH=%%~p;%PATH%"
)

set NO_PROXY=components-file.espressif.com,*.espressif.com
set no_proxy=components-file.espressif.com,*.espressif.com

pushd D:\Espressif\frameworks\esp-idf-v5.5.4
call export.bat
popd

REM Force add IDF tools to PATH as fallback
set "PATH=%IDF_PATH%\tools;%PATH%"

cd /d E:\000WEI~1\XIAOZH~1.4\XIAOZH~1.4

echo ============================================
echo [1/4] set-target esp32s3
echo ============================================
python "%IDF_PATH%\tools\idf.py" set-target esp32s3
if %ERRORLEVEL% NEQ 0 exit /b 1

echo.
echo ============================================
echo [2/4] Configure board
echo ============================================
echo CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_AUDIO_BOARD=y>>sdkconfig
echo CONFIG_AUDIO_BOARD_LCD_JD9853=y>>sdkconfig

echo.
echo ============================================
echo [3/4] Building (may take 10-30 min)
echo ============================================
python "%IDF_PATH%\tools\idf.py" build
if %ERRORLEVEL% NEQ 0 exit /b 1

echo.
echo ============================================
echo [4/4] Flashing to COM3
echo ============================================
python "%IDF_PATH%\tools\idf.py" -p COM3 -b 460800 flash
echo EXIT_CODE=%ERRORLEVEL%
