// ========== TAB SWITCHING ==========

function openTab(tabName) {
    document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));

    const tab = document.getElementById(tabName);
    const btn = document.getElementById(tabName + "-btn");

    if (tab) tab.classList.add("active");
    if (btn) btn.classList.add("active");
}

// Default tab on load

openTab("health");

// ========== SYSTEM HEALTH REFRESH ==========

async function refreshData() {
    try {
        const res = await fetch("/health");
        const data = await res.json();

        document.getElementById("cpu").innerText = data.cpu_percent + "%";
        document.getElementById("memory").innerText = data.memory + "%";
        document.getElementById("disk").innerText = data.disk + "%";
        document.getElementById("uptime").innerText = data.uptime;
        document.getElementById("processes").innerText = data.processes;
        document.getElementById("sent").innerText = data.net_sent;
        document.getElementById("recv").innerText = data.net_recv;
        document.getElementById("time").innerText = data.time;
        document.getElementById("time-right").innerText = data.time;
        document.getElementById("alert").innerText = data.alert;
        document.getElementById("os").innerText = data.os;
    } catch (e) {
        console.warn("Health fetch error:", e);
    }
}

setInterval(refreshData, 5000);
refreshData();

// ========== CAMERA ALERT REFRESH ==========
async function refreshCameraAlert() {
    try {
        const res = await fetch("/get_alert");
        const data = await res.json();
        const text = data.alert || "No alerts";
        document.getElementById("camera-alert").innerText = text;

        if (text && text !== "✅ No alerts" && text !== "No alerts") {
            addLiveAlert(text);
        }
    } catch (e) {
        console.warn("Camera alert error:", e);
    }
}

setInterval(refreshCameraAlert, 4000);
refreshCameraAlert();

// ========== SMART ALERT HISTORY ==========
function addAlertToBox(msg) {
    const box = document.getElementById("alertBox");
    const p = document.createElement("p");
    p.innerText = `${new Date().toLocaleTimeString()} - ${msg}`;
    box.prepend(p);
}

function addLiveAlert(msg) {
    const box = document.getElementById("liveAlerts");
    const p = document.createElement("p");
    p.innerText = `${new Date().toLocaleTimeString()} - ${msg}`;
    box.prepend(p);
}

async function refreshSmartAlerts() {
    try {
        const healthRes = await fetch("/health");
        const healthData = await healthRes.json();
        if (healthData.alert && healthData.alert !== "✅ Normal") {
            addAlertToBox(healthData.alert);
        }

        const camRes = await fetch("/get_alert");
        const camData = await camRes.json();
        if (camData.alert && camData.alert !== "✅ No alerts") {
            addAlertToBox(camData.alert);
        }
    } catch (e) {
        console.warn("Smart alerts error:", e);
    }
}

setInterval(refreshSmartAlerts, 6000);

// ========== DEVICE TOGGLE ==========
async function toggleDevice(device) {
    try {
        const res = await fetch(`/toggle/${device}`, { method: "POST" });
        const data = await res.json();
        if (data[device]) {
            const el = document.getElementById(device + "-status");
            if (el) el.innerText = data[device];
        }
    } catch (e) {
        console.warn("Toggle error:", e);
    }
}

// ========== VOICE ASSISTANT (TEXT INPUT FOR NOW) ==========

let currentController = null;
let currentUtterance = null;
let speakTimer = null;

const ASSISTANT_NAME = "jarvis";

async function handleVoiceCommand(rawText) {
    if (!rawText) return;

    const text = rawText.toLowerCase().trim();

    // ❌ Ignore if Jarvis name not spoken
    if (!text.startsWith(ASSISTANT_NAME)) {
        console.log("Wake word not detected");
        return;
    }

    // 🔴 If user says "Jarvis stop"
    if (text === "jarvis stop") {
        stopCurrentCommand();
        speakReply("Okay, stopping.");
        return;
    }

    // ✂ Remove "jarvis" from command
    const command = rawText.replace(/jarvis/i, "").trim();

    startListening(command);
}

async function startListening(text) {

    // 🔴 Stop previous command
    stopCurrentCommand();

    if (!text) return;

    const userSpan = document.getElementById("userSpeech");
    const aiSpan = document.getElementById("aiReply");

    if (userSpan) userSpan.innerText = "Jarvis heard: " + text;
    if (aiSpan) aiSpan.innerText = "Jarvis is thinking...";

    currentController = new AbortController();

    try {
        const res = await fetch("/assistant", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: text }),
            signal: currentController.signal
        });

        const data = await res.json();
        const reply = data.reply || "No response";

        if (aiSpan) aiSpan.innerText = "Jarvis: " + reply;
        speakReply(reply);

    } catch (e) {
        if (e.name === "AbortError") return;
        if (aiSpan) aiSpan.innerText = "Jarvis error.";
    }
}

function stopCurrentCommand() {
    if (currentController) {
        currentController.abort();
        currentController = null;
    }
    if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
    }
    if (speakTimer) {
        clearTimeout(speakTimer);
        speakTimer = null;
    }
}

function speakReply(text) {
    currentUtterance = new SpeechSynthesisUtterance(text);
    currentUtterance.lang = "en-IN"; // or hi-IN
    currentUtterance.rate = 1;
    currentUtterance.pitch = 1;

    speechSynthesis.speak(currentUtterance);

    speakTimer = setTimeout(() => {
        speechSynthesis.cancel();
    }, 20000);
}
//-------------------------------------------------------
// Modern Voice Assistant JS
//-------------------------------------------------------

// Typing animation for AI replies
function typeText(element, text, speed = 30) {
    element.innerHTML = "";
    let i = 0;
    let interval = setInterval(() => {
        element.innerHTML += text.charAt(i);
        i++;
        if (i >= text.length) clearInterval(interval);
    }, speed);
}

// Text-to-Speech (AI reply voice)
function speak(text) {
    const msg = new SpeechSynthesisUtterance(text);
    msg.lang = "en-US";
    msg.pitch = 1;
    msg.rate = 1;
    msg.volume = 1;
    window.speechSynthesis.speak(msg);
}

// Modern Speech-to-Text using Web Speech API
let recognition;
if ("webkitSpeechRecognition" in window) {
    recognition = new webkitSpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = "en-US";
} else {
    alert("Your browser does NOT support speech recognition!");
}

// Voice assistant logic
async function startListening() {
    const micBtn = document.getElementById("micBtn");
    const userSpeech = document.getElementById("userSpeech");
    const aiReply = document.getElementById("aiReply");

    aiReply.innerHTML = "";
    userSpeech.innerHTML = "...Listening...";

    micBtn.classList.add("recording");

    recognition.start();

    recognition.onresult = async (event) => {
        const text = event.results[0][0].transcript;
        userSpeech.innerText = text;
        micBtn.classList.remove("recording");

        // Send to Python backend
        const res = await fetch("/assistant", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: text })
        });

        const data = await res.json();

        // Typing animation
        typeText(aiReply, data.reply);

        // Voice output
        speak(data.reply);
    };

    recognition.onerror = (event) => {
        userSpeech.innerHTML = "❌ Listening error";
        micBtn.classList.remove("recording");
    };
}
