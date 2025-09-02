import os, time, base64, threading
from time import monotonic, sleep
from pathlib import Path

import cv2
import numpy as np
import requests
from flask import Flask, send_from_directory, request, Response
from flask_socketio import SocketIO, emit
from loguru import logger
from dotenv import load_dotenv

# -------------------- Paths / Flask --------------------
BASE_DIR   = Path(__file__).resolve().parent
FRONT_DIR  = BASE_DIR.parent / "frontend"
ASSETS_DIR = FRONT_DIR / "assets"
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder=str(FRONT_DIR), static_url_path="")
app.config["SECRET_KEY"] = "rashid_kiosk_2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)

@app.route("/")
def index():
    return send_from_directory(str(FRONT_DIR), "index.html")

@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(str(ASSETS_DIR), filename)

@app.route("/favicon.ico")
def favicon():
    fav = ASSETS_DIR / "favicon.ico"
    if fav.exists():
        return send_from_directory(str(ASSETS_DIR), "favicon.ico")
    return ("", 204)

# -------------------- Azure token (optional) --------------------
AZURE_KEY    = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_REGION = os.getenv("AZURE_SPEECH_REGION", "qatarcentral")

@app.route("/azure/token")
def azure_token():
    if not AZURE_KEY:
        return Response("AZURE_SPEECH_KEY missing", status=500)
    url = f"https://{AZURE_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
    try:
        r = requests.post(url, headers={"Ocp-Apim-Subscription-Key": AZURE_KEY}, timeout=8)
        if r.ok and r.text:
            return r.text
        return Response(f"token http {r.status_code}: {r.text}", status=500)
    except Exception as e:
        return Response(f"token error: {e}", status=500)

# -------------------- Avatar (silent loop) --------------------
video_frames = {"silent": []}
default_frame = None
frame_idx = 0
SILENT_MP4   = ASSETS_DIR / "rashid_silent.mp4"
FALLBACK_PNG = ASSETS_DIR / "avatar.png"

def _create_default_frame():
    global default_frame
    if FALLBACK_PNG.exists():
        img = cv2.imread(str(FALLBACK_PNG))
        if img is not None:
            default_frame = cv2.resize(img, (1280, 720)); return
    default_frame = np.zeros((720,1280,3), dtype=np.uint8)
    cv2.putText(default_frame, "Rashid Assistant", (380,370),
                cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255,255,255), 3)

def _load_video(path: Path):
    if not path.exists(): return
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened(): return
    while True:
        ok, frame = cap.read()
        if not ok: break
        video_frames["silent"].append(cv2.resize(frame, (1280,720)))
    cap.release()

def load_video_frames():
    try:
        _load_video(SILENT_MP4)
        if not video_frames["silent"]:
            _create_default_frame()
    except Exception:
        _create_default_frame()

def get_current_frame():
    global frame_idx
    try:
        f = video_frames["silent"][frame_idx % len(video_frames["silent"])] if video_frames["silent"] else default_frame
        frame_idx += 1
        return f
    except Exception:
        return default_frame

def frame_to_b64(frame):
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    return base64.b64encode(buf).decode("utf-8") if ok else None

def video_loop():
    while True:
        b64 = frame_to_b64(get_current_frame())
        if b64:
            socketio.emit("video_frame", {"frame": b64})
        sleep(1/30)

# -------------------- Per-client Conversation State --------------------
# We strictly gate speech & presence so nothing overlaps.
CLIENT = {}  # sid -> {...}

def _get_state(sid: str):
    st = CLIENT.get(sid)
    if st is None:
        st = CLIENT[sid] = {
            "present": False,
            "speaking": False,        # server-side lock (set in _say_to + tts_start/tts_end)
            "last_reply_ts": 0.0,     # time.monotonic()
            "greeted": False,
            "last_greet_ts": 0.0,
            "last_farewell_ts": 0.0,
            # STT dedupe:
            "last_user_text": "",
            "last_user_ts": 0.0,
        }
    return st

ENTER_DELAY_S      = float(os.getenv("ENTER_DELAY_S", 0.7))
LEAVE_DELAY_S      = float(os.getenv("LEAVE_DELAY_S", 1.8))
SERVER_MIN_AREA    = float(os.getenv("SERVER_MIN_AREA", 0.006))   # 0.6%
GREET_COOLDOWN     = 8.0
BYE_COOLDOWN       = 8.0
POST_SPEECH_GRACE  = 1.5  # seconds after TTS before considering farewell
STT_DEDUPE_WINDOW  = 2.0  # ignore identical final results within 2s
SPEAK_WATCHDOG_PAD = 1.0  # add to TTS duration in case tts_end is missed

def _now(): return monotonic()

def _normalize(t: str) -> str:
    return " ".join((t or "").strip().lower().split())

