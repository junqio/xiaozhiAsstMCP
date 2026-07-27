# Setup base PATH
$sysPath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:PATH = "D:\Espressif\python_env\idf5.5_py3.10_env\Scripts;$sysPath;$userPath"
$env:IDF_PATH = "D:\Espressif\frameworks\esp-idf-v5.5.4"
$env:IDF_PYTHON_ENV_PATH = "D:\Espressif\python_env\idf5.5_py3.10_env"
$env:NO_PROXY = "components-file.espressif.com,*.espressif.com"
$env:no_proxy = "components-file.espressif.com,*.espressif.com"

# Activate ESP-IDF (merge stderr into stdout as string, not error records)
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$activateOutput = & { python "$env:IDF_PATH\tools\activate.py" --export } 2>&1 | ForEach-Object { "$_" }
$ErrorActionPreference = $prevEAP
$scriptPath = ($activateOutput -split "`n" | Select-Object -Last 1).Trim()
. $scriptPath

$ErrorActionPreference = "Stop"

Write-Host "ESP-IDF environment ready."

# Navigate to project
Set-Location "E:\000WEI~1\XIAOZH~1.4\XIAOZH~1.4"

# Step 1: set-target
Write-Host "`n============================================"
Write-Host "[1/4] Setting target to esp32s3..."
Write-Host "============================================"
idf.py set-target esp32s3
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: set-target failed!"
    exit $LASTEXITCODE
}

# Step 2: configure board
Write-Host "`n============================================"
Write-Host "[2/4] Configuring board..."
Write-Host "============================================"
Add-Content -Path sdkconfig -Value "CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_AUDIO_BOARD=y"
Add-Content -Path sdkconfig -Value "CONFIG_AUDIO_BOARD_LCD_JD9853=y"

# Step 3: build
Write-Host "`n============================================"
Write-Host "[3/4] Building (may take 10-30 min)..."
Write-Host "============================================"
idf.py build
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: build failed!"
    exit $LASTEXITCODE
}

# Step 4: flash to COM3
Write-Host "`n============================================"
Write-Host "[4/4] Flashing to COM3..."
Write-Host "============================================"
idf.py -p COM3 -b 460800 flash
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: flash failed!"
    exit $LASTEXITCODE
}

Write-Host "`n[SUCCESS] Build and flash completed!"
