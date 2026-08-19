import os
import sys
import time
import urllib.request
import subprocess

# Dynamic Path Resolution
IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    import shutil
    # When frozen, sys.executable is watchdog.exe. We must locate python/pythonw on the system.
    PYTHON_EXE = shutil.which("python") or "python"
    PYTHONW_EXE = shutil.which("pythonw") or "pythonw"
    
    # Check common user install paths if not in PATH
    if not os.path.exists(PYTHONW_EXE) and not shutil.which("pythonw"):
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            python_dir = os.path.join(local_appdata, "Programs", "Python")
            if os.path.exists(python_dir):
                for root, dirs, files in os.walk(python_dir):
                    if "pythonw.exe" in files:
                        PYTHONW_EXE = os.path.join(root, "pythonw.exe")
                        PYTHON_EXE = os.path.join(root, "python.exe")
                        break
else:
    PYTHON_EXE = sys.executable
    PYTHONW_EXE = sys.executable.lower().replace("python.exe", "pythonw.exe")

if IS_FROZEN:
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(CURRENT_DIR, "app.py")
LOG_PATH = os.path.join(CURRENT_DIR, "watchdog_log.txt")
STARTUP_FOLDER = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
VBS_LAUNCHER_PATH = os.path.join(STARTUP_FOLDER, "start_watchdog.vbs")

# Windows-specific flags to prevent console window popups
SUBPROCESS_FLAGS = {}
if os.name == 'nt':
    SUBPROCESS_FLAGS['creationflags'] = 0x08000000


def log_event(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"
    try:
        if sys.stdout is not None:
            print(log_line.strip())
    except Exception:
        pass
    try:
        # Rotate log if it exceeds 1MB
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".bak")
        with open(LOG_PATH, "a") as f:
            f.write(log_line)
    except Exception:
        pass

def is_healthy():
    try:
        # Check if the Flask server port 5000 is open and responding
        with urllib.request.urlopen("http://127.0.0.1:5000/", timeout=5) as response:
            return response.status == 200
    except Exception:
        return False

def kill_stale_processes():
    try:
        log_event("Terminating stale processes...")
        # Precise command to kill any python processes running app.py (excluding the watchdog itself)
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
            f"Where-Object {{$_.CommandLine -like '*app.py*'}} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
        )
        subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, **SUBPROCESS_FLAGS)
        
        # Free up port 5000 if bound by any other zombie process
        port_cmd = (
            "Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | "
            "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
        )
        subprocess.run(["powershell", "-Command", port_cmd], capture_output=True, **SUBPROCESS_FLAGS)
    except Exception as e:
        log_event(f"Error while killing stale processes: {e}")

def start_app():
    log_event("Starting app.py...")
    try:
        # Run app.py in background silently using pythonw.exe
        subprocess.Popen([PYTHONW_EXE, SCRIPT_PATH], cwd=os.path.dirname(SCRIPT_PATH), **SUBPROCESS_FLAGS)
        log_event("app.py successfully launched.")
    except Exception as e:
        log_event(f"Failed to start app.py: {e}")

def install():
    print("==================================================")
    print("Installing Silent Startup & Watchdog Service...")
    print("==================================================")
    
    # 1. Install pip dependencies
    print("\n[1/3] Ensuring dependencies (flask, pynput, pillow) are installed...")
    try:
        subprocess.run([PYTHON_EXE, "-m", "pip", "install", "flask", "pynput", "pillow", "supabase", "python-dotenv"], check=True)
        print("Dependencies verified successfully.")
    except Exception as e:
        print(f"Warning: Dependency installation returned: {e}")

    # 2. Create the VBScript launcher in the user's Startup directory
    print("\n[2/3] Registering silent startup VBScript in Startup folder...")
    vbs_content = (
        f'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """{PYTHONW_EXE}"" ""{os.path.abspath(__file__)}""", 0, False\n'
    )
    try:
        os.makedirs(STARTUP_FOLDER, exist_ok=True)
        with open(VBS_LAUNCHER_PATH, "w") as f:
            f.write(vbs_content)
        print(f"Created silent launcher: {VBS_LAUNCHER_PATH}")
    except Exception as e:
        print(f"Error creating startup VBScript: {e}")
        sys.exit(1)

    # 3. Spawn the watchdog in the background using pythonw.exe
    print("\n[3/3] Launching background watchdog...")
    try:
        subprocess.Popen([PYTHONW_EXE, os.path.abspath(__file__)], cwd=os.path.dirname(os.path.abspath(__file__)), **SUBPROCESS_FLAGS)
        print("Watchdog launched in the background successfully.")
        print("Setup complete! The dashboard is now monitored and will start automatically on reboot.")
    except Exception as e:
        print(f"Error launching watchdog process: {e}")
        sys.exit(1)
    print("==================================================")

# Keep mutex reference alive for the lifetime of the process
sys_mutex = None

def check_single_instance():
    global sys_mutex
    if os.name == 'nt':
        try:
            import ctypes
            # Local namespace is appropriate for user sessions
            mutex_name = "Local\\KeystrokeMonitorWatchdogMutex"
            sys_mutex = ctypes.windll.kernel32.CreateMutexW(None, True, mutex_name)
            last_error = ctypes.windll.kernel32.GetLastError()
            # 183 = ERROR_ALREADY_EXISTS, 5 = ERROR_ACCESS_DENIED
            if last_error == 183 or last_error == 5 or not sys_mutex:
                print("Another watchdog instance is already running. Exiting.")
                sys.exit(0)
        except Exception as e:
            log_event(f"Single instance check failed: {e}")

def monitor_loop():
    log_event("Watchdog monitoring loop active.")
    
    # 1. Startup Delay
    log_event("Startup delay of 300 seconds active to let the system settle. Waiting...")
    time.sleep(300)
    
    consecutive_failures = 0
    while True:
        try:
            if is_healthy():
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log_event(f"Application is unresponsive or down (Consecutive failures: {consecutive_failures}). Re-initializing...")
                
                # 2. Backoff Cooldown after 3 failures
                if consecutive_failures >= 3:
                    log_event("Multiple consecutive failures detected. Entering 5-minute cooldown to prevent system resource exhaustion...")
                    time.sleep(300)
                    consecutive_failures = 0  # Reset counter after cooldown
                    continue
                
                kill_stale_processes()
                time.sleep(2)
                start_app()
        except Exception as e:
            log_event(f"Error in monitor loop: {e}")
        time.sleep(60)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install()
    else:
        check_single_instance()
        monitor_loop()
