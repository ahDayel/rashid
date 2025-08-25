// ===== Socket.IO =====
const socket = io("http://localhost:5000", { transports: ["websocket"] });

const avatarEl = document.getElementById("avatar");
const logEl    = document.getElementById("log");
const micEl    = document.getElementById("micStatus");

function append(kind, text) {
  const box = document.createElement("div");
  box.className = kind; // "bot" | "user" | "sys"
  box.textContent = text;
  logEl.appendChild(box);
  logEl.scrollTop = logEl.scrollHeight;
}
const appendBot  = (t)=>append("bot",  "🤖 " + t);
const appendUser = (t)=>append("user", "👤 " + t);
const appendSys  = (t)=>append("sys",  t);

// ====== حالة عامّة ======
let personPresent   = false;      // من السيرفر (presence)
let isBotSpeaking   = false;      // من السيرفر (speak_state) + TTS callbacks
let rec             = null;       // SpeechRecognition instance
let allowListen     = false;      // إذن الاستماع (وجه موجود + ما فيه كلام)
let bootedASR       = false;
let ttsBusy =         false; // يمنع الاستماع أثناء نطق TTS فعليًا


// ====== فيديو الرموز ======
socket.on("video_frame", ({ frame }) => {
  avatarEl.src = `data:image/jpeg;base64,${frame}`;
});

socket.on("connection_status", ({ status }) => {
  appendBot(`(اتصال بالخادم: ${status})`);
});

// ====== حضور الكاميرا ======
socket.on("presence", ({ present }) => {
  personPresent = !!present;
  if (personPresent) {
    appendBot("تم رصد شخص أمام الكاميرا! 📷");
  } else {
    appendBot("الشخص غادر. 👋");
  }
  updateListeningState();
});

// ====== صوت البوت (TTS) ======
socket.on("voice_response", ({ text }) => {
  speak(text);
});

socket.on("server_response", ({ data }) => {
  appendBot(data);
});

// ====== حالة الكلام من السيرفر ======
socket.on("speak_state", ({ speaking }) => {
  isBotSpeaking = !!speaking;
  updateListeningState();
});

// ====== تحكم الاستماع ======
function updateListeningState() {
// يسمح بالاستماع فقط إذا: في شخص + البوت ساكت (من السيرفر) + المتصفح ما ينطق الآن
  allowListen = personPresent && !isBotSpeaking && !ttsBusy;


  if (allowListen) {
    micEl.textContent = "🎤 أستمع لك الآن...";
    startASR();
  } else {
    micEl.textContent = isBotSpeaking ? "🔊 راشد يتكلم..." : "⏸️ بانتظار اقتراب شخص أمام الكاميرا...";
    stopASR(true); // abort hard
  }
}

// ====== ASR (Web Speech API) ======
function ensureASR() {
  if (bootedASR) return;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    appendSys("❌ المتصفح لا يدعم التعرف على الصوت. استخدم Chrome من فضلك.");
    return;
  }
  rec = new SR();
  rec.lang = "ar-SA";
  rec.continuous = true;
  rec.interimResults = false;

  rec.onstart = () => { micEl.textContent = "🎤 أستمع لك الآن..."; };
  rec.onend   = () => {
    // لا تعيد التشغيل إذا البوت يتكلم أو لا يوجد شخص
    if (allowListen) {
      // أعد التشغيل بهدوء
      try { rec.start(); } catch {}
    }
  };
  rec.onerror = (e) => {
    // أخطاء الشبكة/المايك تحدث، جرّب بعد قليل ان سمحنا
    if (allowListen) setTimeout(() => { try { rec.start(); } catch {} }, 400);
  };

  rec.onresult = (e) => {
    if (!allowListen) return;   // حماية صلبة ضد الإيكو
    const i = e.resultIndex;
    const transcript = (e.results[i] && e.results[i][0] && e.results[i][0].transcript || "").trim();
    if (!transcript) return;
    appendUser(transcript);
    socket.emit("voice_input", { text: transcript });
  };

  bootedASR = true;
}

function startASR() {
  ensureASR();
  if (!rec) return;
  // لا تشغّل إذا ما نسمح
  if (!allowListen) return;
  // حاول تشغيله، قد يكون already running – لا بأس
  try { rec.start(); } catch {}
}

function stopASR(hard=false) {
  if (!rec) return;
  try {
    if (hard) rec.abort();  // أقوى من stop ويكسر أي onend-autorun
    else rec.stop();
  } catch {}
}

// ====== TTS ======
function speak(text) {
  if (!window.speechSynthesis) return;
  window.speechSynthesis.cancel();

  const u = new SpeechSynthesisUtterance(text);
  u.lang = "ar-SA";

  u.onstart = () => {
    ttsBusy = true;           // المتصفح بدأ ينطق
    updateListeningState();   // يوقف ASR مباشرة
  };
  u.onend = () => {
    setTimeout(() => {
      ttsBusy = false;        // خلّص النطق فعليًا
      updateListeningState(); // اسمح بالاستماع
    }, 1500);                 // مهلة أمان بعد انتهاء الصوت
  };
  u.onerror = () => {
    setTimeout(() => {
      ttsBusy = false;
      updateListeningState();
    }, 400);
  };

  window.speechSynthesis.speak(u);
}



// ====== تشغيل أولي ======
(async function boot() {
  // اطلب إذن المايك لتفعيل AEC/NS/AGC من المتصفح
  try {
    await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
  } catch (e) {
    appendSys("⚠️ مشكلة في الميكروفون: تأكد من إعطاء الإذن.");
  }

  socket.emit("start_stream");
  // في البداية، لا نسمع إلا إذا حضر شخص
  updateListeningState();
})();



u.onstart = () => { ttsBusy = true; updateListeningState(); socket.emit("tts_start"); };
u.onend   = () => { setTimeout(()=>{ ttsBusy = false; updateListeningState(); socket.emit("tts_end"); }, 1500); };
