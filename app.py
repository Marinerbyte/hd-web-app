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
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for, request, session, flash

load_dotenv()

# ========================================================================================
# === 2. LOGGING SETUP (MODIFIED FOR PYTHONANYWHERE/RENDER) ==============================
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
    LOGIN_URL = "https://api.howdies.app/api/login"
    WS_URL = "wss://app.howdies.app/"
    ROOM_JOIN_DELAY_SECONDS = 2
    REJOIN_ON_KICK_DELAY_SECONDS = 3
    INITIAL_RECONNECT_DELAY = 10
    MAX_RECONNECT_DELAY = 300
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
        self.masters = [] # Masters will be loaded from a simple config file
        self.room_id_to_name = {}
        self.room_name_to_id = {}
        self.reconnect_delay = Config.INITIAL_RECONNECT_DELAY
        self.quiz_solvers = {}
        self.processed_question_ids = {}
        self.stop_bot_event = threading.Event()

bot_state = BotState()
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
# === CORE BOT UTILITIES =================================================================
# ========================================================================================
def load_masters():
    # Simple master list, you can add more names here
    bot_state.masters = ["yasin"]
    logging.info(f"✅ Loaded {len(bot_state.masters)} masters.")

def send_ws_message(payload):
    if bot_state.is_connected and bot_state.ws_instance:
        try:
            if payload.get("handler") not in ["ping", "pong"]: logging.info(f"--> SENDING: {json.dumps(payload)}")
            bot_state.ws_instance.send(json.dumps(payload))
        except Exception as e: logging.error(f"Error sending message: {e}")
    else: logging.warning("Warning: WebSocket is not connected.")

def reply_to_room(room_id, text):
    send_ws_message({"handler": "chatroommessage", "type": "text", "roomid": room_id, "text": text})

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

# ========================================================================================
# === COMMAND HANDLERS ===================================================================
# ========================================================================================
def handle_help(room_id):
    help_text = (
        "🤖 **ArcadeBot Help Menu** 🤖\n"
        "-----------------------------------\n"
        "**General:** `!j <room>`\n"
        "**Master-Only:** `!quiz on <bot>`, `!quiz off`"
    )
    reply_to_room(room_id, help_text)

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
    try: parts = shlex.split(message_text.strip())
    except ValueError: parts = message_text.strip().split()
    command, args = parts[0][1:].lower(), parts[1:]

    is_master = sender['name'].lower() in bot_state.masters

    if command == 'help': handle_help(room_id)
    elif command == 'j':
        if args:
            join_room(" ".join(args))
        else:
            reply_to_room(room_id, "Usage: `!j <room>`")
    elif is_master:
        if command == 'quiz': handle_quiz_command(args[0] if args else '', args[1:], room_id)

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
    load_masters()
    logging.info(f"--- Starting Web Panel for {Config.BOT_USERNAME} ---")
    logging.info(f"Open your browser to http://127.0.0.1:5000 to control the bot.")
    app.run(host='0.0.0.0', port=5000)