def _say_to(sid: str, text: str, seconds: float = 3.0):
    """
    Speak exactly one thing at a time:
    - Set a server-side 'speaking' lock immediately.
    - Emit voice_response (client will do TTS and also send tts_start/tts_end).
    - Watchdog releases the lock even if client never sends tts_end.
    """
    st = _get_state(sid)
    if st["speaking"]:
        logger.debug("drop speak (busy)")
        return

    st["speaking"] = True
    st["last_reply_ts"] = _now()
    # Let clients optionally update UI immediately
    socketio.emit("speak_state", {"speaking": True}, to=sid)

    socketio.emit("voice_response", {"text": text}, to=sid)
    socketio.emit("server_response", {"data": text}, to=sid)

    def _watchdog():
        # If client never calls tts_end, release lock after seconds + pad
        time.sleep(seconds + SPEAK_WATCHDOG_PAD)
        st2 = _get_state(sid)
        if st2["speaking"]:
            st2["speaking"] = False
            st2["last_reply_ts"] = _now()
            socketio.emit("speak_state", {"speaking": False}, to=sid)

    threading.Thread(target=_watchdog, daemon=True).start()

# -------------------- Presence debounced + gated --------------------
STATE  = {}  # sid -> {"last_eval": False, "since": monotonic(), "n": 0}

def _update_presence_for_sid(sid: str, want_present: bool):
    now = _now()
    st  = _get_state(sid)
    ds  = STATE.setdefault(sid, {"last_eval": False, "since": now, "n": 0})

    # Debounce raw detector
    if want_present != ds["last_eval"]:
        ds["last_eval"] = want_present
        ds["since"] = now
        return

    dwell = now - ds["since"]

    # Handle PRESENT transition
    if want_present and not st["present"] and dwell >= ENTER_DELAY_S:
        st["present"] = True
        socketio.emit("presence", {"present": True}, to=sid)

        # Greet only if not speaking and cooldown passed
        if (not st["speaking"]) and (now - st["last_greet_ts"] > GREET_COOLDOWN):
            _say_to(sid, "حَيَّاكَ اللَّه! أَنَا رَاشِد، كيف أقدر أخدمك؟", seconds=3.0)
            st["greeted"] = True
            st["last_greet_ts"] = now

    # Handle ABSENT transition
    if (not want_present) and st["present"] and dwell >= LEAVE_DELAY_S:
        # Avoid farewell while we're speaking or right after
        if st["speaking"] or (now - st["last_reply_ts"] < POST_SPEECH_GRACE):
            return
        st["present"] = False
        socketio.emit("presence", {"present": False}, to=sid)

        if (now - st["last_farewell_ts"] > BYE_COOLDOWN) and st.get("greeted", False):
            _say_to(sid, "تَشَرَّفْتُ بِخِدْمَتِكَ، مَعَ السَّلَامَة!", seconds=2.0)
            st["last_farewell_ts"] = now
            st["greeted"] = False

# -------------------- Haar cascades (robust lookup) --------------------
def _haar_path(filename: str) -> str:
    for d in ("/usr/share/opencv4/haarcascades", "/usr/share/opencv/haarcascades"):
        p = Path(d, filename)
        if p.exists(): return str(p)
    base = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if base and Path(base, filename).exists():
        return str(Path(base, filename))
    local = BASE_DIR / "haarcascades" / filename
    if local.exists(): return str(local)
    raise FileNotFoundError(f"Missing {filename}. Install opencv-data or place it in backend/haarcascades/")

FRONTAL = cv2.CascadeClassifier(_haar_path("haarcascade_frontalface_default.xml"))
PROFILE = cv2.CascadeClassifier(_haar_path("haarcascade_profileface.xml"))

def _detect_presence(gray, do_rotate=False):
    g = cv2.equalizeHist(gray)
    faces_f = FRONTAL.detectMultiScale(g, 1.1, 3, minSize=(24, 24))
    faces_p = PROFILE.detectMultiScale(g, 1.1, 3, minSize=(24, 24))
    faces = list(faces_f) + list(faces_p)
    if faces:
        h, w = g.shape[:2]; area = float(w*h)
        for (_,_,fw,fh) in faces:
            if (fw*fh)/area >= SERVER_MIN_AREA:
                return True
    if not do_rotate:
        return False
    g90 = cv2.rotate(g, cv2.ROTATE_90_CLOCKWISE)
    if len(FRONTAL.detectMultiScale(g90, 1.1, 3, minSize=(24, 24))) > 0: return True
    if len(PROFILE.detectMultiScale(g90, 1.1, 3, minSize=(24, 24))) > 0: return True
    return False

# -------------------- FAST binary frame handler --------------------
# Lighter OpenCV settings for Pi:
try:
    cv2.setNumThreads(1)
except Exception:
    pass
try:
    cv2.setUseOptimized(True)
except Exception:
    pass

PROCESS_EVERY_N = int(os.getenv("PROCESS_EVERY_N", 2))  # process 1 in N frames

