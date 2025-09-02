// =================== STREAM / PRESENCE ===================
const FPS          = 3;
const FRAME_WIDTH  = 224;
const JPEG_QUALITY = 0.6;

// Azure Speech
const AZURE_REGION = "qatarcentral";
const AZURE_VOICE  = "ar-SA-HamedNeural";
const STT_LANG     = "ar-SA";

// ============================ DOM ==========================
const chatLog      = document.getElementById("chatLog");
const chatInput    = document.getElementById("chatInput");
const sendBtn      = document.getElementById("sendBtn");
const sttStartBtn  = document.getElementById("sttStartBtn");
const sttStopBtn   = document.getElementById("sttStopBtn");

const rashidVideo  = document.getElementById("rashidVideo");
const presencePill = document.getElementById("presencePill");
const statusDot    = document.getElementById("statusDot");

const localVideo   = document.getElementById("localVideo");
const cameraStatus = document.getElementById("cameraStatus");
const enableBtn    = document.getElementById("enableCamBtn");

// ============================ Chat UI helpers ==========================
function addMessage(text, who = "user") {
  const msg = document.createElement("div");
  msg.className = "msg " + who;
  msg.textContent = text;
  chatLog.appendChild(msg);
  chatLog.scrollTop = chatLog.scrollHeight;
}
function showTyping() {
  if (document.getElementById("typingIndicator")) return;
  const div = document.createElement("div");
  div.className = "typing";
  div.innerHTML = "<span></span><span></span><span></span>";
  div.id = "typingIndicator";
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}
function hideTyping() {
  document.getElementById("typingIndicator")?.remove();
}

// ============================ Rashid face video ==========================
function isSiteAudioPlaying() {
  return ttsAudio && !ttsAudio.paused && !ttsAudio.ended && ttsAudio.currentTime > 0;
}
function setRashidState(state) {
  // If site audio is playing, force "speaking" until the audio ENDS.
  if (isSiteAudioPlaying() && state !== "speaking") return;

  if (!rashidVideo) return;
  let src = "assets/rashid_silent.mp4";
  if (state === "speaking")  src = "assets/rashid_speaking.mp4";
  if (state === "listening") src = "assets/rashid_listening.mp4";

  if (rashidVideo.src.includes(src)) return; // avoid reload if same
  rashidVideo.src = src;
  rashidVideo.loop = true;
  rashidVideo.load();
  rashidVideo.play().catch(()=>{});
}

// ============================ Global state ==========================
let recognizer   = null;
let isPresent    = false;   // presence from server
let wantMic      = true;    // user/auto desire to listen when allowed
let handlingUtterance = false;

// Hidden site-wide audio sink for TTS (and any other site sounds you choose to route)
const ttsAudio = new Audio();
ttsAudio.preload = "auto";
ttsAudio.playsInline = true;
// Don't show built-in controls; it's a hidden sink

// Whenever site audio starts playing, force "speaking" and block mic
ttsAudio.addEventListener("playing", () => {
  setRashidState("speaking");
  stopAzureSTT(true); // HARD gate: mic off while audio plays
});
// Even if paused mid-way, keep speaking look until ENDED per your request
ttsAudio.addEventListener("pause", () => {
  if (!ttsAudio.ended) {
    setRashidState("speaking"); // keep speaking until ended
  }
});
// Only on ENDED we can leave speaking
ttsAudio.addEventListener("ended", () => {
  // Decide next state: listen only if presence & user wants & not speaking
  if (isPresent && wantMic) {
    startAzureSTT();          // will flip to "listening"
  } else {
    setRashidState("silent");
  }
});

// Convenience: route any other <audio>/<video> elements through the same sink
// Call registerAudioElement(el) on your other players to link their lifecycle to Rashid:
function registerAudioElement(el) {
  el.addEventListener("playing", () => {
    setRashidState("speaking");
    stopAzureSTT(true);
  });
  el.addEventListener("pause", () => {
    if (!el.ended) setRashidState("speaking");
  });
  el.addEventListener("ended", () => {
    if (isPresent && wantMic) startAzureSTT(); else setRashidState("silent");
  });
}

// ============================ Gating helpers ==========================
function setListeningUI(on) {
  sttStartBtn?.classList.toggle("listening", on);
}

// ============================ Socket.io ==========================
const socket = io("/", { transports: ["websocket", "polling"], withCredentials: true });

socket.on("connect", () => { if (statusDot) statusDot.style.background = "#2bd576"; });
socket.on("connect_error", (e) => addMessage("connect_error: " + e.message,"bot"));

