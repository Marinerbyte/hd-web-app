# ========================================================================================
# === 1. IMPORTS & SETUP =================================================================
# ========================================================================================
import websocket
import json
import requests
import threading
import time
import os
import sqlite3
import re
import logging
import shlex
import sys # ADDED FOR PYTHONANYWHERE LOGGING
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for, request, session, flash

load_dotenv()

# ========================================================================================
# === 2. LOGGING SETUP (MODIFIED FOR PYTHONANYWHERE) =====================================
# ========================================================================================
def setup_logging():
    # This setup is optimized for PythonAnywhere
    # It logs everything to the Error Log for easy debugging
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Clear existing handlers to avoid duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Create a handler that writes to stderr, which PythonAnywhere captures in the Error Log
    handler = logging.StreamHandler(sys.stderr) # IMPORTANT: Use sys.stderr
    handler.setFormatter(log_formatter)
    logger.addHandler(handler)
    
    logging.info("Logging system initialized (PythonAnywhere mode).")


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
    LOGIN_URL = "https://api.howdies.app/api/login"
    WS_URL = "wss://app.howdies.app/"
    DATABASE_FILE = "/home/senoyasi/arcade_bot.db" # UPDATED: Full path for PythonAnywhere
    CONFIG_FILE = "/home/senoyasi/bot_config.json" # UPDATED: Full path for PythonAnywhere
    ROOM_JOIN_DELAY_SECONDS = 2
    REJOIN_ON_KICK_DELAY_SECONDS = 3
    INITIAL_RECONNECT_DELAY = 10
    MAX_RECONNECT_DELAY = 300
    FONT_SELECTION_TIMEOUT_SECONDS = 60
    BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Origin": "https://howdies.app"
    }

class BotState:
    def __init__(self):
        self.bot_user_id = None
        self.token = None
        self.ws_instance = None
        self.is_connected = False
        self.masters = []
        self.room_id_to_name = {}
        self.room_name_to_id = {}
        self.user_profile_callbacks = {}
        self.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
        self.font_selection_sessions = {}
        self.active_font_map = None
        self.quiz_solvers = {}
        self.processed_question_ids = {}
        self.stop_bot_event = threading.Event()

bot_state = BotState()
db_lock = threading.Lock()
config_lock = threading.Lock()
bot_thread = None

# ========================================================================================
# === WEB APP WITH LOGIN PANEL ===========================================================
# ========================================================================================
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY

