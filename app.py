# ========================================================================================
# === 1. IMPORTS & SETUP =================================================================
# ========================================================================================
import websocket
import json
import requests
import threading
import time
import os
import re
import logging
import shlex
import sys
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for, request, session, flash, jsonify
from supabase import create_client, Client
from collections import deque

load_dotenv()

# ========================================================================================
# === 2. LOGGING SETUP ===================================================================
# ========================================================================================
def setup_logging():
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # Prevent duplicate handlers
    if logger.hasHandlers():
        logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(log_formatter)
    logger.addHandler(handler)
    logging.info("Logging system initialized (Server mode).")

# ========================================================================================
# === 3. CONFIGURATION & STATE ===========================================================
# ========================================================================================
class Config:
    BOT_USERNAME = os.getenv("BOT_USERNAME", "ArcadeBot")
    BOT_PASSWORD = os.getenv("BOT_PASSWORD")
    ROOMS_TO_JOIN = os.getenv("ROOMS_TO_JOIN", "life")
    PANEL_USERNAME = os.getenv("PANEL_USERNAME", "admin")
    PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "password")
    UPTIME_SECRET_KEY = os.getenv("UPTIME_SECRET_KEY", "change-this-secret-key")
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "a-very-secret-flask-key")
    MASTERS_LIST = os.getenv("MASTERS_LIST", "yasin")
    LOGIN_URL = "https://api.howdies.app/api/login"
    WS_URL = "wss://app.howdies.app/"
    ROOM_JOIN_DELAY_SECONDS = 2
    REJOIN_ON_KICK_DELAY_SECONDS = 3
    INITIAL_RECONNECT_DELAY = 10
    MAX_RECONNECT_DELAY = 300
    QUIZ_ANSWER_DELAY_MIN_MS = int(os.getenv("QUIZ_ANSWER_DELAY_MIN_MS", "900"))
    QUIZ_ANSWER_DELAY_MAX_MS = int(os.getenv("QUIZ_ANSWER_DELAY_MAX_MS", "2500"))
    
    CYCLE_WORK_MIN_SECONDS, CYCLE_WORK_MAX_SECONDS = 900, 1800
    CYCLE_BREAK_MIN_SECONDS, CYCLE_BREAK_MAX_SECONDS = 20, 120
    CYCLE_STOP_COMMANDS = ['.stop', '.q 0', '.pause']
    CYCLE_START_COMMANDS = ['.start', '.q 1', '.play']

    ROAMER_INTERVAL_MIN_SECONDS = 9 * 60
    ROAMER_INTERVAL_MAX_SECONDS = 11 * 60
    ROAMER_PAUSE_SECONDS = 4
    ROAMER_LISTEN_SECONDS = 7
    ROAMER_VISITED_EXPIRY_SECONDS = 24 * 60 * 60
    SPIN_COMMAND = ".s"
    PRIZE_KEYWORDS = ['won', 'gets', 'prize', 'congratulations', 'unlocked', 'received']
    MASTER_PM_TARGET = MASTERS_LIST.split(',')[0].strip().lower()

    BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36", "Origin": "https://howdies.app"}
    
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    LOG_CLEANUP_INTERVAL_SECONDS = 60 * 60
    
    # NEW: Max activities to store per room
    MAX_ROOM_ACTIVITIES = 50

supabase: Client = None
if Config.SUPABASE_URL and Config.SUPABASE_KEY:
    try:
        supabase = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
        logging.info("✅ Supabase client initialized.")
    except Exception as e:
        logging.error(f"🔴 Failed to initialize Supabase client: {e}")
else:
    logging.warning("⚠️ Supabase credentials not found. Roam logs and visited state will be in-memory only.")

class BotState:
    def __init__(self):
        self.bot_user_id, self.token, self.ws_instance = None, None, None
        self.is_connected = False
        self.masters, self.room_id_to_name, self.room_name_to_id = [], {}, {}
        self.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
        self.quiz_solvers, self.processed_question_ids = {}, {}
        self.stop_bot_event = threading.Event()
        self.cycle_timers, self.break_end_times, self.work_end_times = {}, {}, {}
        
        self.roamer_thread, self.is_roamer_active = None, False
        self.stop_roamer_event = threading.Event()
        self.roamable_rooms = set()
        self.visited_roam_rooms = {} 
        self.roam_lock = threading.Lock()
        self.listening_for_prize_in_room, self.last_prize_won, self.prize_found_event = None, None, None
        self.master_user_id = None
        self.log_cleanup_thread = None

        # --- NEW: Activity Logging State ---
        self.startup_rooms = {name.strip().lower() for name in Config.ROOMS_TO_JOIN.split(',')}
        self.room_activities = {room_name: deque(maxlen=Config.MAX_ROOM_ACTIVITIES) for room_name in self.startup_rooms}
        self.activity_lock = threading.Lock()
        self.bot_start_time = None

bot_state = BotState()
bot_thread = None

# --- NEW: Helper function to add activities ---
def add_activity(room_name, activity_data):
    room_name_lower = room_name.lower()
    if room_name_lower in bot_state.room_activities:
        with bot_state.activity_lock:
            activity_data['timestamp'] = datetime.now(timezone.utc).isoformat()
            bot_state.room_activities[room_name_lower].append(activity_data)

# ========================================================================================
# === DATABASE & BACKGROUND TASKS ========================================================
# ========================================================================================
def load_visited_rooms_from_db():
    if not supabase: return
    with bot_state.roam_lock:
        try:
            logging.info("[DB] Loading visited rooms from Supabase...")
            response = supabase.table('visited_rooms').select("room_name, visited_at").execute()
            if response.data:
                now = time.time()
                for item in response.data:
                    room_name = item['room_name']
                    visited_at_dt = datetime.fromisoformat(item['visited_at'])
                    visited_at_ts = visited_at_dt.timestamp()

                    if now - visited_at_ts > Config.ROAMER_VISITED_EXPIRY_SECONDS:
                        supabase.table('visited_rooms').delete().eq('room_name', room_name).execute()
                    else:
                        bot_state.visited_roam_rooms[room_name] = visited_at_ts
                logging.info(f"[DB] Loaded {len(bot_state.visited_roam_rooms)} non-expired rooms into memory.")
        except Exception as e:
            logging.error(f"[DB] Error loading visited rooms: {e}")