// Presence controls permission; tied to audio sink so we never listen during site audio
socket.on("presence", ({ present }) => {
  isPresent = !!present;
  if (presencePill) {
    presencePill.textContent = present ? "تم رصد شخص أمام الكاميرا" : "بانتظار اقتراب شخص أمام الكاميرا…";
  }
  if (!isSiteAudioPlaying()) {
    if (isPresent && wantMic) startAzureSTT(); else stopAzureSTT(true);
  }
});

// ====================== CAMERA + SENDER =======================
let stream = null;
let loopTimer = null;
let sending = false;

const sendCanvas = document.createElement("canvas");
const sendCtx    = sendCanvas.getContext("2d");

async function captureAndSend() {
  if (sending) return;
  if (!localVideo || localVideo.readyState < 2 || !localVideo.videoWidth) return;
  const scale = FRAME_WIDTH / localVideo.videoWidth;
  sendCanvas.width  = FRAME_WIDTH;
  sendCanvas.height = Math.round(localVideo.videoHeight * scale);
  sendCtx.drawImage(localVideo, 0, 0, sendCanvas.width, sendCanvas.height);

  sending = true;
  try {
    await new Promise((resolve) => {
      sendCanvas.toBlob(async (blob) => {
        const buf = await blob.arrayBuffer();
        socket.emit("client_frame_bin", buf);
        resolve();
      }, "image/jpeg", JPEG_QUALITY);
    });
  } finally { sending = false; }
}
function startSenderLoop() {
  clearInterval(loopTimer);
  loopTimer = setInterval(captureAndSend, Math.max(1, Math.floor(1000 / FPS)));
}
async function startClientCamera() {
  try {
    if (cameraStatus) cameraStatus.textContent = "Camera: requesting permission...";
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } },
      audio: false
    });
  } catch (e) {
    if (cameraStatus) cameraStatus.textContent = `Camera error: ${e.name} - ${e.message}`;
    addMessage(`getUserMedia failed: ${e.name} - ${e.message}`,"bot");
    return;
  }
  try {
    localVideo.srcObject = stream;
    localVideo.muted = true;
    localVideo.playsInline = true;
    await new Promise((res) => (localVideo.onloadedmetadata = res));
    await localVideo.play();
    localVideo.style.display = "block";
    if (cameraStatus) cameraStatus.textContent = `Camera: live (${localVideo.videoWidth}×${localVideo.videoHeight})`;
  } catch (e) {
    if (cameraStatus) cameraStatus.textContent = "Camera stream error";
    addMessage("Cannot attach stream: " + e.message,"bot");
    return;
  }
  startSenderLoop();
}
enableBtn?.addEventListener("click", () => startClientCamera());

// ============================ Azure token ==========================
async function getAzureToken() {
  const r = await fetch("/azure/token", { cache: "no-store" });
  const t = await r.text();
  if (!r.ok || !t) throw new Error("azure token fetch failed");
  return t;
}

// ============================ STT ==========================
function stopAzureSTT(silent=false) {
  if (!recognizer) { setListeningUI(false); return; }
  recognizer.stopContinuousRecognitionAsync(() => {
    try { recognizer.close(); } catch {}
    recognizer = null;
    setListeningUI(false);
    // After stopping, if site audio is NOT playing, show silent
    if (!isSiteAudioPlaying()) setRashidState("silent");
    if (!silent) addMessage("⏹️ تم الإيقاف.","bot");
  }, () => {
    recognizer = null;
    setListeningUI(false);
    if (!isSiteAudioPlaying()) setRashidState("silent");
  });
}

async function startAzureSTT() {
  // NEVER start if site audio is playing
  if (recognizer || !window.SpeechSDK || isSiteAudioPlaying()) return;
  try {
    const token = await getAzureToken();
    const speechConfig = SpeechSDK.SpeechConfig.fromAuthorizationToken(token, AZURE_REGION);
    speechConfig.speechRecognitionLanguage = STT_LANG;

    // Tighten silence so STT doesn't hang open
    speechConfig.setProperty(SpeechSDK.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "7000");
    speechConfig.setProperty(SpeechSDK.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, "700");

    const audioConfig = SpeechSDK.AudioConfig.fromDefaultMicrophoneInput();
    recognizer = new SpeechSDK.SpeechRecognizer(speechConfig, audioConfig);

    setListeningUI(true);
    // Do NOT switch to listening yet; only when user actually speaks
    if (!isSiteAudioPlaying()) setRashidState("silent");

    // When user actually starts speaking, flip to listening
    if (recognizer.speechStartDetected) {
      recognizer.speechStartDetected = () => { if (!isSiteAudioPlaying()) setRashidState("listening"); };
    }
    recognizer.recognizing = (s, e) => {
      if (e?.result?.text && !isSiteAudioPlaying()) setRashidState("listening");
    };

    recognizer.recognized = (s, e) => {
      const txt = e?.result?.text?.trim();
      if (!txt || handlingUtterance) return;
      handlingUtterance = true;

      addMessage(txt,"user");
      showTyping();

      // Stop mic to avoid picking up TTS
      stopAzureSTT(true);
      // emit to server
      socket.emit("voice_input", { text: txt });

      setTimeout(() => { handlingUtterance = false; }, 600);
    };

    recognizer.startContinuousRecognitionAsync();
  } catch (e) {
    addMessage("STT init failed: " + e,"bot");
  }
}

