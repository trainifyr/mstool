# PowerShell Installer for Keystroke Monitor Watchdog Service
# This script installs Python (if missing), installs pip dependencies, sets up credentials, and installs the silent background watchdog service.

$ErrorActionPreference = "Stop"

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "         KEYSTROKE MONITOR INSTALLER              " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Resolve Python Path
$pythonPath = "python"
$pythonCheck = Get-Command python -ErrorAction SilentlyContinue

if (-not $pythonCheck) {
    Write-Host "[-] Python is not installed in the system PATH." -ForegroundColor Yellow
    
    # Check standard install directory to see if it was installed previously
    $localPythonDir = "$env:LOCALAPPDATA\Programs\Python"
    $foundPython = Get-ChildItem -Path $localPythonDir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    
    if ($foundPython) {
        $pythonPath = $foundPython.FullName
        Write-Host "[+] Found Python installed locally at: $pythonPath" -ForegroundColor Green
    } else {
        Write-Host "[*] Downloading Python 3.11 Installer..." -ForegroundColor Cyan
        $installerPath = "$env:TEMP\python-3.11.9-amd64.exe"
        
        try {
            # Use TLS 1.2
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $installerPath
        } catch {
            Write-Host "[-] Failed to download Python. Please check your internet connection." -ForegroundColor Red
            Read-Host "Press Enter to exit..."
            Exit 1
        }
        
        Write-Host "[*] Running Python installation silently... Please wait (takes about 1-2 minutes)." -ForegroundColor Cyan
        # Install quietly for current user, prepend to PATH
        $installProcess = Start-Process -FilePath $installerPath -ArgumentList "/quiet PrependPath=1" -Wait -PassThru
        
        if ($installProcess.ExitCode -ne 0) {
            Write-Host "[-] Python installation failed with exit code $($installProcess.ExitCode)." -ForegroundColor Red
            Read-Host "Press Enter to exit..."
            Exit 1
        }
        
        Write-Host "[+] Python installed successfully!" -ForegroundColor Green
        
        # Clean up installer
        if (Test-Path $installerPath) { Remove-Item $installerPath }
        
        # Try resolving the path again
        $foundPython = Get-ChildItem -Path $localPythonDir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($foundPython) {
            $pythonPath = $foundPython.FullName
        } else {
            Write-Host "[-] Python path could not be resolved automatically. Please restart the installer." -ForegroundColor Red
            Read-Host "Press Enter to exit..."
            Exit 1
        }
    }
} else {
    Write-Host "[+] Python is already installed." -ForegroundColor Green
}

# 2. Verify and install PIP dependencies
Write-Host "`n[*] Installing required dependencies (supabase, python-dotenv, pillow, pynput)..." -ForegroundColor Cyan
try {
    # Ensure pip is up to date and install requirements
    & $pythonPath -m pip install --upgrade pip --quiet
    & $pythonPath -m pip install supabase python-dotenv pillow pynput --quiet
    Write-Host "[+] Dependencies verified and installed successfully." -ForegroundColor Green
} catch {
    Write-Host "[-] Failed to install pip dependencies: $_" -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    Exit 1
}

# 3. Setup Permanent Installation Directory
Write-Host "`n[*] Setting up permanent hidden directory..." -ForegroundColor Cyan
$installDir = "$env:APPDATA\WindowsMonitor"
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

# 4. Configure credentials (.env)
Write-Host "`n[*] Checking credentials configuration..." -ForegroundColor Cyan
$localEnvPath = Join-Path $PSScriptRoot ".env"
$permEnvPath = Join-Path $installDir ".env"

# If local .env exists, copy it to permanent directory
if (Test-Path $localEnvPath) {
    Copy-Item -Path $localEnvPath -Destination $permEnvPath -Force | Out-Null
}

if (-not (Test-Path $permEnvPath)) {
    Write-Host "[*] .env file not found. Let's configure your Supabase connection." -ForegroundColor Yellow
    $url = Read-Host "Enter your Supabase URL (e.g., https://xxxxx.supabase.co)"
    $key = Read-Host "Enter your Supabase service_role secret key"
    
    if (-not $url -or -not $key) {
        Write-Host "[-] Invalid credentials provided. Setup cancelled." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        Exit 1
    }
    
    $envContent = @(
        "# Supabase Credentials Configuration",
        "SUPABASE_URL=$($url.Trim())",
        "SUPABASE_KEY=$($key.Trim())",
        "SUPABASE_BUCKET=screenshots",
        "SERVER_ONLY=false"
    )
    $envContent | Out-File -FilePath $permEnvPath -Encoding utf8
    Write-Host "[+] Credentials saved to permanent directory." -ForegroundColor Green
} else {
    Write-Host "[+] Credentials verified in permanent directory." -ForegroundColor Green
}

# 5. Copy core application files to permanent directory
Write-Host "`n[*] Copying application files to permanent directory..." -ForegroundColor Cyan
try {
    Copy-Item -Path (Join-Path $PSScriptRoot "app.py") -Destination (Join-Path $installDir "app.py") -Force
    Copy-Item -Path (Join-Path $PSScriptRoot "watchdog.py") -Destination (Join-Path $installDir "watchdog.py") -Force
    Write-Host "[+] Application files copied to $installDir" -ForegroundColor Green
} catch {
    Write-Host "[-] Failed to copy files to permanent directory: $_" -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    Exit 1
}

# 6. Install Watchdog service from the Permanent Directory
Write-Host "`n[*] Installing background silent Startup Watchdog..." -ForegroundColor Cyan
try {
    # watchdog.py install will create startup VBScript launcher and spawn pythonw.exe in background
    & $pythonPath (Join-Path $installDir "watchdog.py") --install
    Write-Host "[+] Setup complete! The activity monitor is now running in the background from permanent storage and will auto-start with Windows." -ForegroundColor Green
} catch {
    Write-Host "[-] Failed to install watchdog startup service: $_" -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    Exit 1
}

Write-Host "`n==================================================" -ForegroundColor Green
Write-Host "INSTALLATION COMPLETED SUCCESSFULLY!" -ForegroundColor Green
Write-Host "You can close this window now." -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Start-Sleep -Seconds 5
