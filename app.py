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
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for, request, session, flash

load_dotenv()

# ========================================================================================
# === 2. LOGGING SETUP (MODIFIED FOR RENDER) =============================================
# ========================================================================================
def setup_logging():
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
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
    
    CYCLE_WORK_MIN_SECONDS = 900
    CYCLE_WORK_MAX_SECONDS = 1800
    CYCLE_BREAK_MIN_SECONDS = 20
    CYCLE_BREAK_MAX_SECONDS = 120
    
    CYCLE_STOP_COMMANDS = ['.stop', '.q 0', '.pause']
    CYCLE_START_COMMANDS = ['.start', '.q 1', '.play']

    # <--- MODIFICATION: Spin Roamer Configuration --->
    ROAMER_INTERVAL_MIN_SECONDS = 15 * 60  # 15 minutes
    ROAMER_INTERVAL_MAX_SECONDS = 20 * 60  # 20 minutes
    ROAMER_PAUSE_SECONDS = 4
    ROAMER_VISITED_EXPIRY_SECONDS = 24 * 60 * 60
    SPIN_COMMAND = ".s"
    # <---------------------------------------------->

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
        self.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
        self.quiz_solvers = {}
        self.processed_question_ids = {}
        self.stop_bot_event = threading.Event()
        # Cycle state
        self.cycle_timers = {}
        self.break_end_times = {}
        # <--- MODIFICATION: Roamer State --->
        self.roamer_thread = None
        self.stop_roamer_event = threading.Event()
        self.is_roamer_active = False
        self.roamable_rooms = set()
        self.visited_roam_rooms = {}  # {room_name: timestamp}
        self.roam_lock = threading.Lock()
        # <------------------------------------>

bot_state = BotState()
bot_thread = None

# ========================================================================================
# === SPIN ROAMER LOGIC ==================================================================
# ========================================================================================

def roamer_logic():
    logging.info("[Roamer] Spin Roamer thread started.")
    
    while not bot_state.stop_roamer_event.is_set():
        try:
            # 1. Sleep for the main interval
            interval = random.randint(Config.ROAMER_INTERVAL_MIN_SECONDS, Config.ROAMER_INTERVAL_MAX_SECONDS)
            logging.info(f"[Roamer] Next roam scheduled in {interval/60:.1f} minutes.")
            bot_state.stop_roamer_event.wait(interval)
            if bot_state.stop_roamer_event.is_set(): break

            with bot_state.roam_lock:
                # 2. Clean up old visited rooms
                now = time.time()
                expired_rooms = [name for name, ts in bot_state.visited_roam_rooms.items() if now - ts > Config.ROAMER_VISITED_EXPIRY_SECONDS]
                for name in expired_rooms:
                    del bot_state.visited_roam_rooms[name]
                if expired_rooms:
                    logging.info(f"[Roamer] Cleared {len(expired_rooms)} expired rooms from visited list.")

                # 3. Find a suitable room to visit
                startup_rooms = {name.strip().lower() for name in Config.ROOMS_TO_JOIN.split(',')}
                available_rooms = list(bot_state.roamable_rooms - set(bot_state.visited_roam_rooms.keys()) - startup_rooms)
                
                if not available_rooms:
                    logging.warning("[Roamer] No new rooms available to roam. Waiting for next cycle.")
                    continue
                
                target_room_name = random.choice(available_rooms)

            # 4. Perform the roam-spin-leave sequence
            logging.info(f"[Roamer] Attempting to roam to room: '{target_room_name}'")
            
            # Join room and wait for confirmation
            join_room(target_room_name, source="roamer")
            
            # Wait for room ID to be populated
            roam_room_id = None
            for _ in range(10): # Wait up to 10 seconds
                roam_room_id = bot_state.room_name_to_id.get(target_room_name.lower())
                if roam_room_id: break
                time.sleep(1)
            
            if not roam_room_id:
                logging.error(f"[Roamer] Failed to get room ID for '{target_room_name}' after joining. Aborting this roam.")
                continue

            # Spin, Pause, Leave
            logging.info(f"[Roamer] In '{target_room_name}', sending spin command.")
            reply_to_room(roam_room_id, Config.SPIN_COMMAND)
            time.sleep(Config.ROAMER_PAUSE_SECONDS)
            
            logging.info(f"[Roamer] Leaving room '{target_room_name}'.")
            leave_room(roam_room_id)

            # 5. Mark room as visited
            with bot_state.roam_lock:
                bot_state.visited_roam_rooms[target_room_name] = time.time()
            
            logging.info(f"[Roamer] Successfully roamed to '{target_room_name}'.")

        except Exception as e:
            logging.error(f"[Roamer] An error occurred in the roamer loop: {e}", exc_info=True)
            time.sleep(60) # Wait a minute before retrying on error

    logging.info("[Roamer] Spin Roamer thread has been stopped.")


