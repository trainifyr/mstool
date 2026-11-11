#Requires -Version 5.1
<#
.SYNOPSIS
    MSTool Client Auto-Setup Script
    Installs and configures the monitoring agent on a Windows client machine.

.DESCRIPTION
    One-command setup. Run as Administrator for best results.

    Usage:
        iex (irm 'https://<LAMBDA_URL>/api/client/update/setup_client.ps1')
    Or locally:
        powershell -ExecutionPolicy Bypass -File setup_client.ps1

.NOTES
    - Downloads app.py + watchdog.py from the central Lambda server
    - Installs Python if not found
    - Installs pip dependencies (flask, pynput, pillow, boto3, mss)
    - Creates .env with RENDER_SERVER_URL pointing to Lambda
    - Registers watchdog as a silent startup item
    - Zero AWS credentials stored on client - all traffic via HTTPS to Lambda
#>

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
$LAMBDA_URL   = "https://lnslbk3kmal2h2cdlfrvzpwgmu0qzetl.lambda-url.ap-south-1.on.aws"
$INSTALL_DIR  = "$env:LOCALAPPDATA\MSTool"
$PYTHON_MIN   = "3.9"
# ──────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

function Write-Step { param($n, $msg) Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg)     Write-Host "    ✓ $msg" -ForegroundColor Green }
function Write-Warn { param($msg)     Write-Host "    ⚠ $msg" -ForegroundColor Yellow }
function Write-Err  { param($msg)     Write-Host "    ✗ $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "   MSTool Client Auto-Setup" -ForegroundColor Magenta
Write-Host "   Central Server: $LAMBDA_URL" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Magenta

# ── STEP 1: Create install directory ──────────────────────────────────────────
Write-Step 1 "Creating install directory: $INSTALL_DIR"
New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null
Write-Ok "Directory ready"

# ── STEP 2: Check / install Python ────────────────────────────────────────────
Write-Step 2 "Checking for Python $PYTHON_MIN+"
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+\.\d+)") {
            $found = [version]$Matches[1]
            if ($found -ge [version]$PYTHON_MIN) {
                $python = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
                if (-not $python) { $python = $cmd }
                Write-Ok "Found: $ver  ($python)"
                break
            }
        }
    } catch {}
}

if (-not $python) {
    Write-Warn "Python $PYTHON_MIN+ not found. Downloading installer..."
    $pyInstaller = "$env:TEMP\python_installer.exe"
    # Python 3.12 stable Windows x64 embeddable zip would be smaller, but
    # the full installer handles pip + PATH automatically.
    $pyUrl = "https://www.python.org/ftp/python/3.12.3/python-3.12.3-amd64.exe"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller -UseBasicParsing
        Write-Ok "Installer downloaded"
        Write-Host "    Installing Python silently (this may take ~2 minutes)..." -ForegroundColor Gray
        Start-Process -Wait -FilePath $pyInstaller -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1"
        Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
        # Refresh PATH in current session
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" +
                    [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
        $python = (Get-Command python -ErrorAction SilentlyContinue).Source
        if (-not $python) { $python = "python" }
        Write-Ok "Python installed: $($python)"
    } catch {
        Write-Err "Failed to install Python automatically."
        Write-Host "    Please install Python manually from https://python.org and re-run this script." -ForegroundColor Red
        exit 1
    }
}

# Determine pythonw path (windowless)
$pythonw = $python -replace "python\.exe$", "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# ── STEP 3: Download app.py + watchdog.py from Lambda ─────────────────────────
Write-Step 3 "Downloading monitoring agent from Lambda server..."
$headers = @{ "Cache-Control" = "no-cache" }
try {
    $appCode = Invoke-WebRequest -Uri "$LAMBDA_URL/api/client/update/app.py" -UseBasicParsing -Headers $headers
    Set-Content -Path "$INSTALL_DIR\app.py" -Value $appCode.Content -Encoding UTF8
    Write-Ok "app.py downloaded ($(($appCode.Content).Length) bytes)"
} catch {
    Write-Err "Failed to download app.py: $_"
    exit 1
}
try {
    $wdCode = Invoke-WebRequest -Uri "$LAMBDA_URL/api/client/update/watchdog.py" -UseBasicParsing -Headers $headers
    Set-Content -Path "$INSTALL_DIR\watchdog.py" -Value $wdCode.Content -Encoding UTF8
    Write-Ok "watchdog.py downloaded"
} catch {
    Write-Err "Failed to download watchdog.py: $_"
    exit 1
}

# ── STEP 4: Write .env configuration ──────────────────────────────────────────
Write-Step 4 "Writing .env configuration"
$envContent = @"
# MSTool Client Configuration
# Auto-generated by setup_client.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

# Central AWS Lambda server URL (no credentials needed - all via HTTPS)
RENDER_SERVER_URL=$LAMBDA_URL

# How often to poll for config & remote commands (seconds)
CONFIG_POLL_INTERVAL=30
"@
Set-Content -Path "$INSTALL_DIR\.env" -Value $envContent -Encoding UTF8
Write-Ok ".env written to $INSTALL_DIR\.env"

# ── STEP 5: Install pip dependencies ──────────────────────────────────────────
Write-Step 5 "Installing Python dependencies..."
$deps = @("flask", "pynput", "pillow", "boto3", "python-dotenv", "mss")
try {
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet @deps
    Write-Ok "Dependencies installed: $($deps -join ', ')"
} catch {
    Write-Warn "pip install warning (may be non-fatal): $_"
}

# ── STEP 6: Register silent startup via VBScript ──────────────────────────────
Write-Step 6 "Registering silent startup..."
$startupFolder = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")
$vbsPath = Join-Path $startupFolder "start_mstool_watchdog.vbs"
$vbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$pythonw"" ""$INSTALL_DIR\watchdog.py""", 0, False
"@
try {
    New-Item -ItemType Directory -Force -Path $startupFolder | Out-Null
    Set-Content -Path $vbsPath -Value $vbsContent -Encoding ASCII
    Write-Ok "Startup VBScript created: $vbsPath"
} catch {
    Write-Warn "Could not create startup VBScript: $_"
}

# ── STEP 7: Launch watchdog now ────────────────────────────────────────────────
Write-Step 7 "Launching monitoring agent (background)..."
try {
    $proc = Start-Process -FilePath $pythonw -ArgumentList "`"$INSTALL_DIR\watchdog.py`"" `
        -WorkingDirectory $INSTALL_DIR -WindowStyle Hidden -PassThru
    Write-Ok "Watchdog started (PID: $($proc.Id))"
} catch {
    Write-Warn "Could not start watchdog silently, trying python instead..."
    try {
        Start-Process -FilePath $python -ArgumentList "`"$INSTALL_DIR\watchdog.py`"" `
            -WorkingDirectory $INSTALL_DIR -WindowStyle Hidden
        Write-Ok "Watchdog started"
    } catch {
        Write-Err "Failed to start watchdog: $_"
    }
}

# ── DONE ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "   ✓ MSTool Client Setup Complete!" -ForegroundColor Green
Write-Host ""
Write-Host "   Install dir : $INSTALL_DIR" -ForegroundColor Gray
Write-Host "   Server      : $LAMBDA_URL" -ForegroundColor Gray
Write-Host "   Auto-start  : On every login (via Startup folder)" -ForegroundColor Gray
Write-Host "   Updates     : Automatic (watchdog checks daily)" -ForegroundColor Gray
Write-Host "   Remote PS   : Enabled (dashboard can run commands)" -ForegroundColor Gray
Write-Host ""
Write-Host "   The agent will appear in the dashboard within ~1 minute." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host ""

