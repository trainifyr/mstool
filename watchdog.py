import os
import sys
import time
import urllib.request
import subprocess

# Fixed Paths
PYTHON_EXE = r"C:\Users\Venkatesh\AppData\Local\Python\pythoncore-3.14-64\python.exe"
PYTHONW_EXE = r"C:\Users\Venkatesh\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
SCRIPT_PATH = r"d:\AI\tool\hello.py"
LOG_PATH = r"d:\AI\tool\watchdog_log.txt"
STARTUP_FOLDER = r"C:\Users\Venkatesh\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"
VBS_LAUNCHER_PATH = os.path.join(STARTUP_FOLDER, "start_watchdog.vbs")



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
        with urllib.request.urlopen("http://localhost:5000/", timeout=5) as response:
            return response.status == 200
    except Exception:
        return False

def kill_stale_processes():
    try:
        log_event("Terminating stale processes...")
        # Precise command to kill any python processes running hello.py (excluding the watchdog itself)
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
            f"Where-Object {{$_.CommandLine -like '*hello.py*'}} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
        )
        subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True)
        
        # Free up port 5000 if bound by any other zombie process
        port_cmd = (
            "Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | "
            "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
        )
        subprocess.run(["powershell", "-Command", port_cmd], capture_output=True)
    except Exception as e:
        log_event(f"Error while killing stale processes: {e}")

def start_hello():
    log_event("Starting hello.py...")
    try:
        # Run hello.py in background silently using pythonw.exe
        subprocess.Popen([PYTHONW_EXE, SCRIPT_PATH], cwd=os.path.dirname(SCRIPT_PATH))
        log_event("hello.py successfully launched.")
    except Exception as e:
        log_event(f"Failed to start hello.py: {e}")

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
        subprocess.Popen([PYTHONW_EXE, os.path.abspath(__file__)], cwd=os.path.dirname(os.path.abspath(__file__)))
        print("Watchdog launched in the background successfully.")
        print("Setup complete! The dashboard is now monitored and will start automatically on reboot.")
    except Exception as e:
        print(f"Error launching watchdog process: {e}")
        sys.exit(1)
    print("==================================================")

def monitor_loop():
    log_event("Watchdog monitoring loop active.")
    while True:
        try:
            if not is_healthy():
                log_event("Application is unresponsive or down. Re-initializing...")
                kill_stale_processes()
                time.sleep(2)
                start_hello()
        except Exception as e:
            log_event(f"Error in monitor loop: {e}")
        time.sleep(30)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install()
    else:
        monitor_loop()