# ========================================================================================
# === CYCLE MODE LOGIC (Unchanged) =======================================================
# ========================================================================================
def schedule_next_break(room_id):
    if room_id not in bot_state.cycle_timers or bot_state.stop_bot_event.is_set(): return
    start_command = random.choice(Config.CYCLE_START_COMMANDS)
    reply_to_room(room_id, start_command)
    logging.info(f"[Cycle] Break ended. Sending START command '{start_command}' to room '{bot_state.room_id_to_name.get(room_id)}'.")
    work_duration = random.randint(Config.CYCLE_WORK_MIN_SECONDS, Config.CYCLE_WORK_MAX_SECONDS)
    logging.info(f"[Cycle] Room '{bot_state.room_id_to_name.get(room_id)}': Working for {work_duration/60:.1f} minutes. Next break scheduled.")
    bot_state.break_end_times.pop(room_id, None)
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
    logging.info(f"[Cycle] Room '{bot_state.room_id_to_name.get(room_id)}': On break for {break_duration:.1f} seconds.")
    timer = threading.Timer(break_duration, schedule_next_break, args=[room_id])
    bot_state.cycle_timers[room_id] = timer
    timer.start()

def start_cycle_for_room(room_id):
    if room_id in bot_state.cycle_timers: return
    bot_state.cycle_timers[room_id] = None
    schedule_next_break(room_id)
    reply_to_room(room_id, "✅ Cycle mode activated. The bot will now take random breaks and control the quiz bot.")

def stop_cycle_for_room(room_id):
    if room_id in bot_state.cycle_timers:
        timer = bot_state.cycle_timers.pop(room_id)
        if timer: timer.cancel()
    bot_state.break_end_times.pop(room_id, None)
    start_command = random.choice(Config.CYCLE_START_COMMANDS)
    reply_to_room(room_id, start_command)
    logging.info(f"[Cycle] Cycle mode stopped for room '{bot_state.room_id_to_name.get(room_id)}'. Sent final START command '{start_command}'.")
    # reply_to_room(room_id, "✅ Cycle mode deactivated. The bot will now answer all questions.")