def cleanup_old_logs():
    logging.info("[DB] Log cleanup thread started.")
    while not bot_state.stop_bot_event.is_set():
        try:
            if supabase:
                expire_time = datetime.now(timezone.utc) - timedelta(seconds=Config.ROAMER_VISITED_EXPIRY_SECONDS)
                supabase.table('roam_logs').delete().lt('roam_time', expire_time.isoformat()).execute()
            bot_state.stop_bot_event.wait(Config.LOG_CLEANUP_INTERVAL_SECONDS)
        except Exception as e:
            logging.error(f"[DB] Error in log cleanup thread: {e}", exc_info=True)
            bot_state.stop_bot_event.wait(60)
    logging.info("[DB] Log cleanup thread stopped.")

# ========================================================================================
# === SPIN ROAMER 2.0 LOGIC (Unchanged) ==================================================
# ========================================================================================
def extract_prize(text, bot_username):
    text_lower = text.lower()
    bot_username_lower = bot_username.lower()
    if bot_username_lower in text_lower and any(keyword in text_lower for keyword in Config.PRIZE_KEYWORDS):
        try:
            prize_part = re.split(re.escape(bot_username), text, flags=re.IGNORECASE)[1]
            prize_part = prize_part.replace('!', '').replace('You won', '').replace('You get', '').strip()
            return prize_part if len(prize_part) < 50 else "a special prize"
        except IndexError:
            return "an unknown prize"
    return None

def perform_roam_action(target_room):
    try:
        logging.info(f"[Roamer] Starting roam action for: '{target_room}'")
        join_room(target_room, source="roamer")
        
        roam_room_id = None
        for _ in range(10): # Wait up to 10 seconds for room ID
            roam_room_id = bot_state.room_name_to_id.get(target_room.lower())
            if roam_room_id: break
            time.sleep(1)
        
        if not roam_room_id:
            logging.error(f"[Roamer Action] Failed to get room ID for '{target_room}'. Aborting this roam.")
            return

        bot_state.listening_for_prize_in_room = roam_room_id
        bot_state.last_prize_won, bot_state.prize_found_event = None, threading.Event()
        
        reply_to_room(roam_room_id, Config.SPIN_COMMAND)
        got_prize = bot_state.prize_found_event.wait(timeout=Config.ROAMER_LISTEN_SECONDS)
        prize_won = bot_state.last_prize_won if got_prize else "nothing"

        bot_state.listening_for_prize_in_room = None
        bot_state.prize_found_event = None

        time.sleep(Config.ROAMER_PAUSE_SECONDS)
        leave_room(roam_room_id)
        
        current_time_ts = time.time()
        current_time_iso = datetime.fromtimestamp(current_time_ts, tz=timezone.utc).isoformat()
        
        with bot_state.roam_lock:
            bot_state.visited_roam_rooms[target_room] = current_time_ts
            if supabase:
                supabase.table('visited_rooms').upsert({'room_name': target_room, 'visited_at': current_time_iso}).execute()
                supabase.table('roam_logs').insert({'room_name': target_room, 'prize_won': prize_won, 'roam_time': current_time_iso}).execute()
        
        logging.info(f"[Roamer Action] Roam to '{target_room}' complete. Prize: {prize_won}.")

    except Exception as e:
        logging.error(f"[Roamer Action] CRITICAL ERROR during roam to '{target_room}': {e}", exc_info=True)

def roamer_logic():
    logging.info("[Roamer] Spin Roamer 2.0 thread started.")
    while not bot_state.stop_roamer_event.is_set():
        try:
            interval = random.randint(Config.ROAMER_INTERVAL_MIN_SECONDS, Config.ROAMER_INTERVAL_MAX_SECONDS)
            logging.info(f"[Roamer] Next roam scheduled in {interval/60:.1f} minutes.")
            bot_state.stop_roamer_event.wait(interval)
            if bot_state.stop_roamer_event.is_set(): break

            target_room = None
            with bot_state.roam_lock:
                now = time.time()
                expired_rooms = [r for r, ts in bot_state.visited_roam_rooms.items() if now - ts > Config.ROAMER_VISITED_EXPIRY_SECONDS]
                for room_name in expired_rooms:
                    del bot_state.visited_roam_rooms[room_name]
                    if supabase:
                        supabase.table('visited_rooms').delete().eq('room_name', room_name).execute()
                
                startup_rooms = {name.strip().lower() for name in Config.ROOMS_TO_JOIN.split(',')}
                available = list(bot_state.roamable_rooms - set(bot_state.visited_roam_rooms.keys()) - startup_rooms)
                if not available:
                    logging.warning("[Roamer] No new rooms to roam. Waiting for next cycle.")
                    continue
                target_room = random.choice(available)

            if target_room:
                perform_roam_action(target_room)

        except Exception as e:
            logging.error(f"[Roamer] Error in main roamer loop: {e}", exc_info=True)
            time.sleep(60)
    logging.info("[Roamer] Spin Roamer thread stopped.")

# ========================================================================================
# === CYCLE MODE LOGIC (Unchanged) =======================================================
# ========================================================================================
def schedule_next_break(room_id):
    if room_id not in bot_state.cycle_timers or bot_state.stop_bot_event.is_set(): return
    start_command = random.choice(Config.CYCLE_START_COMMANDS)
    reply_to_room(room_id, start_command)
    logging.info(f"[Cycle] Break ended. Sending START command '{start_command}' to room '{bot_state.room_id_to_name.get(room_id)}'.")
    work_duration = random.randint(Config.CYCLE_WORK_MIN_SECONDS, Config.CYCLE_WORK_MAX_SECONDS)
    bot_state.work_end_times[room_id] = time.time() + work_duration
    bot_state.break_end_times.pop(room_id, None)
    logging.info(f"[Cycle] Room '{bot_state.room_id_to_name.get(room_id)}': Working for {work_duration/60:.1f} minutes.")
    timer = threading.Timer(work_duration, take_a_break, args=[room_id])
    bot_state.cycle_timers[room_id] = timer
    timer.start()