@socketio.on("client_frame_bin")
def on_client_frame_bin(raw: bytes, meta=None):
    try:
        sid = request.sid
        ds  = STATE.setdefault(sid, {"last_eval": False, "since": monotonic(), "n": 0})
        ds["n"] = ds.get("n", 0) + 1

        # Skip some frames to reduce CPU
        if (ds["n"] % PROCESS_EVERY_N) != 0:
            return

        # Decode directly to GRAYSCALE (saves a cvtColor)
        arr  = np.frombuffer(raw, dtype=np.uint8)
        gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            return

        # Defensive downscale (client already sends ~224w)
        h, w = gray.shape[:2]
        if w > 224:
            s = 224.0 / w
            gray = cv2.resize(gray, (224, int(h*s)), interpolation=cv2.INTER_AREA)

        want_rotate = (ds["n"] % (3*PROCESS_EVERY_N) == 0)  # occasional rotate
        has_face = _detect_presence(gray, do_rotate=want_rotate)
        _update_presence_for_sid(sid, has_face)
    except Exception:
        logger.exception("client_frame_bin failed")

# -------------------- LLM (safe wrapper, no echo) --------------------
def _offline_reply(user_text: str) -> str:
    low = _normalize(user_text)
    if not low:
        return "ما سمعت شيئًا. تقدر تعيد كلامك؟"
    if any(g in low for g in ("سلام", "السلام", "hi", "hello")):
        return "وعليكم السلام! كيف أقدر أخدمك؟"
    if any(x in low for x in ("شكرا", "thx", "thanks")):
        return "عفوًا! هل تحتاج أي شيء آخر؟"
    return "أكيد! اخبرني أكثر عن طلبك عشان أساعدك."

try:
    from llm import smart_answer as _llm_smart_answer
except Exception:
    _llm_smart_answer = None

def safe_smart_answer(user_text: str) -> str:
    try:
        if _llm_smart_answer is not None:
            out = _llm_smart_answer(user_text) or {}
            txt = out.get("text")
            if isinstance(txt, str) and txt.strip():
                return txt
    except Exception:
        logger.exception("smart_answer failed")
    return _offline_reply(user_text)

# -------------------- Text / Voice handlers (strict gating + dedupe) --------------------
@socketio.on("voice_input")
def on_voice_input(data):
    sid = request.sid
    st  = _get_state(sid)
    try:
        # Ignore user input while TTS is active
        if st["speaking"]:
            logger.debug("voice_input ignored (speaking)")
            return

        user_text = (data or {}).get("text", "").strip()
        if not user_text:
            return

        # Dedupe final results that Azure sometimes repeats
        now = _now()
        norm = _normalize(user_text)
        if norm and norm == st["last_user_text"] and (now - st["last_user_ts"]) < STT_DEDUPE_WINDOW:
            logger.debug("voice_input duplicate ignored")
            return
        st["last_user_text"] = norm
        st["last_user_ts"] = now

        reply = safe_smart_answer(user_text)
        _say_to(sid, reply, seconds=3.0)
    except Exception:
        logger.exception("voice_input failed")
        _say_to(request.sid, _offline_reply(""), seconds=2.0)

@socketio.on('user_text')
def on_user_text(data):
    sid = request.sid
    st  = _get_state(sid)
    try:
        if st["speaking"]:
            logger.debug("user_text ignored (speaking)")
            return
        txt = (data or {}).get("text", "").strip()
        if not txt: return
        reply = safe_smart_answer(txt)
        _say_to(sid, reply, seconds=3.0)
    except Exception:
        logger.exception("user_text failed")
        _say_to(sid, _offline_reply(""), seconds=2.0)

# -------------------- Socket basics & TTS sync --------------------
@socketio.on('connect')
def on_connect():
    emit('connection_status', {'status': 'connected'})

@socketio.on("disconnect")
def on_disconnect():
    try:
        CLIENT.pop(request.sid, None)
        STATE.pop(request.sid, None)
    except Exception:
        pass

@socketio.on('start_stream')
def on_start_stream():
    b64 = frame_to_b64(get_current_frame())
    if b64: emit('video_frame', {'frame': b64})

@socketio.on("tts_start")
def _tts_start():
    st = _get_state(request.sid)
    st["speaking"] = True
    socketio.emit("speak_state", {"speaking": True}, to=request.sid)

@socketio.on("tts_end")
def _tts_end():
    st = _get_state(request.sid)
    st["speaking"] = False
    st["last_reply_ts"] = _now()
    socketio.emit("speak_state", {"speaking": False}, to=request.sid)

# -------------------- Run --------------------
if __name__ == "__main__":
    # Lighten OpenCV for Pi (also done above, but safe here too)
    try: cv2.setNumThreads(1)
    except Exception: pass
    try: cv2.setUseOptimized(True)
    except Exception: pass

    load_video_frames()
    threading.Thread(target=video_loop, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