# --- TEMPLATES ---
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Login</title>
<style>
body{font-family:sans-serif;background:#121212;color:#e0e0e0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}
.login-box{background:#1e1e1e;padding:40px;border-radius:8px;box-shadow:0 4px 8px rgba(0,0,0,0.3);width:300px;}
h2{color:#bb86fc;text-align:center;}
.input-group{margin-bottom:20px;}
input{width:100%;padding:10px;border:1px solid #333;border-radius:4px;background:#2a2a2a;color:#e0e0e0;box-sizing: border-box;}
.btn{width:100%;padding:10px;border:none;border-radius:4px;background:#03dac6;color:#121212;font-size:16px;cursor:pointer;}
.flash{padding:10px;background:#cf6679;color:#121212;border-radius:4px;margin-bottom:15px;text-align:center;}
</style>
</head>
<body>
<div class="login-box">
<h2>Control Panel Login</h2>
{% with messages = get_flashed_messages() %}
  {% if messages %}
    <div class="flash">{{ messages[0] }}</div>
  {% endif %}
{% endwith %}
<form method="post">
<div class="input-group"><input type="text" name="username" placeholder="Username" required></div>
<div class="input-group"><input type="password" name="password" placeholder="Password" required></div>
<button type="submit" class="btn">Login</button>
</form>
</div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>{{ bot_name }} Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
body{font-family:sans-serif;background:#121212;color:#e0e0e0;margin:0;padding:40px;text-align:center;}
.container{max-width:800px;margin:auto;background:#1e1e1e;padding:20px;border-radius:8px;box-shadow:0 4px 8px rgba(0,0,0,0.3);}
h1{color:#bb86fc;}
.status{padding:15px;border-radius:5px;margin-top:20px;font-weight:bold;}
.running{background:#03dac6;color:#121212;}
.stopped{background:#cf6679;color:#121212;}
.buttons{margin-top:30px;}
.btn{padding:12px 24px;border:none;border-radius:5px;font-size:16px;cursor:pointer;margin:5px;text-decoration:none;color:#121212;display:inline-block;}
.btn-start{background-color:#03dac6;}
.btn-stop{background-color:#cf6679;}
.btn-logout{background-color:#666;color:#fff;position:absolute;top:20px;right:20px;}
</style>
</head>
<body>
<a href="/logout" class="btn btn-logout">Logout</a>
<div class="container">
<h1>{{ bot_name }} Dashboard</h1>
<div class="status {{ 'running' if 'Running' in bot_status else 'stopped' }}">
Bot Status: {{ bot_status }}
</div>
<div class="buttons">
<a href="/start" class="btn btn-start">Start Bot</a>
<a href="/stop" class="btn btn-stop">Stop Bot</a>
</div>
</div>
</body>
</html>
"""

# --- ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['username'] == Config.PANEL_USERNAME and request.form['password'] == Config.PANEL_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('home'))
        else:
            flash('Wrong Username or Password!')
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
def home():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    global bot_thread
    status = "Stopped"
    if bot_thread and bot_thread.is_alive():
        if bot_state.is_connected:
            status = "Running and Connected"
        else:
            status = "Running but Disconnected"

    return render_template_string(
        DASHBOARD_TEMPLATE,
        bot_name=Config.BOT_USERNAME,
        bot_status=status
    )

@app.route('/start')
def start_bot_route():
    uptime_key = request.args.get('key')
    if uptime_key == Config.UPTIME_SECRET_KEY:
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

def start_bot_logic():
    global bot_thread
    if not bot_thread or not bot_thread.is_alive():
        logging.info("WEB PANEL: Received request to start the bot.")
        bot_state.stop_bot_event.clear()
        bot_thread = threading.Thread(target=connect_to_howdies, daemon=True)
        bot_thread.start()

def stop_bot_logic():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        logging.info("WEB PANEL: Received request to stop the bot.")
        bot_state.stop_bot_event.set()
        if bot_state.ws_instance:
            try:
                bot_state.ws_instance.close()
            except Exception:
                pass
        bot_thread.join(timeout=5)
        bot_thread = None

# ========================================================================================
# === DATABASE MANAGER ===================================================================
# ========================================================================================
class DatabaseManager:
    MIGRATIONS = [
        (1, """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                coins INTEGER DEFAULT 0,
                auto_accept_friends INTEGER DEFAULT 0,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """),
        (2, """
            CREATE TABLE custom_fonts (
                font_id INTEGER PRIMARY KEY AUTOINCREMENT,
                font_map TEXT NOT NULL UNIQUE,
                added_by_user_id INTEGER NOT NULL
            );
        """)
    ]
    def __init__(self, db_file):
        self.db_file = db_file
        self.lock = threading.Lock()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def run_migrations(self):
        logging.info("Checking database schema...")
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='room_settings'")
            if c.fetchone():
                logging.warning("DB Migration: Found obsolete 'room_settings' table. Dropping it.")
                c.execute("DROP TABLE room_settings;")
                c.execute("PRAGMA user_version = 1;")
                conn.commit()
                logging.info("DB Migration: Obsolete table dropped and version reset.")
            
            c.execute("PRAGMA user_version;")
            current_version = c.fetchone()[0]

            adjusted_migrations = [(version if version < 3 else version - 1, script) for version, script in self.MIGRATIONS]

            for version, script in adjusted_migrations:
                if current_version < version:
                    logging.info(f"DB Migration: Applying version {version}...")
                    try:
                        c.executescript(script)
                        c.execute(f"PRAGMA user_version = {version};")
                        conn.commit()
                        logging.info(f"DB Migration: Version {version} applied successfully.")
                    except sqlite3.OperationalError as e:
                        logging.error(f"DB Migration: Error applying version {version}: {e}")

            conn.close()
        logging.info("✅ Database schema is up to date.")

    def get_or_create_user(self, user_id, username):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if c.fetchone():
                c.execute("UPDATE users SET username = ? WHERE user_id = ? AND username != ?", (username.lower(), user_id, username.lower()))
            else:
                logging.info(f"New user found: {username} ({user_id}). Adding to database.")
                c.execute("INSERT INTO users (user_id, username, coins) VALUES (?, ?, ?)", (user_id, username.lower(), 0))
            conn.commit()
            conn.close()

    def get_user_coins(self, user_id):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            conn.close()
            return row['coins'] if row else 0

    def update_user_coins(self, user_id, coins):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("UPDATE users SET coins = ? WHERE user_id = ?", (coins, user_id))
            conn.commit()
            conn.close()
    
    def add_font(self, font_map, user_id):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            try:
                c.execute("INSERT INTO custom_fonts (font_map, added_by_user_id) VALUES (?, ?)", (font_map, user_id))
                conn.commit()
                return True
            except sqlite3.IntegrityError: return False
            finally: conn.close()

    def get_all_fonts(self):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("SELECT font_id, font_map FROM custom_fonts ORDER BY font_id")
            fonts = c.fetchall()
            conn.close()
            return fonts

    def get_font_by_id(self, font_id):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("SELECT font_map FROM custom_fonts WHERE font_id = ?", (font_id,))
            row = c.fetchone()
            conn.close()
            return row['font_map'] if row else None
            
    def delete_font(self, font_id):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("DELETE FROM custom_fonts WHERE font_id = ?", (font_id,))
            conn.commit()
            conn.close()
    
    def set_auto_accept_friends(self, user_id, status):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("UPDATE users SET auto_accept_friends = ? WHERE user_id = ?", (status, user_id))
            conn.commit()
            conn.close()
    def get_auto_accept_status(self, user_id):
        with self.lock:
            conn = self._get_connection()
            c = conn.cursor()
            c.execute("SELECT auto_accept_friends FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            conn.close()
            return row['auto_accept_friends'] if row else 0

db_manager = DatabaseManager(Config.DATABASE_FILE)
# ========================================================================================
# === CORE BOT UTILITIES ==============================================================
# ========================================================================================
def save_config_to_file():
    with config_lock:
        try:
            with open(Config.CONFIG_FILE, 'r') as f: data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): data = {}
        data['masters'] = bot_state.masters
        font_id = None
        if bot_state.active_font_map:
            fonts = db_manager.get_all_fonts()
            if fonts:
                for font in fonts:
                    if font['font_map'] == bot_state.active_font_map:
                        font_id = font['font_id']; break
        data['active_font_id'] = font_id
        with open(Config.CONFIG_FILE, 'w') as f: json.dump(data, f, indent=4)

def load_config():
    try:
        with config_lock, open(Config.CONFIG_FILE, 'r') as f:
            config_data = json.load(f)
            bot_state.masters = [m.lower() for m in config_data.get("masters", [])]
            logging.info(f"✅ Loaded {len(bot_state.masters)} masters.")
            if active_font_id := config_data.get("active_font_id"):
                bot_state.active_font_map = db_manager.get_font_by_id(active_font_id)
                if bot_state.active_font_map: logging.info(f"✅ Loaded active font: {bot_state.active_font_map[:10]}...")
                else: logging.warning(f"⚠️ Active font ID {active_font_id} not found. Resetting.")
    except (FileNotFoundError, json.JSONDecodeError):
        logging.warning("Config file not found. Creating one with default master 'yasin'.")
        bot_state.masters = ["yasin"]
        with open(Config.CONFIG_FILE, 'w') as f: json.dump({"masters": bot_state.masters, "active_font_id": None}, f, indent=4)

def send_ws_message(payload):
    if bot_state.is_connected and bot_state.ws_instance:
        try:
            if payload.get("handler") not in ["ping", "pong"]: logging.info(f"--> SENDING: {json.dumps(payload)}")
            bot_state.ws_instance.send(json.dumps(payload))
        except Exception as e: logging.error(f"Error sending message: {e}")
    else: logging.warning("Warning: WebSocket is not connected.")

def reply_to_room(room_id, text):
    final_text = text
    if bot_state.active_font_map and re.search('[a-zA-Z]', text):
        normal_chars = "abcdefghijklmnopqrstuvwxyz"
        translation_table = str.maketrans(normal_chars + normal_chars.upper(), bot_state.active_font_map + bot_state.active_font_map)
        final_text = text.translate(translation_table)
    send_ws_message({"handler": "chatroommessage", "type": "text", "roomid": room_id, "text": final_text})

def send_dm(username, text): send_ws_message({"handler": "message", "type": "text", "to": username, "text": text})
def leave_room(room_id): send_ws_message({"handler": "leavechatroom", "roomid": room_id})
def get_token():
    logging.info("🔑 Acquiring login token...")
    if not Config.BOT_PASSWORD: logging.critical("🔴 CRITICAL: BOT_PASSWORD not set in .env file!"); return None
    try:
        response = requests.post(Config.LOGIN_URL, json={"username": Config.BOT_USERNAME, "password": Config.BOT_PASSWORD}, headers=Config.BROWSER_HEADERS, timeout=15)
        response.raise_for_status()
        token = response.json().get("token")
        if token: logging.info("✅ Token acquired."); return token
        else: logging.error(f"🔴 Failed to get token. Response: {response.text}"); return None
    except requests.RequestException as e: logging.critical(f"🔴 Error fetching token: {e}"); return None

def join_room(room_name, source=None):
    payload = {"handler": "joinchatroom", "name": room_name, "roomPassword": ""}
    if source: payload["__source"] = source
    send_ws_message(payload)

def request_profile_info(username, callback, **kwargs):
    clean_username = re.sub(r'^@', '', username).lower()
    bot_state.user_profile_callbacks.setdefault(clean_username, []).append((callback, kwargs))
    send_ws_message({"handler": "profile", "username": clean_username})

def join_startup_rooms():
    logging.info("Joining startup rooms from .env...")
    time.sleep(1)
    rooms_str = Config.ROOMS_TO_JOIN
    if not rooms_str:
        logging.info("No startup rooms defined in .env (ROOMS_TO_JOIN).")
        return
    room_names = [name.strip() for name in rooms_str.split(',')]
    for room_name in room_names:
        if bot_state.stop_bot_event.is_set(): break
        if room_name:
            time.sleep(Config.ROOM_JOIN_DELAY_SECONDS)
            join_room(room_name, source='startup_join')
    if not bot_state.stop_bot_event.is_set():
      logging.info("✅ Finished joining startup rooms.")

# ========================================================================================
# === COMMAND HANDLERS ===================================================================
# ========================================================================================
def handle_help(room_id):
    help_text = (
        "🤖 **ArcadeBot Help Menu** 🤖\n"
        "-----------------------------------\n"
        "**General:** `!u <user>`, `!j <room>`, `!l <room>`\n"
        "**Coins:** `!give <user> <amount>`\n"
        "**Friends:** `!af <user>`, `!ac <user>`, `!rf <user>`, `!df <user>`, `!autofr`\n"
        "-----------------------------------\n"
        "**Master-Only Commands**\n"
        "**Coins:** `!addcoins <u|a>`, `!setcoins <u|a>`\n"
        "**Fonts:** `!addf <map>`, `!listf`, `!use <num>`, `!delf <num>`, `!defaultf`\n"
        "**Quiz:** `!quiz on <bot>`, `!quiz off`\n"
        "**Admin:** `!k <u|a>`, `!m <u|a>`, `!um <u|a>`, `!r <u|r>`"
    )
    reply_to_room(room_id, help_text)

def on_profile_for_u_command(profile_data, room_id):
    if not profile_data or not profile_data.get('Username'): return reply_to_room(room_id, "❌ User not found.")
    coins = profile_data.get('Coins', 0)
    user_id = profile_data.get('UserID')
    username = profile_data.get('Username', 'N/A')
    if user_id:
        db_manager.get_or_create_user(user_id, username)
        db_manager.update_user_coins(user_id, coins)
    profile_text = (f"--- Profile: @{username} ---\n"
                  f"Role: {profile_data.get('Role', 'Member')}\n"
                  f"Coins: {db_manager.get_user_coins(user_id)} _🪙_")
    reply_to_room(room_id, profile_text)

def handle_give_coins_command(sender, target_username, amount_str, room_id):
    try:
        amount = int(amount_str)
        if amount <= 0: return reply_to_room(room_id, "❌ Amount must be positive.")
    except ValueError: return reply_to_room(room_id, "❌ Invalid amount.")
    sender_coins = db_manager.get_user_coins(sender['id'])
    if sender_coins < amount: return reply_to_room(room_id, f"❌ You only have {sender_coins} _🪙_.")
    def on_profile_found(profile_data, **kwargs):
        receiver_id = profile_data.get('UserID')
        if not receiver_id: return reply_to_room(room_id, f"❌ User '{target_username}' not found.")
        if sender['id'] == receiver_id: return reply_to_room(room_id, "❌ You cannot give coins to yourself.")
        db_manager.update_user_coins(sender['id'], sender_coins - amount)
        db_manager.update_user_coins(receiver_id, db_manager.get_user_coins(receiver_id) + amount)
        reply_to_room(room_id, f"✅ @{sender['name']} gave @{profile_data.get('Username', target_username)} {amount} _🪙_.")
    request_profile_info(target_username, callback=on_profile_found)

def handle_add_font(font_map, sender, room_id):
    if len(font_map) != 26: return reply_to_room(room_id, "❌ Font map must be 26 characters (A-Z).")
    if db_manager.add_font(font_map, sender['id']): reply_to_room(room_id, "✅ New font added!")
    else: reply_to_room(room_id, "ℹ️ This font already exists.")

def handle_list_fonts(sender, room_id):
    fonts = db_manager.get_all_fonts()
    if not fonts: return reply_to_room(room_id, "📭 No fonts added. Use `!addf`.")
    response = "📜 **Bot's Available Fonts** 📜\n"
    font_list_for_session = [font['font_id'] for font in fonts]
    for i, font in enumerate(fonts): response += f"`{i+1}.` {font['font_map'][:10]}...\n"
    response += f"\nReply with `!use <num>` or `!delf <num>` in {Config.FONT_SELECTION_TIMEOUT_SECONDS}s."
    bot_state.font_selection_sessions[sender['id']] = {'timestamp': time.time(), 'fonts': font_list_for_session}
    reply_to_room(room_id, response)

def handle_font_selection(command, sender, number_str, room_id):
    session = bot_state.font_selection_sessions.get(sender['id'])
    if not session or time.time() - session['timestamp'] > Config.FONT_SELECTION_TIMEOUT_SECONDS: return reply_to_room(room_id, "⏰ Session expired. Use `!listf` again.")
    try:
        choice_index = int(number_str) - 1
        font_id = session['fonts'][choice_index]
        if command == 'use':
            font_map = db_manager.get_font_by_id(font_id)
            if font_map:
                bot_state.active_font_map = font_map
                save_config_to_file()
                reply_to_room(room_id, f"✅ Bot will now use font `{int(number_str)}`.")
            else: reply_to_room(room_id, "❌ Font not found in DB.")
        elif command == 'delf':
            font_to_delete_map = db_manager.get_font_by_id(font_id)
            db_manager.delete_font(font_id)
            reply_to_room(room_id, f"✅ Font `{int(number_str)}` deleted.")
            if bot_state.active_font_map == font_to_delete_map:
                bot_state.active_font_map = None
                save_config_to_file()
                reply_to_room(room_id, "ℹ️ Active font was deleted, bot now uses default font.")
        del bot_state.font_selection_sessions[sender['id']]
    except (ValueError, IndexError): reply_to_room(room_id, "❌ Invalid number.")

def handle_default_font(sender, room_id):
    bot_state.active_font_map = None
    save_config_to_file()
    reply_to_room(room_id, "✅ Bot font reset to default.")

def handle_master_coin_command(command, target_username, amount_str, room_id):
    try: amount = int(amount_str); assert amount >= 0
    except (ValueError, AssertionError): return reply_to_room(room_id, "❌ Invalid amount.")
    def on_profile_found(profile_data, **kwargs):
        target_id = profile_data.get('UserID')
        if not target_id: return reply_to_room(room_id, f"❌ User '{target_username}' not found.")
        if command == 'addcoins':
            new_total = db_manager.get_user_coins(target_id) + amount
            db_manager.update_user_coins(target_id, new_total)
            reply_to_room(room_id, f"✅ Added {amount} _🪙_ to @{profile_data.get('Username')}. New balance: {new_total} _🪙_.")
        elif command == 'setcoins':
            db_manager.update_user_coins(target_id, amount)
            reply_to_room(room_id, f"✅ Set @{profile_data.get('Username')}'s balance to {amount} _🪙_.")
    request_profile_info(target_username, callback=on_profile_found)

def handle_admin_command(command, target_username, room_id, extra_arg=None):
    handler_map = {'k': 'kickuser', 'm': 'muteuser', 'um': 'unmuteuser'}
    if command in handler_map: send_ws_message({"handler": handler_map[command], "roomid": room_id, "target": target_username})
    elif command == 'r' and extra_arg: send_ws_message({"handler": "changerole", "roomid": room_id, "target": target_username, "role": extra_arg})

def handle_friend_command(command, target_username, room_id):
    def on_user_id_found(profile_data, **kwargs):
        user_id = profile_data.get('UserID')
        if not user_id: return reply_to_room(room_id, f"❌ User '{target_username}' not found.")
        handler_map = { 'af': 'addfriend', 'ac': 'acceptfriend', 'rf': 'rejectfriend', 'df': 'removefriend' }
        if action := handler_map.get(command): send_ws_message({"handler": action, "friendId": user_id})
    request_profile_info(target_username, callback=on_user_id_found, room_id=room_id)

def handle_autofr_toggle(sender_id, room_id, args):
    if not args or args[0] not in ['on', 'off']:
        status_text = "ON" if db_manager.get_auto_accept_status(sender_id) == 1 else "OFF"
        reply_to_room(room_id, f"ℹ️ Auto-accept is **{status_text}**. Use `!autofr on|off`.")
        return
    new_status = 1 if args[0] == 'on' else 0
    db_manager.set_auto_accept_friends(sender_id, new_status)
    reply_to_room(room_id, f"✅ Auto-accept is now **{'ON' if new_status == 1 else 'OFF'}**.")

def handle_quiz_command(sub_command, args, room_id):
    if sub_command == 'on':
        if not args: return reply_to_room(room_id, "Usage: `!quiz on <bot_username>`")
        quiz_bot_username = args[0].lower()
        bot_state.quiz_solvers[room_id] = quiz_bot_username
        reply_to_room(room_id, f"✅ Quiz solver enabled. Watching for questions from '{quiz_bot_username}'.")
    elif sub_command == 'off':
        if room_id in bot_state.quiz_solvers:
            del bot_state.quiz_solvers[room_id]
            if room_id in bot_state.processed_question_ids:
                del bot_state.processed_question_ids[room_id]
            reply_to_room(room_id, "✅ Quiz solver disabled for this room.")
        else:
            reply_to_room(room_id, "ℹ️ Quiz solver is not active in this room.")
    else:
        reply_to_room(room_id, "Usage: `!quiz on <bot_username>` or `!quiz off`")

def process_command(sender, room_id, message_text):
    db_manager.get_or_create_user(sender['id'], sender['name'])
    try: parts = shlex.split(message_text.strip())
    except ValueError: parts = message_text.strip().split()
    command, args = parts[0][1:].lower(), parts[1:]

    is_master = sender['name'].lower() in bot_state.masters

    if command == 'help': handle_help(room_id)
    elif command == 'u': request_profile_info(args[0].lstrip('@') if args else sender['name'], callback=on_profile_for_u_command, room_id=room_id)
    elif command == 'j':
        if args:
            room_to_join = " ".join(args)
            join_room(room_to_join, source='user_join_command')
            reply_to_room(room_id, f"✅ Attempting to join room: {room_to_join}")
        else:
            reply_to_room(room_id, "Usage: `!j <room>`")
    elif command == 'l':
        if not args: return reply_to_room(room_id, "Usage: `!l <room>`")
        target_room_name = " ".join(args).lower()
        if target_room_id := bot_state.room_name_to_id.get(target_room_name):
            leave_room(target_room_id)
            reply_to_room(room_id, f"✅ Left room: {target_room_name}")
        else: reply_to_room(room_id, f"❌ Not in room '{target_room_name}'.")
    elif command == 'give': handle_give_coins_command(sender, args[0], args[1], room_id) if len(args) >= 2 else reply_to_room(room_id, "Usage: `!give <user> <amount>`")
    elif command in ['af', 'ac', 'rf', 'df']: handle_friend_command(command, args[0], room_id) if args else reply_to_room(room_id, f"Usage: `!{command} <user>`")
    elif command == 'autofr': handle_autofr_toggle(sender['id'], room_id, args)

    elif is_master:
        if command == 'addf': handle_add_font(" ".join(args), sender, room_id) if args else reply_to_room(room_id, "Usage: `!addf <font_map>`")
        elif command == 'listf': handle_list_fonts(sender, room_id)
        elif command in ['use', 'delf']: handle_font_selection(command, sender, args[0], room_id) if args else reply_to_room(room_id, f"Usage: `!{command} <number>`")
        elif command == 'defaultf': handle_default_font(sender, room_id)
        elif command == 'quiz': handle_quiz_command(args[0] if args else '', args[1:], room_id)
        elif command in ['k', 'm', 'um']: handle_admin_command(command, args[0], room_id) if args else reply_to_room(room_id, f"Usage: `!{command} <user>`")
        elif command == 'r': handle_admin_command(command, args[0], room_id, args[1]) if len(args) >= 2 else reply_to_room(room_id, "Usage: `!r <user> <role>`")
        elif command in ['addcoins', 'setcoins']: handle_master_coin_command(command, args[0], args[1], room_id) if len(args) >= 2 else reply_to_room(room_id, f"Usage: `!{command} <user> <amount>`")

# ========================================================================================
# === QUIZ SOLVER LOGIC ==================================================================
# ========================================================================================
def solve_math_problem(problem_str):
    try:
        problem_str = problem_str.replace('x', '*').replace('X', '*').replace('÷', '/').strip()
        if '=' not in problem_str: return None
        
        left, right = problem_str.split('=', 1)
        safe_dict = {"__builtins__": {}}
        
        if '?' in right:
            return int(eval(left, safe_dict, {}))
            
        if '?' in left:
            right_val = int(eval(right, safe_dict, {}))
            for i in range(-2000, 2000):
                try:
                    if int(eval(left.replace('?', str(i)), safe_dict, {})) == right_val:
                        return i
                except (SyntaxError, TypeError, NameError, ZeroDivisionError):
                    continue
        return None
    except Exception as e:
        logging.error(f"[Math Solver] Error: {e} for problem '{problem_str}'")
        return None

def is_simple_equation(problem_str):
    problem_str = problem_str.strip()
    if '=' not in problem_str: return False
    if any(op in problem_str for op in ['+', '-', '*', '/']): return True
    if re.fullmatch(r'[\d\s\?]+', problem_str.replace('-', ' ')): return False
    return True

def process_quiz_message(room_id, text, username):
    quiz_bot_username = bot_state.quiz_solvers.get(room_id)
    if not (quiz_bot_username and username.lower() == quiz_bot_username.lower()):
        return

    end_of_round_patterns = ['The answer was', 'New Record', 'Lightning Fast', 'Hat-trick', 'Right Answer', 'Too Slow', 'Late', 'Super', 'Speedy']
    if any(pattern in text for pattern in end_of_round_patterns):
        if room_id in bot_state.processed_question_ids:
            del bot_state.processed_question_ids[room_id]
            logging.info(f"[Quiz Solver] End of round detected. Unlocking question for room {room_id}.")
        return

    hint_match = re.search(r'Hint\s*:\s*(.*)', text, re.IGNORECASE)
    if hint_match:
        problem = hint_match.group(1).strip()
        answer = solve_math_problem(problem)
        if answer is not None:
            final_answer = abs(answer)
            logging.info(f"[Quiz Solver] Solved hint '{problem}' -> {answer}. Sending {final_answer}")
            reply_to_room(room_id, str(final_answer))
        return

    question_id_match = re.search(r'(?:Question\s*#|#)(\d+)', text)
    if question_id_match:
        question_id = int(question_id_match.group(1))
        
        if bot_state.processed_question_ids.get(room_id) == question_id:
            return
        
        bot_state.processed_question_ids[room_id] = question_id
        logging.info(f"[Quiz Solver] New question locked (ID: {question_id}) in room {room_id}.")

        problem_match = re.search(r'\*\s*(?:M[аa]ths|Maths)\s*-\s*(.*?)\s*\*', text, re.DOTALL)
        if not problem_match:
            logging.error(f"[Quiz Solver] Could not extract problem from text: {text}")
            return

        problem = problem_match.group(1).strip()
        
        if is_simple_equation(problem):
            answer = solve_math_problem(problem)
            if answer is not None:
                logging.info(f"[Quiz Solver] Instantly solved '{problem}' -> {answer}")
                reply_to_room(room_id, str(answer))
            else:
                logging.info(f"[Quiz Solver] Failed to instantly solve '{problem}', requesting hint.")
                reply_to_room(room_id, ".h")
        else:
            logging.info(f"[Quiz Solver] Sequence puzzle detected '{problem}', requesting hint.")
            reply_to_room(room_id, ".h")

# ========================================================================================
# === WEBSOCKET HANDLERS & MAIN BLOCK =====================================================
# ========================================================================================
def on_open(ws):
    logging.info("🚀 WebSocket connection opened. Logging in...")
    bot_state.ws_instance = ws
    bot_state.is_connected = True
    bot_state.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
    send_ws_message({"handler": "login", "username": Config.BOT_USERNAME, "password": Config.BOT_PASSWORD, "token": bot_state.token})

def on_message(ws, message_str):
    if '"handler":"ping"' in message_str: return
    logging.info(f"<-- RECEIVED: {message_str[:500]}")
    try:
        data = json.loads(message_str)
        handler = data.get("handler")
        if handler == "login" and data.get("status") == "success":
            bot_state.bot_user_id = data.get('userID')
            db_manager.get_or_create_user(bot_state.bot_user_id, Config.BOT_USERNAME)
            logging.info(f"✅ Login successful! Bot ID: {bot_state.bot_user_id}.")
            threading.Thread(target=join_startup_rooms, daemon=True).start()
        elif handler == "joinchatroom" and data.get("error") == 0:
            room_id, room_name = data.get('roomid'), data.get('name')
            bot_state.room_id_to_name[room_id] = room_name; bot_state.room_name_to_id[room_name.lower()] = room_id
            logging.info(f"✅ Joined room: '{room_name}' (ID: {room_id})")
        elif handler == "userkicked" and data.get("userid") == bot_state.bot_user_id:
            room_id = data.get('roomid')
            if room_id in bot_state.quiz_solvers: del bot_state.quiz_solvers[room_id]
            if room_id in bot_state.processed_question_ids: del bot_state.processed_question_ids[room_id]
            rejoin_room_name = None
            if room_name := bot_state.room_id_to_name.pop(room_id, None):
                bot_state.room_name_to_id.pop(room_name.lower(), None)
                rejoin_room_name = room_name
            
            startup_rooms = [name.strip().lower() for name in Config.ROOMS_TO_JOIN.split(',')]
            if rejoin_room_name and rejoin_room_name.lower() in startup_rooms:
                logging.warning(f"⚠️ Kicked from startup room '{rejoin_room_name}'. Rejoining in {Config.REJOIN_ON_KICK_DELAY_SECONDS}s...")
                time.sleep(Config.REJOIN_ON_KICK_DELAY_SECONDS)
                join_room(rejoin_room_name, source='startup_join')
            else:
                logging.warning(f"⚠️ Kicked from '{rejoin_room_name}'. Not a startup room, will not rejoin.")

        elif handler == "chatroommessage":
            user_id = data.get('userid')
            if user_id == bot_state.bot_user_id: return
            room_id = data.get('roomid')
            text = data.get('text', '').strip()
            username = data.get('username')
            if text.startswith('!'):
                sender = {'id': user_id, 'name': username}
                threading.Thread(target=process_command, args=(sender, room_id, text), daemon=True).start()
            if room_id in bot_state.quiz_solvers:
                threading.Thread(target=process_quiz_message, args=(room_id, text, username), daemon=True).start()
        elif handler == "profile":
            username_from_profile = data.get('Username', '').lower()
            if callbacks_to_run := bot_state.user_profile_callbacks.pop(username_from_profile, []):
                for callback, kwargs in callbacks_to_run: callback(data, **kwargs)
    except (json.JSONDecodeError, Exception) as e: logging.error(f"An error occurred in on_message: {e}", exc_info=True)

def on_error(ws, error): logging.error(f"--- WebSocket Error: {error} ---")
def on_close(ws, close_status_code, close_msg):
    bot_state.is_connected = False
    bot_state.quiz_solvers.clear()
    bot_state.processed_question_ids.clear()
    
    if bot_state.stop_bot_event.is_set():
        logging.info("--- Bot gracefully stopped by web panel. ---")
    else:
        logging.warning(f"--- WebSocket closed unexpectedly. Reconnecting in {bot_state.reconnect_delay}s... ---")
        if not bot_state.stop_bot_event.is_set():
          time.sleep(bot_state.reconnect_delay)
          bot_state.reconnect_delay = min(bot_state.reconnect_delay * 2, Config.MAX_RECONNECT_DELAY)
          start_bot_logic()

def connect_to_howdies():
    bot_state.token = get_token()
    if not bot_state.token or bot_state.stop_bot_event.is_set():
        logging.error("Could not get token or stop event was set. Bot will not connect.")
        bot_state.is_connected = False
        return
    
    ws_url = f"{Config.WS_URL}?token={bot_state.token}"
    ws_app = websocket.WebSocketApp(ws_url, header=Config.BROWSER_HEADERS, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    bot_state.ws_instance = ws_app
    
    while not bot_state.stop_bot_event.is_set():
        ws_app.run_forever()
        if bot_state.stop_bot_event.is_set():
            break
        logging.info("WebSocket connection ended. Will try to reconnect if not stopped.")
        time.sleep(bot_state.reconnect_delay)
        
    bot_state.is_connected = False
    bot_state.ws_instance = None
    logging.info("Bot's run_forever loop has ended.")

# ========================================================================================
# === MAIN EXECUTION BLOCK ===============================================================
# ========================================================================================
if __name__ == "__main__":
    setup_logging()
    db_manager.run_migrations()
    load_config()
    logging.info(f"--- Starting Web Panel for {Config.BOT_USERNAME} ---")
    logging.info(f"Open your browser to http://127.0.0.1:5000 to control the bot.")
    
    # Use 'app' for PythonAnywhere, which is defined in the wsgi.py file as 'application'
    # For local testing, we can still run it directly.
    # On PythonAnywhere, this block is not executed directly.
    # The WSGI server (like Gunicorn) imports the 'app' object.
    app.run(host='0.0.0.0', port=5000)