// ============================ TTS (site audio–driven) ==========================
async function playTTSUntilFinished(text) {
  // Synthesize to MP3 bytes, then play via the shared ttsAudio element
  try {
    if (window.SpeechSDK) {
      const token = await getAzureToken();
      const speechConfig = SpeechSDK.SpeechConfig.fromAuthorizationToken(token, AZURE_REGION);
      speechConfig.speechSynthesisVoiceName = AZURE_VOICE;
      speechConfig.speechSynthesisOutputFormat =
        SpeechSDK.SpeechSynthesisOutputFormat.Audio24Khz160KBitRateMonoMp3;

      const synth = new SpeechSDK.SpeechSynthesizer(speechConfig, null);
      const audioUrl = await new Promise((resolve, reject) => {
        synth.speakTextAsync(
          text,
          (result) => {
            try {
              synth.close();
              const blob = new Blob([result.audioData], { type: "audio/mpeg" });
              resolve(URL.createObjectURL(blob));
            } catch (err) { reject(err); }
          },
          (err) => { try { synth.close(); } catch {} reject(err); }
        );
      });

      // Route playback through our sink (drives Rashid face + mic gating)
      ttsAudio.src = audioUrl;
      try { await ttsAudio.play(); } catch (err) { console.warn("audio.play() failed", err); }
      // We DO NOT resolve here; speaking ends only on ttsAudio 'ended'
      await new Promise((res) => ttsAudio.addEventListener("ended", res, { once: true }));
      URL.revokeObjectURL(audioUrl);
      return;
    }
  } catch (e) {
    console.warn("Azure TTS (data->audio) failed, falling back:", e);
  }

  // Fallback: Web Speech API (route can't use ttsAudio; we emulate same behavior)
  await new Promise((res) => {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "ar-SA";
    const v = speechSynthesis.getVoices().find(v => v.lang === "ar-SA") || null;
    if (v) u.voice = v;

    // Simulate the same "speaking" hold: set speaking at start and only release on end
    u.onstart = () => { setRashidState("speaking"); stopAzureSTT(true); };
    u.onend   = () => { res(); };
    speechSynthesis.speak(u);
  });
}

// ============================ Bot reply ==========================
socket.on("voice_response", async ({ text }) => {
  hideTyping();
  addMessage(text,"bot");

  // Speak through site audio sink; mic is auto-stopped by audio event handlers
  try { await playTTSUntilFinished(text); }
  catch (e) { console.warn("TTS playback failed:", e); }

  // After audio ENDS, the 'ended' listener decides whether to restart STT
});

// If your backend also emits plain text replies, you may comment this out to avoid duplicates
// socket.on("server_response", ({ data }) => { addMessage(data,"bot"); });

// ============================ Mic buttons ==========================
sttStartBtn?.addEventListener("click", () => {
  wantMic = true;
  if (!isSiteAudioPlaying() && isPresent) startAzureSTT();
});
sttStopBtn ?.addEventListener("click", () => {
  wantMic = false;
  stopAzureSTT();
});

// ============================ Text input ==========================
sendBtn?.addEventListener("click", () => {
  const text = chatInput.value.trim();
  if (!text) return;
  addMessage(text,"user");
  chatInput.value="";
  showTyping();

  // Stop mic so the bot's audio won't be picked up
  stopAzureSTT(true);
  socket.emit("voice_input",{ text });
});

// ============================ Init ==========================
document.addEventListener("DOMContentLoaded", async () => {
  setRashidState("silent");
  try { await startClientCamera(); }
  catch { if (cameraStatus) cameraStatus.textContent = "Camera: press Enable Camera"; }
  // Wait for presence before arming STT
});
