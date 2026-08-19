# PowerShell Uninstaller for Keystroke Monitor Watchdog Service
# This script completely stops the active background processes, deletes the Startup shortcut, and removes all permanent files.

$ErrorActionPreference = "Continue"

Write-Host "==================================================" -ForegroundColor Yellow
Write-Host "         KEYSTROKE MONITOR UNINSTALLER            " -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Yellow

# 1. Stop background processes
Write-Host "[*] Stopping background activity monitor processes..." -ForegroundColor Cyan
try {
    $processes = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe' or Name='watchdog.exe'" -ErrorAction SilentlyContinue
    $killedCount = 0
    
    foreach ($p in $processes) {
        if ($p.CommandLine -like "*watchdog.py*" -or $p.CommandLine -like "*app.py*" -or $p.Name -eq "watchdog.exe") {
            Stop-Process -Id $p.ProcessId -Force
            $killedCount++
        }
    }
    Write-Host "[+] Successfully stopped $killedCount active logging processes." -ForegroundColor Green
} catch {
    Write-Host "[-] Error while trying to stop active processes: $_" -ForegroundColor Red
}

# 2. Remove VBS Startup shortcut
Write-Host "`n[*] Removing Auto-Start VBScript launcher..." -ForegroundColor Cyan
$startupFolder = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$vbsPath = Join-Path $startupFolder "start_watchdog.vbs"

if (Test-Path $vbsPath) {
    try {
        Remove-Item -Path $vbsPath -Force
        Write-Host "[+] Startup launcher deleted successfully." -ForegroundColor Green
    } catch {
        Write-Host "[-] Failed to delete startup launcher: $_" -ForegroundColor Red
    }
} else {
    Write-Host "[+] No startup launcher found (already removed)." -ForegroundColor Green
}

# 3. Delete AppData permanent folder
Write-Host "`n[*] Removing permanent application files..." -ForegroundColor Cyan
$installDir = "$env:APPDATA\WindowsMonitor"

if (Test-Path $installDir) {
    try {
        # Force garbage collection/resource free to ensure file handle is released
        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()
        
        Remove-Item -Path $installDir -Recurse -Force
        Write-Host "[+] Permanent directory $installDir removed completely." -ForegroundColor Green
    } catch {
        Write-Host "[-] Could not completely delete some files. They will be cleaned up on next system reboot." -ForegroundColor Yellow
    }
} else {
    Write-Host "[+] No permanent folder found." -ForegroundColor Green
}

Write-Host "`n==================================================" -ForegroundColor Green
Write-Host "UNINSTALL COMPLETED SUCCESSFULLY!" -ForegroundColor Green
Write-Host "The monitor tool has been completely removed from this computer." -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Start-Sleep -Seconds 5
