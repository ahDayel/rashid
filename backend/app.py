# app.py
import os, base64, time, threading
from time import monotonic, sleep
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, request
from flask_socketio import SocketIO, emit
from loguru import logger
from dotenv import load_dotenv

# ===== تحميل المفاتيح والبيئة =====
BASE_DIR = Path(__file__).resolve().parent
FRONT_DIR = BASE_DIR.parent / "frontend"
ASSETS_DIR = FRONT_DIR / "assets"

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = "rashid_kiosk_2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_timeout=60, ping_interval=25)

# ===== حالة الجلسة =====
face_present = False          # هل في شخص قدام الكاميرا
engaged = False               # قلنا له الترحيب ولا بعد
is_speaking = False           # راشد يتكلم الآن؟
last_reply_time = 0.0         # حاجز الإيكو

# NEW: مخزن الجلسات لكل عميل
SESSIONS = {}  # key = request.sid -> {"history": [...], "slots": {...}, "pending_question": None}

# ===== الفيديو (أفاتار) =====
video_frames = {"silent": [], "speaking": [], "listening" : [] }
default_frame = None
frame_idx = 0

SILENT_MP4   = ASSETS_DIR / "rashid_silent.mp4"
SPEAKING_MP4 = ASSETS_DIR / "rashid_speaking.mp4"
LISTENING_MP4 = ASSETS_DIR / "rashid_listening.mp4"
FALLBACK_PNG = ASSETS_DIR / "avatar.png"


def _create_default_frame():
    global default_frame
    # صورة سوداء مع كتابة، ولو فيه avatar.png نستخدمه
    if FALLBACK_PNG.exists():
        img = cv2.imread(str(FALLBACK_PNG))
        if img is not None:
            default_frame = cv2.resize(img, (1280, 720))
            return
    default_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.putText(default_frame, "Rashid Assistant", (380, 370),
                cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 3)


def _load_video(path: Path, key: str):
    if not path.exists():
        return
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.resize(frame, (1280, 720))
        video_frames[key].append(frame)
    cap.release()
    logger.info(f"✅ {key} frames: {len(video_frames[key])}")


def load_video_frames():
    logger.info("🎬 Loading avatar frames...")
    try:
        _load_video(SILENT_MP4, "silent")
        _load_video(SPEAKING_MP4, "speaking")
        if not video_frames["silent"] and not video_frames["speaking"]:
            _create_default_frame()
    except Exception as e:
        logger.error(f"❌ load_video_frames: {e}")
        _create_default_frame()


def get_current_frame():
    global frame_idx
    try:
        if is_speaking and video_frames["speaking"]:
            f = video_frames["speaking"][frame_idx % len(video_frames["speaking"])]
        elif video_frames["silent"]:
            f = video_frames["silent"][frame_idx % len(video_frames["silent"])]
        else:
            f = default_frame
        frame_idx += 1
        return f
    except Exception as e:
        logger.error(f"❌ get_current_frame: {e}")
        return default_frame


def frame_to_b64(frame):
    try:
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf).decode("utf-8")
    except Exception as e:
        logger.error(f"❌ frame_to_b64: {e}")
        return None


def video_loop():
    while True:
        try:
            bgr = get_current_frame()
            b64 = frame_to_b64(bgr)
            if b64:
                socketio.emit("video_frame", {"frame": b64})
            sleep(1/30)
        except Exception as e:
            logger.error(f"❌ video_loop: {e}")
            sleep(0.5)

# ===== التحكم بحالة الكلام لمنع الإيكو =====
def set_speaking(flag: bool):
    global is_speaking, last_reply_time
    is_speaking = bool(flag)
    socketio.emit("speak_state", {"speaking": is_speaking})
    if is_speaking:
        last_reply_time = monotonic()


def say(text: str):
    """
    يرسل ردّ نصي وصوتي + يضبط speaking True لفترة قصيرة حتى ما يسمع نفسه.
    """
    set_speaking(True)
    socketio.emit("server_response", {"data": text})
    socketio.emit("voice_response", {"text": text})

    # خفّض speaking بعد شوي (يُعطي وقت للمتصفح يوقف الاستماع)
    def _reset():
        sleep(3.5)
        set_speaking(False)
    threading.Thread(target=_reset, daemon=True).start()

# ===== LLM =====
from llm import smart_answer  # يُرجع dict فيه {text, mode, sources}

# ===== Socket.IO =====
@socketio.on('connect')
def on_connect():
    logger.info("🔌 Client connected")
    emit('connection_status', {'status': 'connected'})


@socketio.on("disconnect")
def on_disconnect():
    logger.info("🔌 Client disconnected")

@socketio.on('start_stream')
def on_start_stream():
    frame = get_current_frame()
    b64 = frame_to_b64(frame)
    if b64:
        emit('video_frame', {'frame': b64})

@socketio.on('voice_input')
def on_voice_input(data):
    user_text = (data or {}).get("text", "").strip()
    if not user_text:
        return
    handle_user_text(user_text)

@socketio.on('user_text')
def on_user_text(data):
    text = (data or {}).get("text","").strip()
    if not text:
        return
    logger.info(f"🎤 User: {text}")
    try:
        out = smart_answer(text)          # out dict
        reply_text = out.get("text","")
    except Exception:
        logger.exception("smart_answer failed")
        reply_text = "صارت مشكلة بسيطة في المساعد. جرّبي إعادة السؤال."

    # 🔊 أعلن أن البوت يتكلم
    socketio.emit("speak_state", {"speaking": True})
    socketio.emit("voice_response", {"text": reply_text})
    socketio.emit("server_response", {"data": reply_text})

    def reset():
        global is_speaking
        time.sleep(3)
        is_speaking = False
        # ⏹️ بعد ما يخلص أرجع الحالة
        socketio.emit("speak_state", {"speaking": False})
    threading.Thread(target=reset, daemon=True).start()