def take_a_break(room_id):
    if room_id not in bot_state.cycle_timers or bot_state.stop_bot_event.is_set(): return
    stop_command = random.choice(Config.CYCLE_STOP_COMMANDS)
    reply_to_room(room_id, stop_command)
    logging.info(f"[Cycle] Starting break. Sending STOP command '{stop_command}' to room '{bot_state.room_id_to_name.get(room_id)}'.")
    break_duration = random.randint(Config.CYCLE_BREAK_MIN_SECONDS, Config.CYCLE_BREAK_MAX_SECONDS)
    bot_state.break_end_times[room_id] = time.time() + break_duration
    bot_state.work_end_times.pop(room_id, None)
    logging.info(f"[Cycle] Room '{bot_state.room_id_to_name.get(room_id)}': On break for {break_duration:.1f} seconds.")
    timer = threading.Timer(break_duration, schedule_next_break, args=[room_id])
    bot_state.cycle_timers[room_id] = timer
    timer.start()

def start_cycle_for_room(room_id, show_message=True):
    if room_id in bot_state.cycle_timers: return
    bot_state.cycle_timers[room_id] = None
    schedule_next_break(room_id)
    if show_message: reply_to_room(room_id, "✅ Cycle mode activated.")

def stop_cycle_for_room(room_id, show_message=True):
    if room_id in bot_state.cycle_timers:
        timer = bot_state.cycle_timers.pop(room_id)
        if timer: timer.cancel()
    bot_state.break_end_times.pop(room_id, None)
    bot_state.work_end_times.pop(room_id, None)
    start_command = random.choice(Config.CYCLE_START_COMMANDS)
    reply_to_room(room_id, start_command)
    if show_message: logging.info(f"[Cycle] Cycle mode stopped for room '{bot_state.room_id_to_name.get(room_id)}'.")

# ========================================================================================
# === WEB APP & BOT LIFECYCLE (HEAVILY MODIFIED) =========================================
# ========================================================================================
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ArcadeBot Login</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #111827; }
        .login-box { background-color: rgba(31, 41, 55, 0.8); backdrop-filter: blur(10px); }
    </style>