# ========================================================================================
# === WEB APP (Omitted for brevity) ======================================================
# ========================================================================================
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Login</title><style>body{font-family:sans-serif;background:#121212;color:#e0e0e0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}.login-box{background:#1e1e1e;padding:40px;border-radius:8px;box-shadow:0 4px 8px rgba(0,0,0,0.3);width:300px;}h2{color:#bb86fc;text-align:center;}.input-group{margin-bottom:20px;}input{width:100%;padding:10px;border:1px solid #333;border-radius:4px;background:#2a2a2a;color:#e0e0e0;box-sizing: border-box;}.btn{width:100%;padding:10px;border:none;border-radius:4px;background:#03dac6;color:#121212;font-size:16px;cursor:pointer;}.flash{padding:10px;background:#cf6679;color:#121212;border-radius:4px;margin-bottom:15px;text-align:center;}</style></head><body><div class="login-box"><h2>Control Panel Login</h2>{% with messages = get_flashed_messages() %}{% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}{% endwith %}<form method="post"><div class="input-group"><input type="text" name="username" placeholder="Username" required></div><div class="input-group"><input type="password" name="password" placeholder="Password" required></div><button type="submit" class="btn">Login</button></form></div></body></html>
"""
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>{{ bot_name }} Dashboard</title><meta http-equiv="refresh" content="10"><style>body{font-family:sans-serif;background:#121212;color:#e0e0e0;margin:0;padding:40px;text-align:center;}.container{max-width:800px;margin:auto;background:#1e1e1e;padding:20px;border-radius:8px;box-shadow:0 4px 8px rgba(0,0,0,0.3);}h1{color:#bb86fc;}.status{padding:15px;border-radius:5px;margin-top:20px;font-weight:bold;}.running{background:#03dac6;color:#121212;}.stopped{background:#cf6679;color:#121212;}.buttons{margin-top:30px;}.btn{padding:12px 24px;border:none;border-radius:5px;font-size:16px;cursor:pointer;margin:5px;text-decoration:none;color:#121212;display:inline-block;}.btn-start{background-color:#03dac6;}.btn-stop{background-color:#cf6679;}.btn-logout{background-color:#666;color:#fff;position:absolute;top:20px;right:20px;}</style></head><body><a href="/logout" class="btn btn-logout">Logout</a><div class="container"><h1>{{ bot_name }} Dashboard</h1><div class="status {{ 'running' if 'Running' in bot_status else 'stopped' }}">Bot Status: {{ bot_status }}</div><div class="buttons"><a href="/start" class="btn btn-start">Start Bot</a><a href="/stop" class="btn btn-stop">Stop Bot</a></div></div></body></html>
"""

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
        if bot_state.is_connected: status = "Running and Connected"
        else: status = "Running but Disconnected"
    return render_template_string(DASHBOARD_TEMPLATE, bot_name=Config.BOT_USERNAME, bot_status=status)
@app.route('/start')
def start_bot_route():
    if (uptime_key := request.args.get('key')) and uptime_key == Config.UPTIME_SECRET_KEY:
        start_bot_logic()
        return "Bot start initiated by uptime service."
    if not session.get('logged_in'): return redirect(url_for('login'))
    start_bot_logic()
    return redirect(url_for('home'))
@app.route('/stop')
def stop_bot_route():
    if not session.get('logged_in'): return redirect(url_for('login'))
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
        # Stop roamer first
        if bot_state.is_roamer_active:
            handle_roamer_command('off', None)
        # Stop main bot event
        bot_state.stop_bot_event.set()
        for room_id in list(bot_state.cycle_timers.keys()): stop_cycle_for_room(room_id)
        if bot_state.ws_instance:
            try: bot_state.ws_instance.close()
            except Exception: pass
        bot_thread.join(timeout=5)
        bot_thread = None

# ========================================================================================
# === CORE BOT UTILITIES & COMMANDS ======================================================
# ========================================================================================
def load_masters():
    masters_str = Config.MASTERS_LIST
    if masters_str: bot_state.masters = [name.strip().lower() for name in masters_str.split(',')]
    logging.info(f"✅ Loaded {len(bot_state.masters)} masters from .env.")

def send_ws_message(payload):
    if bot_state.is_connected and bot_state.ws_instance:
        try:
            if payload.get("text") not in Config.CYCLE_START_COMMANDS and payload.get("text") not in Config.CYCLE_STOP_COMMANDS and payload.get("text") != Config.SPIN_COMMAND:
               if payload.get("handler") not in ["ping", "pong"]: logging.info(f"--> SENDING: {json.dumps(payload)}")
            bot_state.ws_instance.send(json.dumps(payload))
        except Exception as e: logging.error(f"Error sending message: {e}")
    else: logging.warning("Warning: WebSocket is not connected.")

def reply_to_room(room_id, text):
    send_ws_message({"handler": "chatroommessage", "type": "text", "roomid": room_id, "text": text})

# <--- MODIFICATION: New function to leave a room --->
def leave_room(room_id):
    send_ws_message({"handler": "leaveroom", "roomid": room_id})
    # Clean up state for the temporary room
    if room_name := bot_state.room_id_to_name.pop(room_id, None):
        bot_state.room_name_to_id.pop(room_name.lower(), None)
# <--------------------------------------------------->

def send_delayed_quiz_answer(room_id, answer_text):
    if bot_state.stop_bot_event.is_set(): return
    delay_ms = random.randint(Config.QUIZ_ANSWER_DELAY_MIN_MS, Config.QUIZ_ANSWER_DELAY_MAX_MS)
    delay_s = delay_ms / 1000.0
    logging.info(f"[Quiz Solver] Solved. Waiting for {delay_s:.2f} seconds before answering in room {room_id}.")
    time.sleep(delay_s)
    if not bot_state.stop_bot_event.is_set(): reply_to_room(room_id, answer_text)

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

def handle_help(room_id):
    help_text = (
        "🤖 **ArcadeBot Help Menu** 🤖\n"
        "-----------------------------------\n"
        "**General:** `!j <room>`\n"
        "**Master-Only:** `!quiz on <bot>`, `!quiz off`, `!delay [min] [max]`, `!cycle on|off|status`, `!roamer on|off|status`"
    )
    reply_to_room(room_id, help_text)

# <--- MODIFICATION: Command handler for the roamer --->
def handle_roamer_command(sub_command, room_id):
    if sub_command == 'on':
        if bot_state.is_roamer_active:
            if room_id: reply_to_room(room_id, "ℹ️ Spin Roamer is already running.")
        else:
            bot_state.stop_roamer_event.clear()
            bot_state.roamer_thread = threading.Thread(target=roamer_logic, daemon=True)
            bot_state.roamer_thread.start()
            bot_state.is_roamer_active = True
            if room_id: reply_to_room(room_id, "✅ Spin Roamer activated. The bot will now visit other rooms to spin.")
    elif sub_command == 'off':
        if not bot_state.is_roamer_active:
            if room_id: reply_to_room(room_id, "ℹ️ Spin Roamer is not running.")
        else:
            bot_state.stop_roamer_event.set()
            if bot_state.roamer_thread:
                bot_state.roamer_thread.join(timeout=5)
            bot_state.is_roamer_active = False
            bot_state.roamer_thread = None
            if room_id: reply_to_room(room_id, "✅ Spin Roamer deactivated.")
    elif sub_command == 'status':
        if room_id:
            status = "ON" if bot_state.is_roamer_active else "OFF"
            with bot_state.roam_lock:
                roamable_count = len(bot_state.roamable_rooms)
                visited_count = len(bot_state.visited_roam_rooms)
            reply_to_room(room_id, f"✳️ Roamer Status: {status}\n- Known rooms: {roamable_count}\n- Visited in last 24h: {visited_count}")
    else:
        if room_id: reply_to_room(room_id, "Usage: `!roamer on|off|status`")

def handle_quiz_command(sub_command, args, room_id):
    # Unchanged
    if sub_command == 'on':
        if not args: return reply_to_room(room_id, "Usage: `!quiz on <bot_username>`")
        quiz_bot_username = args[0].lower()
        bot_state.quiz_solvers[room_id] = quiz_bot_username
        reply_to_room(room_id, f"✅ Quiz solver enabled. Watching for questions from '{quiz_bot_username}'.")
    elif sub_command == 'off':
        if room_id in bot_state.quiz_solvers:
            del bot_state.quiz_solvers[room_id]
            if room_id in bot_state.processed_question_ids: del bot_state.processed_question_ids[room_id]
            if room_id in bot_state.cycle_timers: stop_cycle_for_room(room_id)
            reply_to_room(room_id, "✅ Quiz solver disabled for this room.")
        else:
            reply_to_room(room_id, "ℹ️ Quiz solver is not active in this room.")
    else:
        reply_to_room(room_id, "Usage: `!quiz on <bot_username>` or `!quiz off`")

def handle_delay_command(args, room_id):
    # Unchanged
    if not args:
        min_delay, max_delay = Config.QUIZ_ANSWER_DELAY_MIN_MS, Config.QUIZ_ANSWER_DELAY_MAX_MS
        reply_to_room(room_id, f"ℹ️ Current answer delay is set from {min_delay}ms to {max_delay}ms.")
        return
    try:
        if len(args) != 2:
            reply_to_room(room_id, "Usage: `!delay <min_milliseconds> <max_milliseconds>`")
            return
        new_min, new_max = int(args[0]), int(args[1])
        if new_min < 0 or new_max < 0: reply_to_room(room_id, "❌ Error: Delay values cannot be negative.")
        elif new_min > new_max: reply_to_room(room_id, "❌ Error: Minimum delay cannot be greater than maximum delay.")
        else:
            Config.QUIZ_ANSWER_DELAY_MIN_MS, Config.QUIZ_ANSWER_DELAY_MAX_MS = new_min, new_max
            logging.info(f"MASTER COMMAND: Quiz delay updated to {new_min}ms - {new_max}ms")
            reply_to_room(room_id, f"✅ Answer delay updated to `{new_min}ms` - `{new_max}ms`.")
    except ValueError: reply_to_room(room_id, "❌ Error: Please provide valid numbers for delay values.")

def handle_cycle_command(sub_command, room_id):
    # Unchanged
    if sub_command == 'on':
        if room_id not in bot_state.quiz_solvers: reply_to_room(room_id, "ℹ️ Please turn the quiz solver on first with `!quiz on <bot>`.")
        elif room_id in bot_state.cycle_timers: reply_to_room(room_id, "ℹ️ Cycle mode is already active in this room.")
        else: start_cycle_for_room(room_id)
    elif sub_command == 'off':
        if room_id not in bot_state.cycle_timers: reply_to_room(room_id, "ℹ️ Cycle mode is not active in this room.")
        else: stop_cycle_for_room(room_id)
    elif sub_command == 'status':
        if room_id in bot_state.cycle_timers:
            if time.time() < bot_state.break_end_times.get(room_id, 0):
                break_ends_in = bot_state.break_end_times[room_id] - time.time()
                reply_to_room(room_id, f"✳️ Cycle mode is ON and the bot is currently on a break for another {break_ends_in:.0f} seconds.")
            else: reply_to_room(room_id, "✳️ Cycle mode is ON and the bot is currently working (answering questions).")
        else: reply_to_room(room_id, "⚪ Cycle mode is OFF.")
    else: reply_to_room(room_id, "Usage: `!cycle on|off|status`")

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
        if command == 'quiz': handle_quiz_command(args[0] if args else '', args[1:], room_id)
        elif command == 'delay': handle_delay_command(args, room_id)
        elif command == 'cycle': handle_cycle_command(args[0] if args else 'status', room_id)
        elif command == 'roamer': handle_roamer_command(args[0] if args else 'status', room_id)

# ========================================================================================
# === QUIZ SOLVER & WEBSOCKETS ===========================================================
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
    if time.time() < bot_state.break_end_times.get(room_id, 0): return
    quiz_bot_username = bot_state.quiz_solvers.get(room_id)
    if not (quiz_bot_username and username.lower() == quiz_bot_username.lower()): return

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
            logging.info(f"[Quiz Solver] Solved hint '{problem}' -> {answer}. Preparing to send {final_answer}")
            threading.Thread(target=send_delayed_quiz_answer, args=(room_id, str(final_answer)), daemon=True).start()
        return

    question_id_match = re.search(r'(?:Question\s*#|#)(\d+)', text)
    if question_id_match:
        question_id = int(question_id_match.group(1))
        if bot_state.processed_question_ids.get(room_id) == question_id: return
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
                threading.Thread(target=send_delayed_quiz_answer, args=(room_id, str(answer)), daemon=True).start()
            else:
                logging.info(f"[Quiz Solver] Failed to instantly solve '{problem}', requesting hint.")
                reply_to_room(room_id, ".h")
        else:
            logging.info(f"[Quiz Solver] Sequence puzzle detected '{problem}', requesting hint.")
            reply_to_room(room_id, ".h")

def on_open(ws):
    logging.info("🚀 WebSocket connection opened. Logging in...")
    bot_state.is_connected = True
    bot_state.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
    send_ws_message({"handler": "login", "username": Config.BOT_USERNAME, "password": Config.BOT_PASSWORD, "token": bot_state.token})

def on_message(ws, message_str):
    if '"handler":"ping"' in message_str: return
    try:
        data = json.loads(message_str)
        handler = data.get("handler")
        if handler == "login" and data.get("status") == "success":
            bot_state.bot_user_id = data.get('userID')
            logging.info(f"✅ Login successful! Bot ID: {bot_state.bot_user_id}.")
            threading.Thread(target=join_startup_rooms, daemon=True).start()
        
        # <--- MODIFICATION: Populate roamable rooms list --->
        elif handler == "chatroomplus" and "data" in data:
            with bot_state.roam_lock:
                initial_count = len(bot_state.roamable_rooms)
                for room in data["data"]:
                    if "name" in room:
                        bot_state.roamable_rooms.add(room["name"])
                new_count = len(bot_state.roamable_rooms)
                if new_count > initial_count:
                    logging.info(f"[Roamer] Cached {new_count - initial_count} new rooms. Total known rooms: {new_count}")
        # <--------------------------------------------------->

        elif handler == "joinchatroom" and data.get("error") == 0:
            room_id, room_name = data.get('roomid'), data.get('name')
            bot_state.room_id_to_name[room_id] = room_name; bot_state.room_name_to_id[room_name.lower()] = room_id
            logging.info(f"✅ Joined room: '{room_name}' (ID: {room_id})")
        
        elif handler == "userkicked" and data.get("userid") == bot_state.bot_user_id:
            room_id = data.get('roomid')
            if room_id in bot_state.quiz_solvers: del bot_state.quiz_solvers[room_id]
            if room_id in bot_state.processed_question_ids: del bot_state.processed_question_ids[room_id]
            if room_id in bot_state.cycle_timers: stop_cycle_for_room(room_id)
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
            if str(user_id) == str(bot_state.bot_user_id): return
            room_id = data.get('roomid')
            text = data.get('text', '').strip()
            username = data.get('username')
            if text.startswith('!'):
                sender = {'id': user_id, 'name': username}
                threading.Thread(target=process_command, args=(sender, room_id, text), daemon=True).start()
            if room_id in bot_state.quiz_solvers:
                threading.Thread(target=process_quiz_message, args=(room_id, text, username), daemon=True).start()
    except (json.JSONDecodeError, Exception) as e: logging.error(f"An error occurred in on_message: {e}", exc_info=True)

def on_error(ws, error): logging.error(f"--- WebSocket Error: {error} ---")
def on_close(ws, close_status_code, close_msg):
    bot_state.is_connected = False
    
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
        if bot_state.stop_bot_event.is_set(): break
        logging.info("WebSocket connection ended. Will try to reconnect if not stopped.")
        time.sleep(bot_state.reconnect_delay)
        
    bot_state.is_connected = False
    bot_state.ws_instance = None
    logging.info("Bot's run_forever loop has ended.")

# ========================================================================================
# === MAIN EXECUTION BLOCK ===============================================================
# ========================================================================================
setup_logging()
load_masters()

if __name__ == "__main__":
    logging.info(f"--- Starting Web Panel for {Config.BOT_USERNAME} ---")
    logging.info(f"Open your browser to http://127.0.0.1:5000 to control the bot.")
    app.run(host='0.0.0.0', port=5000)