# ===== ترحيب/توديع بالحضور =====
def greet_on_arrival():
    # ترحيب ذكي + قفل الاستماع في الواجهة (speaking=True)
    reply = " حَيَّاكَ اللَّه! أَنَا رَاشِد، كيف اقدر اخدمك؟"
    socketio.emit("speak_state", {"speaking": True})
    socketio.emit('voice_response', {'text': reply})
    socketio.emit('server_response', {'data': reply})
    # افتح الميك بعد ما يخلص النطق
    threading.Thread(target=_end_speaking_after, args=(3.0,), daemon=True).start()

def farewell_on_leave():
    reply = "تَشَرَّفْتُ بِخِدْمَتِكَ، مَعَ السَّلَامَة!"
    socketio.emit("speak_state", {"speaking": True})
    socketio.emit('voice_response', {'text': reply})
    socketio.emit('server_response', {'data': reply})
    threading.Thread(target=_end_speaking_after, args=(2.0,), daemon=True).start()

def _end_speaking_after(sec: float):
    time.sleep(sec)
    socketio.emit("speak_state", {"speaking": False})

# ========= الرد الذكي =========
def handle_user_text(user_text: str):
    logger.info(f"🎤 User: {user_text}")
    try:
        out = smart_answer(user_text)   # {'text': ..., ...}
        reply_text = out.get("text", "")
    except Exception:
        logger.exception("smart_answer failed")
        reply_text = "صارت مشكلة بسيطة في المساعد. جرّبي إعادة السؤال."

    # 🔇 اقفل الاستماع قبل ما نتكلم
    socketio.emit("speak_state", {"speaking": True})
    socketio.emit('voice_response', {'text': reply_text})
    socketio.emit('server_response', {'data': reply_text})
    # 🔊 افتح الاستماع بعد ما يخلص الصوت
    threading.Thread(target=_end_speaking_after, args=(4.0,), daemon=True).start()


# ===== كاشف الوجه (MediaPipe) =====
def face_presence_watcher(cam_index=0, greet_delay_s=2.0, farewell_delay_s=5.0,
                          min_conf=0.6, min_area=0.04):
    """
    يراقب الكاميرا ويُفعّل الترحيب بعد ثبات وجود الوجه لمدة greet_delay_s،
    ويُفعّل التوديع بعد اختفاء الوجه لمدة farewell_delay_s.
    """
    global face_present
    try:
        import mediapipe as mp
    except Exception as e:
        logger.error(f"❌ mediapipe not available: {e}")
        return

    cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        logger.error("❌ لا أستطيع فتح الكاميرا")
        return

    mp_fd = mp.solutions.face_detection

    # طوابع زمنية لضبط التأخير
    first_seen_ts = None         # أول لحظة بدأ فيها الوجه يظهر بشكل متواصل
    last_seen_ts = None          # آخر لحظة كان فيها الوجه ظاهر
    greeted = False              # هل قد طُلب الترحيب لهذه الجلسة؟

    with mp_fd.FaceDetection(model_selection=0, min_detection_confidence=min_conf) as fd:
        while True:
            ok, frame = cap.read()
            if not ok:
                sleep(0.03)
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = fd.process(rgb)

            # تحقّق وجود وجه بمساحة كافية
            has_face = False
            if res and res.detections:
                for det in res.detections:
                    bb = det.location_data.relative_bounding_box
                    if max(bb.width, 0) * max(bb.height, 0) >= min_area:
                        has_face = True
                        break

            now = monotonic()

            if has_face:
                last_seen_ts = now
                if first_seen_ts is None:
                    first_seen_ts = now

                # إذا لم نُرحّب بعد، وانتظم الوجود لمدة كافية -> رحّب
                if not greeted and (now - first_seen_ts) >= greet_delay_s:
                    face_present = True
                    greeted = True
                    socketio.emit("presence", {"present": True})
                    logger.info("🟢 Face present (stable) -> greet")
                    greet_on_arrival()
            else:
                # لا يوجد وجه: صفّر بداية الظهور، وراقب زمن الغياب
                first_seen_ts = None
                if last_seen_ts is not None and greeted:
                    if (now - last_seen_ts) >= farewell_delay_s:
                        # غياب مستقر بما يكفي -> وداع
                        face_present = False
                        greeted = False
                        last_seen_ts = None
                        socketio.emit("presence", {"present": False})
                        logger.info("⚪ Face absent (stable) -> farewell")
                        farewell_on_leave()

            # سليب خفيف لتقليل الحمل (لا يعتمد على الزمن كمعيار)
            sleep(0.03)


# ===== التشغيل =====
if __name__ == "__main__":
    logger.info("🚀 Starting Rashid Kiosk...")
    load_video_frames()
    threading.Thread(target=video_loop, daemon=True).start()
    threading.Thread(
    target=face_presence_watcher,
    args=(0, 2.0, 5.0),  # greet_delay_s=2s, farewell_delay_s=5s
    daemon=True
    ).start()
    logger.info("🌐 Open frontend: frontend/index.html")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)







@socketio.on("tts_start")
def _tts_start():
    set_speaking(True)

@socketio.on("tts_end")
def _tts_end():
    set_speaking(False)