</head>
<body class="flex items-center justify-center h-screen font-sans">
    <div class="login-box w-full max-w-sm p-8 space-y-6 rounded-xl shadow-lg border border-gray-700">
        <h2 class="text-3xl font-bold text-center text-cyan-400">ArcadeBot Control Panel</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="p-4 text-sm text-red-200 bg-red-800/50 rounded-lg" role="alert">
                        {{ message }}
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post" class="space-y-6">
            <div>
                <input type="text" name="username" placeholder="Username" required
                       class="w-full px-4 py-2 text-white bg-gray-900 border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-cyan-500">
            </div>
            <div>
                <input type="password" name="password" placeholder="Password" required
                       class="w-full px-4 py-2 text-white bg-gray-900 border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-cyan-500">
            </div>
            <button type="submit"
                    class="w-full px-4 py-2 font-bold text-gray-900 bg-cyan-400 rounded-md hover:bg-cyan-300 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-gray-800 focus:ring-cyan-500 transition-colors duration-300">
                Login
            </button>
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ bot_name }} Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css">
    <style>
        :root { --main-bg: #111827; --sidebar-bg: #1f2937; --content-bg: #374151; --accent-cyan: #22d3ee; --accent-blue: #3b82f6; --text-light: #f3f4f6; --text-dark: #9ca3af; }
        body { background-color: var(--main-bg); color: var(--text-light); font-family: 'Inter', sans-serif; }
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&family=Fira+Code&display=swap');
        .font-fira-code { font-family: 'Fira Code', monospace; }
        .sidebar { background-color: rgba(31, 41, 55, 0.5); backdrop-filter: blur(12px); border-right: 1px solid rgba(255,255,255,0.1); }
        .chat-area { background-color: rgba(17, 24, 39, 0.7); backdrop-filter: blur(12px); }
        .chat-bubble-user { background-color: #4b5563; }
        .chat-bubble-bot { background-color: #3b82f6; }
        .chat-bubble-system { color: #9ca3af; font-style: italic; }
        .status-orb { box-shadow: 0 0 10px var(--color), 0 0 20px var(--color); }
        .nav-item.active { background-color: var(--accent-blue); color: white; }
        .nav-item:not(.active):hover { background-color: #4b5563; }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background-color: #4b5563; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background-color: #6b7280; }
    </style>
</head>
<body class="flex h-screen overflow-hidden">
    <!-- Sidebar -->
    <aside id="sidebar" class="sidebar flex flex-col w-64 p-4 space-y-6">
        <div class="flex items-center space-x-3">
            <div id="status-orb" class="status-orb w-4 h-4 rounded-full transition-all duration-500"></div>
            <h1 class="text-xl font-bold">{{ bot_name }}</h1>
        </div>
        <div>
            <p class="text-sm text-gray-400">Status</p>
            <p id="status-text" class="font-medium">Initializing...</p>
        </div>
        <div class="flex items-center justify-between">
             <a href="/start" class="flex-1 mr-1 text-center bg-green-500/80 hover:bg-green-500 text-white font-bold py-2 px-4 rounded transition-colors duration-300"><i class="fas fa-play"></i></a>
             <a href="/stop" class="flex-1 ml-1 text-center bg-red-500/80 hover:bg-red-500 text-white font-bold py-2 px-4 rounded transition-colors duration-300"><i class="fas fa-stop"></i></a>
        </div>
        <hr class="border-gray-600">
        <div class="flex-grow overflow-y-auto">
            <h2 class="text-sm font-semibold text-gray-400 mb-2">Joined Rooms</h2>
            <nav id="room-list" class="space-y-1"></nav>
        </div>
        <hr class="border-gray-600">
        <a href="/logout" class="w-full text-center bg-gray-600 hover:bg-gray-500 text-white font-bold py-2 px-4 rounded transition-colors duration-300">
            <i class="fas fa-sign-out-alt mr-2"></i>Logout
        </a>
    </aside>

    <!-- Main Content -->
    <main class="flex-1 flex flex-col p-4">
        <div id="chat-container" class="chat-area flex-1 flex flex-col rounded-xl border border-gray-700 overflow-hidden">
            <div id="chat-header" class="p-4 border-b border-gray-600 text-lg font-semibold">
                Select a room to view activity
            </div>
            <div id="chat-window" class="flex-1 p-4 overflow-y-auto space-y-4">
                <!-- Messages will be injected here -->
            </div>
            <div id="chat-input-container" class="p-4 border-t border-gray-600">
                <form id="message-form" class="flex items-center space-x-3">
                    <input type="text" id="message-input" placeholder="Type a message or command..." disabled
                           class="flex-1 bg-gray-900 px-4 py-2 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-cyan-500 transition-all">
                    <button type="submit" id="send-button" disabled
                            class="bg-cyan-500 text-white font-bold py-2 px-4 rounded-lg hover:bg-cyan-400 disabled:bg-gray-500 disabled:cursor-not-allowed transition-colors">
                        <i class="fas fa-paper-plane"></i>
                    </button>
                </form>
            </div>
        </div>
    </main>

<script>
let currentRoom = null;
let activityCache = {};

function formatTimestamp(isoString) {
    const date = new Date(isoString);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function renderMessage(activity) {
    const { type, username, text, timestamp, user_id } = activity;
    const time = formatTimestamp(timestamp);
    let bubbleHtml = '';

    if (type === 'message') {
        const isBot = username === '{{ bot_name }}';
        const bubbleClass = isBot ? 'chat-bubble-bot self-end' : 'chat-bubble-user self-start';
        const alignClass = isBot ? 'text-right' : 'text-left';
        bubbleHtml = `
            <div class="flex flex-col ${isBot ? 'items-end' : 'items-start'}">
                <div class="text-xs text-gray-400 mb-1">${username} <span class="font-fira-code text-gray-500">${time}</span></div>
                <div class="${bubbleClass} max-w-lg p-3 rounded-xl shadow">
                    <p class="text-white">${text}</p>
                </div>
            </div>`;
    } else if (type === 'system') {
        bubbleHtml = `
            <div class="chat-bubble-system text-center text-sm py-1">
                <span class="font-fira-code text-gray-500">${time}</span> - ${text}
            </div>`;
    }
    return bubbleHtml;
}

async function fetchAndUpdateStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();

        // Update Status Orb and Text
        const statusOrb = document.getElementById('status-orb');
        const statusText = document.getElementById('status-text');
        if (data.is_running) {
            statusOrb.style.setProperty('--color', data.is_connected ? '#22c55e' : '#f97316'); // green or orange
            statusText.textContent = data.is_connected ? 'Running & Connected' : 'Running & Disconnected';
        } else {
            statusOrb.style.setProperty('--color', '#ef4444'); // red
            statusText.textContent = 'Stopped';
        }
        statusOrb.className = 'status-orb w-4 h-4 rounded-full transition-all duration-500'; // re-apply to trigger animation

        // Update Room List
        const roomList = document.getElementById('room-list');
        const activeRooms = new Set(data.joined_rooms.map(r => r.name.toLowerCase()));
        
        let roomListHtml = '';
        data.joined_rooms.forEach(room => {
            const isActive = room.name.toLowerCase() === currentRoom;
            roomListHtml += \`
                <a href="#" class="nav-item flex items-center p-2 rounded-md transition-colors duration-200 \${isActive ? 'active' : ''}" 
                   onclick="selectRoom('${room.name.toLowerCase()}')">
                   <i class="fas fa-hashtag w-5 text-gray-400"></i>
                   <span>\${room.name}</span>
                </a>
            \`;
        });
        roomList.innerHTML = roomListHtml;

    } catch (error) {
        console.error('Error fetching status:', error);
        document.getElementById('status-text').textContent = 'Error fetching status';
        document.getElementById('status-orb').style.setProperty('--color', '#ef4444');
    }
}

async function fetchAndUpdateActivity(roomName) {
    if (!roomName) return;
    try {
        const response = await fetch(\`/api/activity/\${roomName}\`);
        const data = await response.json();
        
        const newActivities = data.activities;
        if (JSON.stringify(activityCache[roomName]) === JSON.stringify(newActivities)) {
            return; // No change
        }
        activityCache[roomName] = newActivities;

        const chatWindow = document.getElementById('chat-window');
        const shouldScroll = chatWindow.scrollTop + chatWindow.clientHeight >= chatWindow.scrollHeight - 50;

        chatWindow.innerHTML = newActivities.map(renderMessage).join('');

        if (shouldScroll) {
            chatWindow.scrollTop = chatWindow.scrollHeight;
        }

    } catch (error) {
        console.error('Error fetching activity:', error);
    }
}

function selectRoom(roomName) {
    currentRoom = roomName.toLowerCase();
    
    // Update UI
    document.getElementById('chat-header').textContent = `Activity in #${roomName}`;
    document.querySelectorAll('.nav-item').forEach(el => {
        el.classList.remove('active');
        if (el.innerText.toLowerCase().trim() === roomName) {
            el.classList.add('active');
        }
    });

    // Enable input
    document.getElementById('message-input').disabled = false;
    document.getElementById('send-button').disabled = false;
    document.getElementById('message-input').placeholder = `Message #${roomName}`;

    // Clear and fetch new data
    document.getElementById('chat-window').innerHTML = '';
    activityCache[roomName] = null; // Force refresh
    fetchAndUpdateActivity(currentRoom);
}

async function sendMessage(event) {
    event.preventDefault();
    const input = document.getElementById('message-input');
    const text = input.value.trim();
    if (!text || !currentRoom) return;

    try {
        await fetch('/api/send_message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ room_name: currentRoom, text: text })
        });
        input.value = '';
        // Manually add a temporary bot message to the UI for instant feedback
        const tempActivity = {
            type: 'message',
            username: '{{ bot_name }}',
            text: text,
            timestamp: new Date().toISOString()
        };
        const chatWindow = document.getElementById('chat-window');
        chatWindow.insertAdjacentHTML('beforeend', renderMessage(tempActivity));
        chatWindow.scrollTop = chatWindow.scrollHeight;
        
    } catch (error) {
        console.error('Error sending message:', error);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    fetchAndUpdateStatus();
    setInterval(fetchAndUpdateStatus, 3000);
    setInterval(() => fetchAndUpdateActivity(currentRoom), 2000);
    document.getElementById('message-form').addEventListener('submit', sendMessage);
});
</script>
</body>
</html>
"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['username'] == Config.PANEL_USERNAME and request.form['password'] == Config.PANEL_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('home'))
        else:
            flash('Wrong Username or Password!', 'error')
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
def home():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template_string(DASHBOARD_TEMPLATE, bot_name=Config.BOT_USERNAME)

@app.route('/start')
def start_bot_route():
    if (uptime_key := request.args.get('key')) and uptime_key == Config.UPTIME_SECRET_KEY:
        start_bot_logic()
        return "Bot start initiated by uptime service."
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    start_bot_logic()
    return redirect(url_for('home'))

@app.route('/stop')
def stop_bot_route():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    stop_bot_logic()
    return redirect(url_for('home'))

# --- NEW API ENDPOINTS ---
@app.route('/api/status')
def api_status():
    if not session.get('logged_in'): return jsonify({"error": "Not authenticated"}), 401
    
    is_running = bot_thread and bot_thread.is_alive()
    uptime = "N/A"
    if is_running and bot_state.bot_start_time:
        delta = timedelta(seconds=int(time.time() - bot_state.bot_start_time))
        uptime = str(delta)

    return jsonify({
        "is_running": is_running,
        "is_connected": bot_state.is_connected,
        "bot_name": Config.BOT_USERNAME,
        "uptime": uptime,
        "joined_rooms": [{"name": name, "id": id} for id, name in bot_state.room_id_to_name.items() if name.lower() in bot_state.startup_rooms]
    })

@app.route('/api/activity/<room_name>')
def api_activity(room_name):
    if not session.get('logged_in'): return jsonify({"error": "Not authenticated"}), 401
    
    room_name_lower = room_name.lower()
    if room_name_lower in bot_state.room_activities:
        with bot_state.activity_lock:
            activities = list(bot_state.room_activities[room_name_lower])
        return jsonify({"room_name": room_name, "activities": activities})
    return jsonify({"error": "Room not found or not tracked"}), 404

@app.route('/api/send_message', methods=['POST'])
def api_send_message():
    if not session.get('logged_in'): return jsonify({"error": "Not authenticated"}), 401
    
    data = request.json
    room_name = data.get('room_name')
    text = data.get('text')

    if not room_name or not text:
        return jsonify({"error": "Missing room_name or text"}), 400

    room_id = bot_state.room_name_to_id.get(room_name.lower())
    if not room_id:
        return jsonify({"error": "Bot is not in that room"}), 404
        
    reply_to_room(room_id, text)
    # Add our own message to the log for instant UI update
    add_activity(room_name, {'type': 'message', 'username': Config.BOT_USERNAME, 'user_id': bot_state.bot_user_id, 'text': text})
    return jsonify({"status": "Message sent"}), 200

def start_bot_logic():
    global bot_thread
    if not bot_thread or not bot_thread.is_alive():
        logging.info("WEB PANEL: Received request to start the bot.")
        bot_state.stop_bot_event.clear()
        bot_state.bot_start_time = time.time()
        # Reset activity logs on start
        with bot_state.activity_lock:
            for room_name in bot_state.startup_rooms:
                bot_state.room_activities[room_name].clear()
                add_activity(room_name, {'type': 'system', 'text': 'Bot starting...'})

        bot_thread = threading.Thread(target=connect_to_howdies, daemon=True)
        bot_thread.start()
        if not bot_state.log_cleanup_thread or not bot_state.log_cleanup_thread.is_alive():
            bot_state.log_cleanup_thread = threading.Thread(target=cleanup_old_logs, daemon=True)
            bot_state.log_cleanup_thread.start()

def stop_bot_logic():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        logging.info("WEB PANEL: Received request to stop the bot.")
        if bot_state.is_roamer_active:
            handle_roamer_command('off', None) # Pass None for room_id
        bot_state.stop_bot_event.set()
        for room_id in list(bot_state.cycle_timers.keys()):
            stop_cycle_for_room(room_id, show_message=False)
        if bot_state.ws_instance:
            try:
                bot_state.ws_instance.close()
            except Exception:
                pass
        bot_thread.join(timeout=5)
        bot_thread = None
        bot_state.bot_start_time = None
        with bot_state.activity_lock:
            for room_name in bot_state.startup_rooms:
                 add_activity(room_name, {'type': 'system', 'text': 'Bot stopped by panel.'})


# ========================================================================================
# === CORE BOT UTILITIES & COMMANDS ======================================================
# ========================================================================================
def load_masters():
    masters_str = Config.MASTERS_LIST
    if masters_str:
        bot_state.masters = [name.strip().lower() for name in masters_str.split(',')]
    logging.info(f"✅ Loaded {len(bot_state.masters)} masters from .env.")

def send_ws_message(payload):
    if bot_state.is_connected and bot_state.ws_instance:
        try:
            bot_state.ws_instance.send(json.dumps(payload))
        except Exception as e:
            logging.error(f"Error sending message: {e}")
    else:
        logging.warning("Warning: WebSocket is not connected.")

def reply_to_room(room_id, text):
    send_ws_message({"handler": "chatroommessage", "type": "text", "roomid": room_id, "text": text})

def leave_room(room_id):
    send_ws_message({"handler": "leaveroom", "roomid": room_id})
    room_name = bot_state.room_id_to_name.pop(room_id, None)
    if room_name:
        bot_state.room_name_to_id.pop(room_name.lower(), None)

def send_delayed_quiz_answer(room_id, answer_text):
    if bot_state.stop_bot_event.is_set(): return
    delay_ms = random.randint(Config.QUIZ_ANSWER_DELAY_MIN_MS, Config.QUIZ_ANSWER_DELAY_MAX_MS)
    time.sleep(delay_ms / 1000.0)
    if not bot_state.stop_bot_event.is_set():
        reply_to_room(room_id, answer_text)
        room_name = bot_state.room_id_to_name.get(room_id)
        if room_name:
            add_activity(room_name, {'type': 'message', 'username': Config.BOT_USERNAME, 'user_id': bot_state.bot_user_id, 'text': answer_text})

def get_token():
    logging.info("🔑 Acquiring login token...")
    if not Config.BOT_PASSWORD:
        logging.critical("🔴 CRITICAL: BOT_PASSWORD not set!")
        return None
    try:
        response = requests.post(Config.LOGIN_URL, json={"username": Config.BOT_USERNAME, "password": Config.BOT_PASSWORD}, headers=Config.BROWSER_HEADERS, timeout=15)
        response.raise_for_status()
        token = response.json().get("token")
        if token:
            logging.info("✅ Token acquired.")
            return token
        else:
            logging.error(f"🔴 Failed to get token: {response.text}")
            return None
    except requests.RequestException as e:
        logging.critical(f"🔴 Error fetching token: {e}")
        return None

def join_room(room_name, source=None):
    send_ws_message({"handler": "joinchatroom", "name": room_name, "roomPassword": "", "__source": source})

def join_startup_rooms():
    logging.info("Joining startup rooms...")
    time.sleep(1)
    if not (rooms_str := Config.ROOMS_TO_JOIN):
        logging.info("No startup rooms defined in ROOMS_TO_JOIN.")
        return
    for room_name in [name.strip() for name in rooms_str.split(',')]:
        if bot_state.stop_bot_event.is_set(): break
        if room_name:
            time.sleep(Config.ROOM_JOIN_DELAY_SECONDS)
            join_room(room_name, source='startup_join')
    if not bot_state.stop_bot_event.is_set():
        logging.info("✅ Finished joining startup rooms.")

# --- COMMAND HANDLERS (Unchanged, but logging can be added) ---
def handle_help(room_id):
    help_text = (
        "🤖 **ArcadeBot Help Menu** 🤖\n"
        "-----------------------------------\n"
        "**General:** `!j <room>`\n"
        "**Master-Only:** `!status`, `!quiz on|off`, `!cycle on|off`, `!delay [min] [max]`\n"
        "**Roamer:** `!roamer on|off`, `!roamlog`, `!roamnow <room>`"
    )
    reply_to_room(room_id, help_text)

def handle_status_command(room_id):
    status_lines = ["🤖 **Bot Status Report** 🤖"]
    roamer_status = "ON" if bot_state.is_roamer_active else "OFF"
    with bot_state.roam_lock: visited_count = len(bot_state.visited_roam_rooms)
    status_lines.append(f"--- Global ---\n• Roamer: **{roamer_status}** (Visited {visited_count}/24h)")
    room_name = bot_state.room_id_to_name.get(room_id, "this room")
    status_lines.append(f"--- Status for '{room_name}' ---")
    if (quiz_bot := bot_state.quiz_solvers.get(room_id)):
        status_lines.append(f"• Quiz Solver: **ON** (for *{quiz_bot}*)")
    else: status_lines.append("• Quiz Solver: **OFF**")
    if room_id in bot_state.cycle_timers:
        now = time.time()
        if (break_end := bot_state.break_end_times.get(room_id)) and now < break_end:
            status_lines.append(f"• Cycle Mode: **ON** (On Break for {break_end - now:.0f}s)")
        elif (work_end := bot_state.work_end_times.get(room_id)) and now < work_end:
            status_lines.append(f"• Cycle Mode: **ON** (Working for {(work_end - now) / 60:.1f} more mins)")
        else: status_lines.append("• Cycle Mode: **ON** (Transitioning)")
    else: status_lines.append("• Cycle Mode: **OFF**")
    reply_to_room(room_id, "\n".join(status_lines))

def handle_roamnow_command(args, room_id):
    if not args: return reply_to_room(room_id, "Usage: `!roamnow <room_name>`")
    target_room = " ".join(args)
    reply_to_room(room_id, f"✅ Forcing a roam to '{target_room}'.")
    threading.Thread(target=perform_roam_action, args=(target_room,), daemon=True).start()

def handle_roamer_command(sub_command, room_id):
    message_target_id = room_id # Can be None
    
    if sub_command == 'on':
        if bot_state.is_roamer_active:
            if message_target_id: reply_to_room(message_target_id, "ℹ️ Spin Roamer is already running.")
        else:
            bot_state.stop_roamer_event.clear()
            bot_state.roamer_thread = threading.Thread(target=roamer_logic, daemon=True)
            bot_state.roamer_thread.start(); bot_state.is_roamer_active = True
            if message_target_id: reply_to_room(message_target_id, "✅ Spin Roamer 2.0 activated.")
    elif sub_command == 'off':
        if not bot_state.is_roamer_active:
            if message_target_id: reply_to_room(message_target_id, "ℹ️ Spin Roamer is not running.")
        else:
            bot_state.stop_roamer_event.set()
            if bot_state.roamer_thread: bot_state.roamer_thread.join(timeout=5)
            bot_state.is_roamer_active, bot_state.roamer_thread = False, None
            if message_target_id: reply_to_room(message_target_id, "✅ Spin Roamer deactivated.")
    else:
        if message_target_id: reply_to_room(message_target_id, "Usage: `!roamer on|off`")

def handle_roamlog_command(room_id):
    if not supabase: return reply_to_room(room_id, "❌ Database not configured. Cannot fetch roam logs.")
    try:
        response = supabase.table('roam_logs').select("room_name, prize_won, roam_time").order("roam_time", desc=True).limit(10).execute()
        if not response.data: return reply_to_room(room_id, "No spin activity recorded.")
        log_strings = ["--- Spin Roamer Log (Last 10) ---"]
        for log in response.data:
            timestamp = datetime.fromisoformat(log['roam_time']).strftime('%I:%M %p')
            log_strings.append(f"• `[{timestamp}]` in **{log['room_name']}**: Won _{log['prize_won']}_")
        reply_to_room(room_id, "\n".join(log_strings))
    except Exception as e:
        logging.error(f"[DB] Error fetching roam log: {e}", exc_info=True)
        reply_to_room(room_id, "❌ Error fetching logs from the database.")

def handle_quiz_command(sub_command, args, room_id):
    if sub_command == 'on':
        if not args: return reply_to_room(room_id, "Usage: `!quiz on <bot_username>`")
        quiz_bot_username = args[0].lower(); bot_state.quiz_solvers[room_id] = quiz_bot_username
        start_cycle_for_room(room_id, show_message=False)
        reply_to_room(room_id, f"✅ Quiz solver & Cycle mode enabled for '{quiz_bot_username}'.")
    elif sub_command == 'off':
        if room_id in bot_state.quiz_solvers:
            stop_cycle_for_room(room_id, show_message=False)
            del bot_state.quiz_solvers[room_id]
            if room_id in bot_state.processed_question_ids: del bot_state.processed_question_ids[room_id]
            reply_to_room(room_id, "✅ Quiz solver & Cycle mode disabled.")
        else: reply_to_room(room_id, "ℹ️ Quiz solver is not active in this room.")
    else: reply_to_room(room_id, "Usage: `!quiz on <bot>` or `!quiz off`")

def handle_delay_command(args, room_id):
    if not args:
        min_d, max_d = Config.QUIZ_ANSWER_DELAY_MIN_MS, Config.QUIZ_ANSWER_DELAY_MAX_MS
        return reply_to_room(room_id, f"ℹ️ Current delay: {min_d}ms - {max_d}ms.")
    try:
        if len(args) != 2: return reply_to_room(room_id, "Usage: `!delay <min_ms> <max_ms>`")
        n_min, n_max = int(args[0]), int(args[1])
        if n_min < 0 or n_max < 0: return reply_to_room(room_id, "❌ Error: Negative delay.")
        if n_min > n_max: return reply_to_room(room_id, "❌ Error: Min > Max.")
        Config.QUIZ_ANSWER_DELAY_MIN_MS, Config.QUIZ_ANSWER_DELAY_MAX_MS = n_min, n_max
        logging.info(f"MASTER CMD: Delay updated to {n_min}ms - {n_max}ms")
        reply_to_room(room_id, f"✅ Delay updated to `{n_min}ms - {n_max}ms`.")
    except ValueError: reply_to_room(room_id, "❌ Error: Invalid numbers.")

def handle_cycle_command(sub_command, room_id):
    if sub_command == 'on':
        if room_id not in bot_state.quiz_solvers: reply_to_room(room_id, "ℹ️ Quiz must be on to start cycle manually.")
        elif room_id in bot_state.cycle_timers: reply_to_room(room_id, "ℹ️ Cycle already on.")
        else: start_cycle_for_room(room_id)
    elif sub_command == 'off':
        if room_id not in bot_state.cycle_timers: reply_to_room(room_id, "ℹ️ Cycle not active.")
        else: stop_cycle_for_room(room_id); reply_to_room(room_id, "✅ Cycle mode deactivated.")
    else: reply_to_room(room_id, "Usage: `!cycle on|off`")

def process_command(sender, room_id, message_text):
    try: parts = shlex.split(message_text.strip())
    except ValueError: parts = message_text.strip().split()
    command, args = parts[0][1:].lower(), parts[1:]
    is_master = sender['name'].lower() in bot_state.masters
    if command == 'help': handle_help(room_id)
    elif command == 'j':
        if args: join_room(" ".join(args))
        else: reply_to_room(room_id, "Usage: `!j <room>`")
    elif is_master:
        if command == 'status': handle_status_command(room_id)
        elif command == 'quiz': handle_quiz_command(args[0] if args else '', args[1:], room_id)
        elif command == 'delay': handle_delay_command(args, room_id)
        elif command == 'cycle': handle_cycle_command(args[0] if args else '', room_id)
        elif command == 'roamer': handle_roamer_command(args[0] if args else '', room_id)
        elif command == 'roamlog': handle_roamlog_command(room_id)
        elif command == 'roamnow': handle_roamnow_command(args, room_id)

# ========================================================================================
# === WEBSOCKET HANDLERS & MAIN ==========================================================
# ========================================================================================
def on_open(ws):
    logging.info("🚀 WebSocket connection opened. Logging in...")
    bot_state.is_connected = True
    bot_state.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
    send_ws_message({"handler": "login", "username": Config.BOT_USERNAME, "password": Config.BOT_PASSWORD, "token": bot_state.token})
    for room_name in bot_state.startup_rooms:
        add_activity(room_name, {'type': 'system', 'text': 'WebSocket connected.'})

def on_message(ws, message_str):
    if '"handler":"ping"' in message_str: return
    try:
        data = json.loads(message_str)
        handler = data.get("handler")
        
        if handler == "login" and data.get("status") == "success":
            bot_state.bot_user_id = data.get('userID')
            logging.info(f"✅ Login successful! Bot ID: {bot_state.bot_user_id}.")
            for room_name in bot_state.startup_rooms:
                add_activity(room_name, {'type': 'system', 'text': f'Logged in as {Config.BOT_USERNAME}.'})
            load_visited_rooms_from_db()
            threading.Thread(target=join_startup_rooms, daemon=True).start()

        elif handler == "chatroomplus" and "data" in data:
            with bot_state.roam_lock:
                for room in data["data"]:
                    if "name" in room and room.get("userCount", 0) > 0:
                        bot_state.roamable_rooms.add(room["name"])

        elif handler == "joinchatroom" and data.get("error") == 0:
            room_id, room_name = data.get('roomid'), data.get('name')
            bot_state.room_id_to_name[room_id] = room_name
            bot_state.room_name_to_id[room_name.lower()] = room_id
            logging.info(f"✅ Joined room: '{room_name}' (ID: {room_id})")
            add_activity(room_name, {'type': 'system', 'text': f'Successfully joined room.'})

        elif handler == "userkicked" and data.get("userid") == bot_state.bot_user_id:
            room_id = data.get('roomid')
            room_name = bot_state.room_id_to_name.get(room_id)
            if room_name:
                add_activity(room_name, {'type': 'system', 'text': f'Kicked from room.'})

            if room_id in bot_state.quiz_solvers: del bot_state.quiz_solvers[room_id]
            if room_id in bot_state.processed_question_ids: del bot_state.processed_question_ids[room_id]
            if room_id in bot_state.cycle_timers: stop_cycle_for_room(room_id, show_message=False)
            
            if room_name := bot_state.room_id_to_name.pop(room_id, None):
                bot_state.room_name_to_id.pop(room_name.lower(), None)
                if room_name.lower() in bot_state.startup_rooms:
                    logging.warning(f"⚠️ Kicked from startup room '{room_name}'. Rejoining...");
                    time.sleep(Config.REJOIN_ON_KICK_DELAY_SECONDS)
                    join_room(room_name, source='startup_join')
                else:
                    logging.warning(f"⚠️ Kicked from '{room_name}'. Not a startup room.")

        elif handler == "chatroommessage":
            room_id = data.get('roomid')
            room_name = bot_state.room_id_to_name.get(room_id)
            if not room_name: return

            text = data.get('text', '').strip()
            user_id = data.get('userid')
            username = data.get('username')
            
            # Add to activity log for dashboard
            add_activity(room_name, {'type': 'message', 'user_id': user_id, 'username': username, 'text': text})

            # Roamer prize listening logic
            if room_id == bot_state.listening_for_prize_in_room:
                prize = extract_prize(text, Config.BOT_USERNAME)
                if prize and bot_state.prize_found_event:
                    bot_state.last_prize_won = prize
                    bot_state.prize_found_event.set()

            if str(user_id) == str(bot_state.bot_user_id): return
            
            if text.startswith('!'):
                threading.Thread(target=process_command, args=({'id': user_id, 'name': username}, room_id, text), daemon=True).start()
            
            if room_id in bot_state.quiz_solvers:
                threading.Thread(target=process_quiz_message, args=(room_id, text, username), daemon=True).start()
                
    except (json.JSONDecodeError, Exception) as e:
        logging.error(f"Error in on_message: {e}", exc_info=True)

def on_error(ws, error):
    logging.error(f"--- WebSocket Error: {error} ---")
    for room_name in bot_state.startup_rooms:
        add_activity(room_name, {'type': 'system', 'text': f'WebSocket Error: {error}'})

def on_close(ws, close_status_code, close_msg):
    bot_state.is_connected = False
    for room_name in bot_state.startup_rooms:
        add_activity(room_name, {'type': 'system', 'text': 'WebSocket connection closed.'})
        
    if bot_state.stop_bot_event.is_set():
        logging.info("--- Bot gracefully stopped by web panel. ---")
    else:
        logging.warning(f"--- WebSocket closed unexpectedly. Reconnecting in {bot_state.reconnect_delay}s... ---")
        if not bot_state.stop_bot_event.is_set():
          time.sleep(bot_state.reconnect_delay)
          bot_state.reconnect_delay = min(bot_state.reconnect_delay * 2, Config.MAX_RECONNECT_DELAY)

def connect_to_howdies():
    bot_state.token = get_token()
    if not bot_state.token or bot_state.stop_bot_event.is_set():
        logging.error("Could not get token or stop event was set.")
        bot_state.is_connected = False
        return
    ws_url = f"{Config.WS_URL}?token={bot_state.token}"
    ws_app = websocket.WebSocketApp(ws_url, header=Config.BROWSER_HEADERS, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    bot_state.ws_instance = ws_app
    while not bot_state.stop_bot_event.is_set():
        ws_app.run_forever(ping_interval=30, ping_timeout=10)
        if bot_state.stop_bot_event.is_set(): break
        logging.info("WebSocket connection ended. Will try to reconnect if not stopped.")
        time.sleep(bot_state.reconnect_delay)
    bot_state.is_connected = False
    bot_state.ws_instance = None
    logging.info("Bot's run_forever loop has ended.")

# ========================================================================================
# === QUIZ SOLVER LOGIC (Unchanged) ======================================================
# ========================================================================================
def solve_math_problem(problem_str):
    try:
        problem_str = problem_str.replace('x', '*').replace('X', '*').replace('÷', '/').strip()
        if '=' not in problem_str: return None
        left, right = problem_str.split('=', 1)
        safe_dict = {"__builtins__": {}}
        if '?' in right: return int(eval(left, safe_dict, {}))
        if '?' in left:
            right_val = int(eval(right, safe_dict, {}))
            for i in range(-2000, 2000):
                try:
                    if int(eval(left.replace('?', str(i)), safe_dict, {})) == right_val: return i
                except: continue
        return None
    except Exception: return None

def is_simple_equation(problem_str):
    problem_str = problem_str.strip()
    if '=' not in problem_str: return False
    if any(op in problem_str for op in ['+', '-', '*', '/']): return True
    if re.fullmatch(r'[\d\s\?]+', problem_str.replace('-', ' ')): return False
    return True

def process_quiz_message(room_id, text, username):
    if time.time() < bot_state.break_end_times.get(room_id, 0): return
    quiz_bot_username = bot_state.quiz_solvers.get(room_id)
    if not (quiz_bot_username and username.lower() == quiz_bot_username.lower()): return
    end_of_round_patterns = ['The answer was', 'New Record', 'Lightning Fast', 'Hat-trick', 'Right Answer', 'Too Slow', 'Late', 'Super', 'Speedy']
    if any(pattern in text for pattern in end_of_round_patterns):
        if room_id in bot_state.processed_question_ids: del bot_state.processed_question_ids[room_id]
        return
    hint_match = re.search(r'Hint\s*:\s*(.*)', text, re.IGNORECASE)
    if hint_match:
        problem = hint_match.group(1).strip()
        answer = solve_math_problem(problem)
        if answer is not None: threading.Thread(target=send_delayed_quiz_answer, args=(room_id, str(abs(answer))), daemon=True).start()
        return
    question_id_match = re.search(r'(?:Question\s*#|#)(\d+)', text)
    if question_id_match:
        question_id = int(question_id_match.group(1))
        if bot_state.processed_question_ids.get(room_id) == question_id: return
        bot_state.processed_question_ids[room_id] = question_id
        problem_match = re.search(r'\*\s*(?:M[аa]ths|Maths)\s*-\s*(.*?)\s*\*', text, re.DOTALL)
        if not problem_match: return
        problem = problem_match.group(1).strip()
        if is_simple_equation(problem):
            answer = solve_math_problem(problem)
            if answer is not None: threading.Thread(target=send_delayed_quiz_answer, args=(room_id, str(answer)), daemon=True).start()
            else: reply_to_room(room_id, ".h")
        else: reply_to_room(room_id, ".h")

# ========================================================================================
# === MAIN EXECUTION BLOCK ===============================================================
# ========================================================================================
setup_logging()
load_masters()

if __name__ == "__main__":
    logging.info(f"--- Starting Web Panel for {Config.BOT_USERNAME} ---")
    logging.info(f"Open your browser to http://127.0.0.1:5000 to control the bot.")
    app.run(host='0.0.0.0', port=5000)