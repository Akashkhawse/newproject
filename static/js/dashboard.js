const assistantModeLabels = {
    hybrid: "Hybrid",
    manual: "Manual",
    ai: "AI",
    research: "Research",
    self_monitoring: "Self monitoring",
    sentinel: "Sentinel",
};

const appState = {
    isAdmin: document.body?.dataset.isAdmin === "1",
    currentUser: document.body?.dataset.currentUser || "",
    currentUserName: document.body?.dataset.currentUserName || "",
    currentRole: document.body?.dataset.currentRole || "user",
    wakeWordEnabled: false,
    wakeWordRecognition: null,
    wakeCommandRecognition: null,
    wakeWordCooldownUntil: 0,
    cameraProfiles: [],
    activeCameraId: "",
    geminiConfigured: false,
    automationSnapshot: null,
};

let localCameraStream = null;
let typingTimer = null;
let manualRecognition = null;

function getJsonHeaders(extraHeaders = {}) {
    return {
        Accept: "application/json",
        ...extraHeaders,
    };
}

async function parseJson(response) {
    try {
        return await response.json();
    } catch (error) {
        return null;
    }
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, {
        ...options,
        headers: getJsonHeaders(options.headers),
    });
    const payload = await parseJson(response);

    if (response.status === 401) {
        if (payload?.redirect) {
            window.location.href = payload.redirect;
        }
        throw new Error(payload?.error || "Authentication required");
    }

    if (response.status === 403) {
        throw new Error(payload?.error || "Access denied");
    }

    if (!response.ok) {
        const error = new Error(payload?.error || `Request failed (${response.status})`);
        error.payload = payload;
        throw error;
    }

    return payload || {};
}

async function requestFormData(url, formData, options = {}) {
    const response = await fetch(url, {
        method: options.method || "POST",
        body: formData,
        headers: getJsonHeaders(options.headers),
    });
    const payload = await parseJson(response);

    if (response.status === 401) {
        if (payload?.redirect) {
            window.location.href = payload.redirect;
        }
        throw new Error(payload?.error || "Authentication required");
    }

    if (response.status === 403) {
        throw new Error(payload?.error || "Access denied");
    }

    if (!response.ok) {
        const error = new Error(payload?.error || `Request failed (${response.status})`);
        error.payload = payload;
        throw error;
    }

    return payload || {};
}

function escapeFieldKey(value) {
    const raw = String(value || "");
    return window.CSS?.escape ? window.CSS.escape(raw) : raw.replace(/["\\]/g, "\\$&");
}

function getBoundElements(key) {
    const elements = new Set();
    const byId = document.getElementById(key);
    if (byId) {
        elements.add(byId);
    }

    document.querySelectorAll(`[data-field="${escapeFieldKey(key)}"]`).forEach((element) => {
        elements.add(element);
    });

    return [...elements];
}

function setText(key, value, fallback = "--") {
    const finalValue = value ?? fallback;
    const text = finalValue === "" ? fallback : String(finalValue);

    getBoundElements(key).forEach((element) => {
        element.textContent = text;
    });
}

function formatTimestamp(value) {
    if (!value) return "Waiting for updates";

    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return value;
    }

    return date.toLocaleString();
}

function formatAssistantMode(mode) {
    return assistantModeLabels[mode] || assistantModeLabels.hybrid;
}

function hasDevanagari(text) {
    return /[\u0900-\u097F]/.test(String(text || ""));
}

function getRecognitionLanguage() {
    const candidates = Array.isArray(navigator.languages) && navigator.languages.length > 0
        ? navigator.languages
        : [navigator.language || "en-IN"];
    return candidates.find((value) => /^hi/i.test(String(value || ""))) || candidates[0] || "en-IN";
}

function setInlineMessage(elementId, message, isError = false, defaultText = "") {
    const element = document.getElementById(elementId);
    if (!element) return;
    const hasMessage = Boolean(message);
    element.textContent = message || defaultText || "";
    element.classList.toggle("feedback-error", hasMessage && Boolean(isError));
    element.classList.toggle("feedback-success", hasMessage && !isError);
}

function formatBooleanLabel(value, enabledLabel = "Enabled", disabledLabel = "Disabled") {
    return value ? enabledLabel : disabledLabel;
}

function formatVisibilityLabel(value) {
    const labels = {
        private: "Private",
        team: "Team",
        public: "Public",
        admins: "Admins",
    };
    return labels[value] || value || "--";
}

function formatPolicyBoolean(value) {
    return value ? "Acknowledged" : "Pending";
}

function getAssistantMode() {
    return document.getElementById("assistant-mode")?.value || "hybrid";
}

function setAssistantActionText(text) {
    setText("assistantAction", text, "No manual action yet");
}

function setFaceFeedback(text, isError = false) {
    setInlineMessage(
        "face-upload-feedback",
        text,
        isError,
        "Upload clear face images to identify known vs unknown visitors.",
    );
}

function setProfileFeedback(text, isError = false) {
    setInlineMessage(
        "profile-feedback",
        text,
        isError,
        "Use this space to update your profile details and privacy preferences.",
    );
}

function setAutomationFeedback(text, isError = false) {
    setInlineMessage(
        "automation-feedback",
        text,
        isError,
        "Save thresholds to keep self monitoring tuned to your space.",
    );
}

function isLocalPreviewActive() {
    return Boolean(localCameraStream) && document.getElementById("camera-source")?.value === "local";
}

function refreshServerCameraFeed() {
    const image = document.getElementById("server-camera");
    if (!image) return;
    image.src = `/camera_feed?ts=${Date.now()}`;
}

function typeText(element, text, speed = 16) {
    if (!element) return;

    if (typingTimer) {
        clearInterval(typingTimer);
    }

    element.textContent = "";
    let index = 0;
    typingTimer = window.setInterval(() => {
        element.textContent += text.charAt(index);
        index += 1;

        if (index >= text.length) {
            clearInterval(typingTimer);
            typingTimer = null;
        }
    }, speed);
}

function speak(text) {
    if (!("speechSynthesis" in window) || !text) return;

    const message = new SpeechSynthesisUtterance(text);
    message.lang = hasDevanagari(text) ? "hi-IN" : "en-IN";
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(message);
}

function createSpeechRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return null;
    return new SpeechRecognition();
}

function updateWakeWordUi(message) {
    const button = document.getElementById("wake-word-toggle");
    if (button) {
        button.textContent = appState.wakeWordEnabled ? "Disable wake word" : "Enable wake word";
    }
    setText("wake-word-status", message, "Wake word idle");
}

function openTab(tabName) {
    const wasCameraOpen = document.getElementById("camera")?.classList.contains("active");

    document.querySelectorAll(".tab-panel").forEach((section) => {
        const isActive = section.id === tabName;
        section.classList.toggle("active", isActive);
        section.setAttribute("aria-hidden", String(!isActive));
    });

    document.querySelectorAll(".tab-btn").forEach((button) => {
        button.classList.toggle("active", button.dataset.tab === tabName);
    });

    if (tabName !== "camera" && wasCameraOpen) {
        stopLocalCamera(false);
    }
}

function appendAssistantLog(role, text, metaText = "") {
    const container = document.getElementById("assistant-log");
    if (!container) return null;

    const entry = document.createElement("article");
    entry.className = `assistant-entry ${role}`;

    const meta = document.createElement("div");
    meta.className = "assistant-meta";
    meta.textContent = metaText;

    const bubble = document.createElement("div");
    bubble.className = "assistant-bubble";
    bubble.textContent = text;

    entry.appendChild(meta);
    entry.appendChild(bubble);
    container.appendChild(entry);
    container.scrollTop = container.scrollHeight;

    return { entry, meta, bubble };
}

function ensureAssistantIntro() {
    const container = document.getElementById("assistant-log");
    if (!container || container.childElementCount > 0) return;

    appendAssistantLog(
        "system",
        "Assistant console ready. Ask about health, faces, cameras, alerts, profile details, admin summary, or device actions.",
        "SmartAI / control scope",
    );
}

function buildFaceWatchSummary(faces) {
    const items = Array.isArray(faces) ? faces : [];
    if (items.length === 0) {
        return "No face activity yet";
    }

    const parts = [];
    items.forEach((face) => {
        const count = face.count ?? 1;
        if (face.recognized) {
            parts.push(`${face.name} (${count})`);
        } else {
            parts.push(`Unknown (${count})`);
        }
    });
    return parts.join(", ");
}

function renderDetections(detections, faces) {
    const container = document.getElementById("detections");
    if (!container) return;

    container.innerHTML = "";

    const faceItems = Array.isArray(faces) ? faces : [];
    const detectionItems = Array.isArray(detections) ? detections : [];

    if (faceItems.length === 0 && detectionItems.length === 0) {
        container.textContent = "No detections yet";
        return;
    }

    faceItems.forEach((face) => {
        const row = document.createElement("p");
        row.textContent = face.recognized
            ? `Face: ${face.name} (${face.count})`
            : `Unknown face (${face.count})`;
        container.appendChild(row);
    });

    detectionItems.forEach((detection) => {
        const row = document.createElement("p");
        row.textContent = `Object: ${detection.label} (${detection.count})`;
        container.appendChild(row);
    });
}

function renderAlertList(elementId, items, emptyText) {
    const box = document.getElementById(elementId);
    if (!box) return;

    box.innerHTML = "";

    if (!items || items.length === 0) {
        box.textContent = emptyText;
        return;
    }

    items.slice(0, 12).forEach((item) => {
        const row = document.createElement("p");
        const source = item?.source ? `[${String(item.source).toUpperCase()}] ` : "";
        const message = item?.message || item?.alert || "No alerts";
        const timestamp = item?.time ? `${formatTimestamp(item.time)} - ` : "";
        row.textContent = `${timestamp}${source}${message}`;
        box.appendChild(row);
    });
}

function createDeviceRow(device) {
    const row = document.createElement("div");
    row.className = "device-row";
    row.classList.add(device.state === "ON" ? "is-on" : "is-off");
    row.dataset.deviceName = device.name;

    const info = document.createElement("div");
    info.className = "device-meta";

    const name = document.createElement("strong");
    name.className = "device-name";
    name.textContent = device.name;

    const state = document.createElement("span");
    state.className = "device-state";
    state.classList.add(device.state === "ON" ? "is-on" : "is-off");
    state.textContent = device.state;

    const updated = document.createElement("span");
    updated.className = "panel-note";
    updated.textContent = device.updated_at
        ? `Updated ${formatTimestamp(device.updated_at)}`
        : "Recently updated";

    info.appendChild(name);
    info.appendChild(state);
    info.appendChild(updated);

    const actions = document.createElement("div");
    actions.className = "device-actions";

    const toggleButton = document.createElement("button");
    toggleButton.type = "button";
    toggleButton.className = "btn-small";
    toggleButton.textContent = "Toggle";
    toggleButton.addEventListener("click", async () => {
        await toggleDevice(device.name, state);
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "btn-small btn-secondary danger-button";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", async () => {
        await deleteDevice(device.name);
    });

    actions.appendChild(toggleButton);
    actions.appendChild(deleteButton);

    row.appendChild(info);
    row.appendChild(actions);
    return row;
}

function renderCameraSwitcher(cameras, activeCameraId) {
    const container = document.getElementById("camera-switcher-list");
    const select = document.getElementById("server-camera-select");
    if (!container || !select) return;

    container.innerHTML = "";
    select.innerHTML = "";

    if (!Array.isArray(cameras) || cameras.length === 0) {
        container.textContent = "No cameras configured";
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No cameras";
        select.appendChild(option);
        return;
    }

    cameras.forEach((camera) => {
        const option = document.createElement("option");
        option.value = camera.id;
        option.textContent = camera.label || camera.name;
        select.appendChild(option);

        const card = document.createElement("div");
        card.className = "camera-switcher-card";
        if (camera.id === activeCameraId) {
            card.classList.add("active");
        }

        const button = document.createElement("button");
        button.type = "button";
        button.className = "switcher-button";
        button.innerHTML = `
            <span class="switcher-meta">
              <strong>${camera.name}</strong>
              <span class="panel-note">${camera.source_display}</span>
            </span>
            <span class="role-pill ${camera.id === activeCameraId ? "role-pill-admin" : "role-pill-user"}">
              ${camera.id === activeCameraId ? "Live" : "Standby"}
            </span>
        `;
        button.addEventListener("click", async () => {
            await switchActiveServerCamera(camera.id);
        });

        const actions = document.createElement("div");
        actions.className = "switcher-actions";

        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "btn-small btn-secondary danger-button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", async () => {
            await deleteServerCamera(camera.id, camera.name);
        });

        actions.appendChild(deleteButton);
        card.appendChild(button);
        card.appendChild(actions);
        container.appendChild(card);
    });

    select.value = activeCameraId || cameras[0].id;
}

function setCameraAddFeedback(message, isError = false) {
    setInlineMessage("camera-add-feedback", message, isError);
}

function setMobileCameraFeedback(message, isError = false) {
    setInlineMessage(
        "mobile-camera-feedback",
        message,
        isError,
        "Open the copied link on your phone and tap Start streaming.",
    );
}

async function copyTextToClipboard(text) {
    if (!text) return false;
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (error) {
        console.warn("Clipboard write failed:", error);
    }
    return false;
}

async function setupMobileCameraBridge(copyLinkOnly = false) {
    try {
        setMobileCameraFeedback(copyLinkOnly ? "Getting mobile link..." : "Preparing mobile camera bridge...");
        const payload = await requestJson("/api/mobile-camera/link", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({
                device_id: "default",
                set_active: !copyLinkOnly,
                name: "Mobile Phone Camera",
            }),
        });

        appState.cameraProfiles = Array.isArray(payload.cameras) ? payload.cameras : appState.cameraProfiles;
        appState.activeCameraId = payload.active_camera_id || payload.active_camera?.id || appState.activeCameraId;
        renderCameraSwitcher(appState.cameraProfiles, appState.activeCameraId);

        const source = document.getElementById("camera-source");
        if (source && !copyLinkOnly) source.value = "server";
        if (!copyLinkOnly) {
            stopLocalCamera(false);
            refreshServerCameraFeed();
            setText("camera-stream-source", "mobile");
            setText("camera-status", "Waiting for phone camera stream");
            setText("camera-active-label", payload.active_camera?.name || "Mobile Phone Camera");
        }

        const mobileLink = payload.mobile_page_url || "";
        const copied = await copyTextToClipboard(mobileLink);
        if (mobileLink && !copied) {
            window.prompt("Copy this mobile camera link", mobileLink);
        }
        setMobileCameraFeedback(
            copied
                ? "Mobile link copied. Open it on your phone and tap Start streaming."
                : mobileLink
                    ? "Open the shown link on your phone and tap Start streaming."
                    : "Mobile camera bridge is ready.",
        );
        await Promise.all([fetchHealth(), fetchCameraAlert()]);
    } catch (error) {
        setMobileCameraFeedback(error.message || "Unable to prepare mobile camera bridge.", true);
    }
}

async function deleteServerCamera(cameraId, cameraName) {
    const shouldDelete = window.confirm(`Delete camera ${cameraName}?`);
    if (!shouldDelete) return;

    try {
        setCameraAddFeedback(`Deleting ${cameraName}...`);
        const payload = await requestJson(`/api/cameras/${encodeURIComponent(cameraId)}`, {
            method: "DELETE",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });

        appState.cameraProfiles = Array.isArray(payload.cameras) ? payload.cameras : [];
        appState.activeCameraId = payload.active_camera_id || payload.active_camera?.id || "";
        renderCameraSwitcher(appState.cameraProfiles, appState.activeCameraId);

        const source = document.getElementById("camera-source");
        if (source) source.value = "server";
        stopLocalCamera(false);
        refreshServerCameraFeed();
        setCameraAddFeedback(`${cameraName} deleted successfully.`);
        await Promise.all([fetchHealth(), fetchCameraAlert()]);
    } catch (error) {
        setCameraAddFeedback(error.message || "Failed to delete camera.", true);
    }
}

async function addServerCamera(event) {
    event.preventDefault();

    const nameInput = document.getElementById("camera-name");
    const sourceInput = document.getElementById("camera-source-input");
    if (!nameInput || !sourceInput) return;

    const name = nameInput.value.trim();
    const source = sourceInput.value.trim();
    if (!name || !source) {
        setCameraAddFeedback("Name and camera IP/URL are required.", true);
        return;
    }

    try {
        setCameraAddFeedback("Adding camera...");
        const payload = await requestJson("/api/cameras", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({ name, source }),
        });

        if (!payload || payload.error) {
            throw new Error(payload?.error || "Unable to add camera.");
        }

        nameInput.value = "";
        sourceInput.value = "";
        setCameraAddFeedback("Camera added successfully.");
        appState.cameraProfiles = Array.isArray(payload.cameras) ? payload.cameras : appState.cameraProfiles;
        appState.activeCameraId = payload.active_camera_id || payload.active_camera?.id || appState.activeCameraId;
        renderCameraSwitcher(appState.cameraProfiles, appState.activeCameraId);
        await Promise.all([fetchHealth(), fetchCameraAlert()]);
    } catch (error) {
        setCameraAddFeedback(error.message || "Failed to add camera.", true);
    }
}

function renderKnownFaceList(people, latestFaces = []) {
    const container = document.getElementById("known-face-list");
    if (!container) return;

    container.innerHTML = "";
    const visibleRecognized = new Set(
        (Array.isArray(latestFaces) ? latestFaces : [])
            .filter((face) => face.recognized)
            .map((face) => String(face.name)),
    );

    if (!Array.isArray(people) || people.length === 0) {
        container.textContent = "No known faces registered yet";
        return;
    }

    people.forEach((person) => {
        const row = document.createElement("div");
        row.className = "roster-row";

        const meta = document.createElement("div");
        meta.className = "roster-meta";

        const name = document.createElement("strong");
        const visibilityText = visibleRecognized.has(person.name) ? "Visible now" : "Stored";
        name.textContent = person.name;

        const details = document.createElement("span");
        details.className = "panel-note";
        details.textContent = `${person.sample_count} samples • ${visibilityText}`;

        meta.appendChild(name);
        meta.appendChild(details);

        const actions = document.createElement("div");
        actions.className = "roster-actions";

        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "btn-small btn-secondary danger-button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", async () => {
            await deleteKnownFace(person.name);
        });

        actions.appendChild(deleteButton);

        row.appendChild(meta);
        row.appendChild(actions);
        container.appendChild(row);
    });
}

async function fetchHealth() {
    try {
        const data = await requestJson("/health");
        appState.currentRole = data.current_user_role || appState.currentRole;
        appState.geminiConfigured = Boolean(data.gemini_configured);

        setText("cpu", `${data.cpu_percent}%`);
        setText("memory", `${data.memory}%`);
        setText("disk", `${data.disk}%`);
        setText("uptime", data.uptime);
        setText("processes", data.processes);
        setText("sent", data.net_sent);
        setText("recv", data.net_recv);
        setText("time", data.time);
        setText("time-right", data.time);
        setText("os", data.os);
        setText("alert", data.alert);
        setText("current-role", appState.currentRole);
        setText("profile-role", appState.currentRole);
        setText("yolo-enabled", data.yolo_enabled ? "Yes" : "No");
        setText("yolo-health-status", data.yolo_status || "YOLO status unavailable");
        setText("face-recognition-enabled", data.face_recognition_enabled ? "Yes" : "No");
        setText("face-recognition-status", data.face_recognition_status || "Face recognition unavailable");
        setText("known-people", data.known_people ?? 0, "0");
        setText("device-total", data.device_count ?? 0, "0");
        setText("device-active", data.active_device_count ?? 0, "0");
        setText("camera-count", data.camera_count ?? appState.cameraProfiles.length ?? 0, "0");
        setText("system-mode-status", data.system_mode_label || formatAssistantMode(data.system_mode));
        setText("automation-status", data.automation_status || "Loading automation status");
        setText("automation-risk", data.automation_risk || "normal");
        if (!isLocalPreviewActive()) {
            setText("camera-active-label", data.active_camera?.name || "Server camera");
            setText("camera-status", data.camera_status || "Waiting for camera");
        }
        setText(
            "assistant-stack-status",
            data.gemini_configured ? "AI linked + local fallback" : "Local fallback active",
            "Local fallback active",
        );
    } catch (error) {
        console.warn("Health fetch failed:", error);
    }
}

async function fetchCameraAlert() {
    try {
        const data = await requestJson("/get_alert");
        const diagnostics = data.camera_diagnostics || {};
        const cachedFaces = document.getElementById("known-face-list")?.dataset.cachedFaces;
        let cachedPeople = [];
        try {
            cachedPeople = cachedFaces ? JSON.parse(cachedFaces) : [];
        } catch (error) {
            cachedPeople = [];
        }

        setText("camera-alert", data.alert || data.message || "No alerts");
        setText(
            "camera-last",
            data.time ? `Last camera alert ${formatTimestamp(data.time)}` : "No recent camera alerts",
            "No recent camera alerts",
        );
        setText("camera-yolo-status", diagnostics.yolo_status || "Unavailable");
        setText("camera-frame-count", diagnostics.frames_processed ?? 0, "0");
        setText("camera-object-count", diagnostics.object_count ?? 0, "0");
        setText(
            "camera-last-detection",
            diagnostics.last_detection_at ? formatTimestamp(diagnostics.last_detection_at) : "No detection yet",
            "No detection yet",
        );
        setText("camera-last-error", diagnostics.last_error || "None", "None");
        setText("face-recognition-enabled", data.face_recognition_enabled ? "Yes" : "No");
        setText("face-recognition-status", data.face_recognition_status || "Face recognition unavailable");
        setText("known-people", data.known_people ?? 0, "0");
        setText("camera-count", data.camera_count ?? appState.cameraProfiles.length ?? 0, "0");
        setText("live-face-watch", buildFaceWatchSummary(data.faces || []));
        if (!isLocalPreviewActive()) {
            setText("camera-active-label", data.active_camera?.name || diagnostics.active_camera_name || "Server camera");
            setText("camera-status", data.camera_status || "Using server AI camera feed");
            setText("camera-stream-source", diagnostics.stream_source || "server");
        }

        renderDetections(data.detections || [], data.faces || []);
        renderKnownFaceList(cachedPeople, data.faces || []);
    } catch (error) {
        console.warn("Camera alert fetch failed:", error);
    }
}

async function fetchSmartAlerts() {
    try {
        const data = await requestJson("/alerts");
        const items = Array.isArray(data.items) ? data.items : [];
        const latest = data.latest_camera_alert ? [data.latest_camera_alert] : [];
        const liveItems = items.filter((item) => item.level && item.level !== "info");
        const summary = data.summary || {};

        renderAlertList("alertBox", items, "No alerts yet");
        renderAlertList("liveAlerts", liveItems.length > 0 ? liveItems : latest, "No live alerts");

        setText("alert-total", summary.total ?? items.length, "0");
        setText("alert-warning", summary.warning ?? 0, "0");
        setText("alert-error", summary.error ?? 0, "0");
        setText("alert-info", summary.info ?? 0, "0");
    } catch (error) {
        console.warn("Alerts fetch failed:", error);
    }
}

async function fetchDevices() {
    const container = document.getElementById("devices-list");
    if (!container) return;

    try {
        const payload = await requestJson("/api/devices");
        const devices = Array.isArray(payload.devices) ? payload.devices : [];
        container.innerHTML = "";

        if (devices.length === 0) {
            container.textContent = "No devices found";
            return;
        }

        devices.forEach((device) => {
            container.appendChild(createDeviceRow(device));
        });
    } catch (error) {
        console.warn("Devices fetch failed:", error);
    }
}

async function addDevice(name) {
    try {
        await requestJson("/api/devices", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({ name }),
        });
        await refreshOperationalViews();
    } catch (error) {
        window.alert(error.message);
    }
}

async function deleteDevice(name) {
    const shouldDelete = window.confirm(`Delete ${name}?`);
    if (!shouldDelete) return;

    try {
        await requestJson(`/api/devices/${encodeURIComponent(name)}`, {
            method: "DELETE",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        await refreshOperationalViews();
    } catch (error) {
        window.alert(error.message);
    }
}

async function toggleDevice(deviceName, stateElement) {
    try {
        const payload = await requestJson(`/toggle/${encodeURIComponent(deviceName)}`, {
            method: "POST",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });

        if (payload[deviceName] && stateElement) {
            stateElement.textContent = payload[deviceName];
        }

        await refreshOperationalViews();
    } catch (error) {
        console.warn("Toggle failed:", error);
    }
}

function renderAssistantActions(actions = [], handledLocally = false) {
    if (!Array.isArray(actions) || actions.length === 0) {
        setAssistantActionText(handledLocally ? "Handled locally with no direct state change" : "No manual action yet");
        return;
    }

    const action = actions[0];
    if (action.type === "device") {
        setAssistantActionText(`${action.name} -> ${action.state}`);
        return;
    }
    if (action.type === "camera") {
        setAssistantActionText(`Camera -> ${action.name}`);
        return;
    }

    setAssistantActionText("Local action completed");
}

async function sendAssistantQuery(text, options = {}) {
    const mode = options.mode || getAssistantMode();
    const source = options.source || "ui";
    const userSpeech = document.getElementById("userSpeech");
    const aiReply = document.getElementById("aiReply");

    if (userSpeech) userSpeech.textContent = text;
    if (aiReply) aiReply.textContent = "Thinking...";
    setText("assistantModeStatus", formatAssistantMode(mode), "Hybrid");
    setAssistantActionText("Waiting for response");

    appendAssistantLog("user", text, `Operator / ${formatAssistantMode(mode)}`);
    const pending = appendAssistantLog("assistant", "Thinking...", `SmartAI / ${formatAssistantMode(mode)}`);

    try {
        
        document.getElementById('voice-visualizer')?.classList.remove('hidden');
        const data = await requestJson("/assistant", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({
                query: text,
                mode,
                source,
                preferred_language: navigator.language || "en-IN",
            }),
        });

        const reply = data.reply || "No response";
        const effectiveMode = data.mode || mode;
        const modeText = data.handled_locally ? "Local control" : "AI response";

        setText("assistantModeStatus", formatAssistantMode(effectiveMode), "Hybrid");
        renderAssistantActions(data.actions || [], Boolean(data.handled_locally));
        typeText(aiReply, reply);
        speak(reply);

        if (pending) {
            pending.meta.textContent = `SmartAI / ${modeText}`;
            pending.bubble.textContent = reply;
        }

        if (Array.isArray(data.actions) && data.actions.some((action) => action.type === "camera")) {
            const sourceSelect = document.getElementById("camera-source");
            if (sourceSelect) sourceSelect.value = "server";
            stopLocalCamera(false);
            refreshServerCameraFeed();
        }

        if (Array.isArray(data.actions) && data.actions.length > 0) {
            await refreshOperationalViews();
        }
    } catch (error) {
        console.warn("Assistant request failed:", error);
        if (aiReply) aiReply.textContent = error.message || "Error talking to assistant.";
        document.getElementById('voice-visualizer')?.classList.add('hidden');
        setAssistantActionText("Assistant request failed");

        if (pending) {
            pending.meta.textContent = "SmartAI / error";
            pending.bubble.textContent = error.message || "Assistant request failed.";
        }
    }
}

function getManualRecognition() {
    if (manualRecognition) return manualRecognition;

    manualRecognition = createSpeechRecognition();
    if (!manualRecognition) return null;

    manualRecognition.continuous = false;
    manualRecognition.interimResults = false;
    manualRecognition.lang = getRecognitionLanguage();
    return manualRecognition;
}

async function startListening() {
    const micBtn = document.getElementById("micBtn");
    const userSpeech = document.getElementById("userSpeech");
    const aiReply = document.getElementById("aiReply");
    const recognitionInstance = getManualRecognition();

    if (!recognitionInstance) {
        const text = window.prompt("Speech recognition is not supported here. Type your query:");
        if (text) {
            await sendAssistantQuery(text, { source: "voice" });
        }
        return;
    }

    if (userSpeech) userSpeech.textContent = "Listening...";
    if (aiReply) aiReply.textContent = "---";
    if (micBtn) {
        micBtn.disabled = true;
        micBtn.textContent = "Listening...";
    }

    recognitionInstance.onresult = async (event) => {
        const text = event.results[0][0].transcript;
        if (micBtn) {
            micBtn.disabled = false;
            micBtn.textContent = "Speak now";
        }
        await sendAssistantQuery(text, { source: "voice" });
    };

    recognitionInstance.onerror = () => {
        if (userSpeech) userSpeech.textContent = "Listening failed";
        if (micBtn) {
            micBtn.disabled = false;
            micBtn.textContent = "Speak now";
        }
    };

    recognitionInstance.onend = () => {
        if (micBtn) {
            micBtn.disabled = false;
            micBtn.textContent = "Speak now";
        }
    };

    try {
        recognitionInstance.start();
    } catch (error) {
        if (micBtn) {
            micBtn.disabled = false;
            micBtn.textContent = "Speak now";
        }
        console.warn("Speech recognition start failed:", error);
    }
}

function transcriptHasWakeWord(text) {
    const normalized = String(text || "").trim().toLowerCase();
    return normalized.includes("computer") || normalized.includes("कंप्यूटर") || normalized.includes("कम्प्यूटर");
}

function extractCommandAfterWakeWord(text) {
    const raw = String(text || "").trim();
    const patterns = [
        /computer[\s,:-]+(.+)/i,
        /कंप्यूटर[\s,:-]+(.+)/i,
        /कम्प्यूटर[\s,:-]+(.+)/i,
    ];

    for (const pattern of patterns) {
        const match = raw.match(pattern);
        if (match?.[1]) {
            return match[1].trim();
        }
    }

    return "";
}

function stopWakeWordMode() {
    appState.wakeWordEnabled = false;

    try {
        appState.wakeWordRecognition?.stop();
    } catch (error) {
        console.warn("Wake recognition stop failed:", error);
    }

    try {
        appState.wakeCommandRecognition?.stop();
    } catch (error) {
        console.warn("Wake command stop failed:", error);
    }

    appState.wakeWordRecognition = null;
    appState.wakeCommandRecognition = null;
    updateWakeWordUi("Wake word idle");
}

function restartWakeWordModeSoon() {
    if (!appState.wakeWordEnabled) return;
    window.setTimeout(() => {
        if (appState.wakeWordEnabled && !appState.wakeWordRecognition) {
            startWakeWordMode();
        }
    }, 800);
}

function startWakeCommandCapture() {
    const recognition = createSpeechRecognition();
    if (!recognition) {
        updateWakeWordUi("Wake word supported only in browsers with speech recognition");
        return;
    }

    appState.wakeCommandRecognition = recognition;
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = getRecognitionLanguage();
    updateWakeWordUi("Wake word heard. Listening for command...");

    recognition.onresult = async (event) => {
        const text = event.results[0][0].transcript;
        updateWakeWordUi(`Command captured: ${text}`);
        await sendAssistantQuery(text, { source: "voice" });
    };

    recognition.onerror = () => {
        updateWakeWordUi("Wake command failed. Say computer again.");
    };

    recognition.onend = () => {
        appState.wakeCommandRecognition = null;
        if (appState.wakeWordEnabled) {
            updateWakeWordUi("Wake word armed. Say computer...");
            restartWakeWordModeSoon();
        }
    };

    try {
        recognition.start();
    } catch (error) {
        console.warn("Wake command recognition failed:", error);
        updateWakeWordUi("Could not capture wake command");
        restartWakeWordModeSoon();
    }
}

async function handleWakeWordHeard(commandText = "") {
    const now = Date.now();
    if (now < appState.wakeWordCooldownUntil) return;
    appState.wakeWordCooldownUntil = now + 3000;

    openTab("assistant");
    try {
        appState.wakeWordRecognition?.stop();
    } catch (error) {
        console.warn("Wake recognition stop after detection failed:", error);
    }
    appState.wakeWordRecognition = null;
    if (commandText) {
        updateWakeWordUi(`Wake word heard. Command: ${commandText}`);
        await sendAssistantQuery(commandText, { source: "voice" });
        restartWakeWordModeSoon();
        return;
    }
    startWakeCommandCapture();
}

function startWakeWordMode() {
    const recognition = createSpeechRecognition();
    if (!recognition) {
        updateWakeWordUi("Wake word is not supported in this browser");
        return;
    }

    appState.wakeWordEnabled = true;
    appState.wakeWordRecognition = recognition;

    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = getRecognitionLanguage();

    recognition.onresult = (event) => {
        const transcript = Array.from(event.results)
            .map((result) => result[0]?.transcript || "")
            .join(" ");
        updateWakeWordUi(`Heard: ${transcript || "..."}`);
        if (transcriptHasWakeWord(transcript)) {
            handleWakeWordHeard(extractCommandAfterWakeWord(transcript));
        }
    };

    recognition.onerror = () => {
        updateWakeWordUi("Wake word listener interrupted. Restarting...");
        appState.wakeWordRecognition = null;
        restartWakeWordModeSoon();
    };

    recognition.onend = () => {
        appState.wakeWordRecognition = null;
        if (appState.wakeWordEnabled && !appState.wakeCommandRecognition) {
            updateWakeWordUi("Wake word armed. Say computer...");
            restartWakeWordModeSoon();
        }
    };

    try {
        recognition.start();
        updateWakeWordUi("Wake word armed. Say computer...");
    } catch (error) {
        console.warn("Wake word start failed:", error);
        updateWakeWordUi("Wake word could not start");
    }
}

function toggleWakeWordMode() {
    if (appState.wakeWordEnabled) {
        stopWakeWordMode();
        return;
    }
    startWakeWordMode();
}

async function startLocalCamera() {
    const video = document.getElementById("local-camera");
    const serverFeed = document.getElementById("server-camera");
    const source = document.getElementById("camera-source");

    if (!video || !serverFeed) return;
    if (localCameraStream) return;

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setText("camera-status", "Browser camera API is not supported");
        return;
    }

    try {
        const preferredConstraints = {
            video: {
                facingMode: { ideal: "environment" },
                width: { ideal: 1280 },
                height: { ideal: 720 },
            },
            audio: false,
        };
        const fallbackConstraints = {
            video: true,
            audio: false,
        };
        localCameraStream = await navigator.mediaDevices.getUserMedia(preferredConstraints).catch(async () => {
            return navigator.mediaDevices.getUserMedia(fallbackConstraints);
        });
        video.srcObject = localCameraStream;
        video.hidden = false;
        serverFeed.hidden = true;
        if (source) source.value = "local";
        setText("camera-status", "Using local browser camera");
        setText("camera-last", "Local preview is running");
        setText("camera-stream-source", "local");
        setText("camera-active-label", "Browser preview");
    } catch (error) {
        setText("camera-status", "Camera permission denied or unavailable");
        console.warn("Local camera start failed:", error);
    }
}

function stopLocalCamera(updateSelect = true) {
    const video = document.getElementById("local-camera");
    const serverFeed = document.getElementById("server-camera");
    const source = document.getElementById("camera-source");

    if (localCameraStream) {
        localCameraStream.getTracks().forEach((track) => track.stop());
        localCameraStream = null;
    }

    if (video) {
        video.srcObject = null;
        video.hidden = true;
    }
    if (serverFeed) {
        serverFeed.hidden = false;
    }
    if (source && updateSelect) {
        source.value = "server";
    }
}

function switchCameraSource(event) {
    const nextValue = event?.target?.value || "server";
    if (nextValue === "local") {
        startLocalCamera();
        return;
    }

    stopLocalCamera(false);
    refreshServerCameraFeed();
    setText("camera-status", "Using server AI camera feed");
    setText("camera-stream-source", "server");
}

async function switchActiveServerCamera(cameraId) {
    try {
        const payload = await requestJson("/api/cameras/active", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({ camera_id: cameraId }),
        });

        appState.cameraProfiles = Array.isArray(payload.cameras) ? payload.cameras : appState.cameraProfiles;
        appState.activeCameraId = payload.active_camera_id || payload.active_camera?.id || cameraId;
        renderCameraSwitcher(appState.cameraProfiles, appState.activeCameraId);

        const source = document.getElementById("camera-source");
        if (source) source.value = "server";
        stopLocalCamera(false);
        refreshServerCameraFeed();
        setText("camera-active-label", payload.active_camera?.name || "--");
        setText("camera-status", payload.camera_status || "Selected camera");
        await fetchCameraAlert();
    } catch (error) {
        window.alert(error.message);
    }
}

async function cycleServerCamera(direction) {
    try {
        const payload = await requestJson("/api/cameras/active", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({ direction }),
        });

        appState.cameraProfiles = Array.isArray(payload.cameras) ? payload.cameras : appState.cameraProfiles;
        appState.activeCameraId = payload.active_camera_id || payload.active_camera?.id || appState.activeCameraId;
        renderCameraSwitcher(appState.cameraProfiles, appState.activeCameraId);

        const source = document.getElementById("camera-source");
        if (source) source.value = "server";
        stopLocalCamera(false);
        refreshServerCameraFeed();
        setText("camera-active-label", payload.active_camera?.name || "--");
        setText("camera-status", payload.camera_status || "Selected camera");
        await fetchCameraAlert();
    } catch (error) {
        window.alert(error.message);
    }
}

async function fetchCameras() {
    try {
        const payload = await requestJson("/api/cameras");
        appState.cameraProfiles = Array.isArray(payload.cameras) ? payload.cameras : [];
        appState.activeCameraId = payload.active_camera_id || payload.active_camera?.id || "";

        renderCameraSwitcher(appState.cameraProfiles, appState.activeCameraId);
        setText("camera-count", payload.camera_count ?? appState.cameraProfiles.length ?? 0, "0");
        if (!isLocalPreviewActive()) {
            setText("camera-active-label", payload.active_camera?.name || "--");
            setText("camera-status", payload.camera_status || "Waiting for camera");
        }
    } catch (error) {
        console.warn("Camera registry fetch failed:", error);
    }
}

async function fetchFaceRegistry() {
    try {
        const payload = await requestJson("/api/faces");
        const people = Array.isArray(payload.people) ? payload.people : [];
        const latestFaces = Array.isArray(payload.latest_faces) ? payload.latest_faces : [];
        const faceContainer = document.getElementById("known-face-list");
        if (faceContainer) {
            faceContainer.dataset.cachedFaces = JSON.stringify(people);
        }

        setText("known-people", payload.known_people ?? 0, "0");
        setText("face-recognition-enabled", payload.face_recognition_enabled ? "Yes" : "No");
        setText("face-recognition-status", payload.face_recognition_status || "Face recognition unavailable");
        renderKnownFaceList(people, latestFaces);
        setText("live-face-watch", buildFaceWatchSummary(latestFaces));
    } catch (error) {
        console.warn("Face registry fetch failed:", error);
    }
}

async function saveFaceRegistryEntry(event) {
    event.preventDefault();

    const nameInput = document.getElementById("face-person-name");
    const fileInput = document.getElementById("face-images");
    const personName = nameInput?.value.trim() || "";
    const files = fileInput?.files ? [...fileInput.files] : [];

    if (!personName) {
        setFaceFeedback("Enter a person name before training.");
        return;
    }

    if (files.length === 0) {
        setFaceFeedback("Choose at least one face image.");
        return;
    }

    const formData = new FormData();
    formData.append("name", personName);
    files.forEach((file) => {
        formData.append("images", file);
    });

    setFaceFeedback("Uploading and training known face samples...");

    try {
        const payload = await requestFormData("/api/faces", formData, {
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        setFaceFeedback(
            `Trained ${payload.person} with ${payload.saved_files?.length || 0} valid sample(s). ${payload.face_recognition_status || ""}`.trim(),
        );
        if (nameInput) nameInput.value = "";
        if (fileInput) fileInput.value = "";
        await fetchFaceRegistry();
        await fetchCameraAlert();
    } catch (error) {
        const extra = error.payload?.skip_messages?.join(", ");
        setFaceFeedback(extra ? `${error.message}. ${extra}` : error.message, true);
    }
}

async function deleteKnownFace(name) {
    const shouldDelete = window.confirm(`Delete face profile for ${name}?`);
    if (!shouldDelete) return;

    try {
        await requestJson(`/api/faces/${encodeURIComponent(name)}`, {
            method: "DELETE",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        setFaceFeedback(`${name} removed from the known roster.`);
        await fetchFaceRegistry();
        await fetchCameraAlert();
    } catch (error) {
        setFaceFeedback(error.message, true);
    }
}

async function retrainFaces() {
    setFaceFeedback("Retraining face roster...");
    try {
        const payload = await requestJson("/api/faces/retrain", {
            method: "POST",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        setFaceFeedback(payload.face_recognition_status || "Face roster retrained.");
        await fetchFaceRegistry();
        await fetchCameraAlert();
    } catch (error) {
        setFaceFeedback(error.message, true);
    }
}

function styleAvatarElement(element, initials, seed, avatarUrl = "") {
    if (!element) return;

    element.textContent = initials || "SA";
    element.style.backgroundImage = "";
    element.style.backgroundSize = "";
    element.style.backgroundPosition = "";
    element.style.color = "#071018";
    element.classList.remove("has-photo");

    if (typeof seed === "number") {
        element.style.background = `linear-gradient(135deg, hsl(${seed} 82% 62%), hsl(${(seed + 38) % 360} 86% 68%))`;
    }

    if (avatarUrl) {
        element.classList.add("has-photo");
        element.style.backgroundImage = `linear-gradient(135deg, rgba(8, 20, 28, 0.12), rgba(8, 20, 28, 0.12)), url("${avatarUrl}")`;
        element.style.backgroundSize = "cover";
        element.style.backgroundPosition = "center";
        element.style.color = "transparent";
    }
}

function applyAvatarSeed(initials, seed, avatarUrl = "") {
    [
        document.getElementById("profile-avatar"),
        document.getElementById("profile-preview-avatar"),
        document.getElementById("profile-modal-avatar"),
    ].forEach((element) => {
        styleAvatarElement(element, initials, seed, avatarUrl);
    });
}

function updateProfileSummary(profile) {
    const displayName = profile.display_name || profile.full_name || appState.currentUserName || appState.currentUser;
    const email = profile.email || appState.currentUser;

    appState.currentUserName = displayName;
    appState.currentRole = profile.role || appState.currentRole;

    setText("hero-user-name", displayName);
    setText("profile-name", displayName);
    setText("profile-email", email);
    setText("profile-role", profile.role || appState.currentRole);
    setText("current-role", profile.role || appState.currentRole);
    setText("profile-joined", profile.created_at ? formatTimestamp(profile.created_at) : "--");
    setText("profile-last-login", profile.last_login_at ? formatTimestamp(profile.last_login_at) : "No login recorded");
    setText("profile-activity", profile.activity_count ?? 0, "0");
    setText("profile-modal-name", displayName);

    setText("profile-view-visibility", formatVisibilityLabel(profile.profile_visibility));
    setText("profile-view-activity-visibility", formatVisibilityLabel(profile.activity_visibility));
    setText("profile-view-alert-opt-in", formatBooleanLabel(profile.alert_opt_in, "Opted in", "Muted"));
    setText("profile-view-face-opt-in", formatBooleanLabel(profile.face_enrollment_opt_in, "Allowed", "Disabled"));
    setText(
        "profile-privacy-summary",
        `${formatVisibilityLabel(profile.profile_visibility)} profile • ${formatVisibilityLabel(profile.activity_visibility)} activity`,
        "Profile and privacy settings are loading.",
    );

    const policy = profile.policy || {};
    setText("policy-retention", policy.retention_days ? `${policy.retention_days} days` : "--");
    setText("policy-access-scope", policy.access_scope || "--");
    setText("policy-audio", policy.audio_recording || "--");
    setText("policy-data-rights", policy.data_rights || "--");

    const fieldValues = {
        "profile-full-name": profile.full_name || displayName || "",
        "profile-bio": profile.bio || "",
        "profile-phone": profile.phone || "",
        "profile-location": profile.location || "",
    };
    Object.entries(fieldValues).forEach(([id, value]) => {
        const element = document.getElementById(id);
        if (element) {
            element.value = value;
        }
    });

    const profileVisibility = document.getElementById("profile-visibility");
    if (profileVisibility) profileVisibility.value = profile.profile_visibility || "team";
    const activityVisibility = document.getElementById("profile-activity-visibility");
    if (activityVisibility) activityVisibility.value = profile.activity_visibility || "admins";
    const alertOptIn = document.getElementById("profile-alert-opt-in");
    if (alertOptIn) alertOptIn.checked = Boolean(profile.alert_opt_in);
    const faceOptIn = document.getElementById("profile-face-opt-in");
    if (faceOptIn) faceOptIn.checked = Boolean(profile.face_enrollment_opt_in);
    const policyAck = document.getElementById("profile-policy-ack");
    if (policyAck) policyAck.checked = Boolean(profile.privacy_policy_acknowledged_at);

    applyAvatarSeed(profile.initials, profile.avatar_seed, profile.avatar_url);
}

async function fetchProfile() {
    try {
        const payload = await requestJson("/api/profile");
        updateProfileSummary(payload.profile || {});
    } catch (error) {
        console.warn("Profile fetch failed:", error);
    }
}

async function saveProfile(event) {
    event.preventDefault();
    const fullName = document.getElementById("profile-full-name")?.value.trim() || "";
    if (!fullName) {
        setProfileFeedback("Please enter your full name.", true);
        return;
    }

    const payload = {
        full_name: fullName,
        bio: document.getElementById("profile-bio")?.value.trim() || "",
        phone: document.getElementById("profile-phone")?.value.trim() || "",
        location: document.getElementById("profile-location")?.value.trim() || "",
        profile_visibility: document.getElementById("profile-visibility")?.value || "team",
        activity_visibility: document.getElementById("profile-activity-visibility")?.value || "admins",
        alert_opt_in: Boolean(document.getElementById("profile-alert-opt-in")?.checked),
        face_enrollment_opt_in: Boolean(document.getElementById("profile-face-opt-in")?.checked),
        privacy_policy_acknowledged: Boolean(document.getElementById("profile-policy-ack")?.checked),
    };

    try {
        setProfileFeedback("Saving profile...");
        const response = await requestJson("/api/profile", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(payload),
        });
        updateProfileSummary(response.profile || {});
        setProfileFeedback("Profile updated successfully.");
    } catch (error) {
        setProfileFeedback(error.message || "Unable to save profile.", true);
    }
}

async function uploadProfilePhoto(event) {
    event.preventDefault();
    const file = document.getElementById("profile-photo-input")?.files?.[0];
    if (!file) {
        setProfileFeedback("Please choose a photo first.", true);
        return;
    }

    const formData = new FormData();
    formData.append("photo", file);

    try {
        setProfileFeedback("Uploading photo...");
        const response = await requestFormData("/api/profile/photo", formData, {
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        updateProfileSummary(response.profile || {});
        setProfileFeedback("Profile photo updated.");
        const input = document.getElementById("profile-photo-input");
        if (input) input.value = "";
    } catch (error) {
        setProfileFeedback(error.message || "Unable to upload profile photo.", true);
    }
}

async function deleteProfilePhoto() {
    const shouldDelete = window.confirm("Delete your profile photo?");
    if (!shouldDelete) return;

    try {
        setProfileFeedback("Removing profile photo...");
        const response = await requestJson("/api/profile/photo", {
            method: "DELETE",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        updateProfileSummary(response.profile || {});
        setProfileFeedback("Profile photo removed.");
    } catch (error) {
        setProfileFeedback(error.message || "Unable to delete profile photo.", true);
    }
}

async function deleteProfile() {
    const shouldDelete = window.confirm("Delete this profile permanently?");
    if (!shouldDelete) return;

    try {
        const response = await requestJson("/api/profile", {
            method: "DELETE",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        window.location.href = response.redirect || "/login";
    } catch (error) {
        setProfileFeedback(error.message || "Unable to delete profile.", true);
    }
}

function setProfileModalOpen(isOpen) {
    const modal = document.getElementById("profile-modal");
    if (!modal) return;
    modal.hidden = !isOpen;
    document.body.classList.toggle("modal-open", isOpen);
}

function renderAutomationActions(actions = []) {
    const items = Array.isArray(actions)
        ? actions.map((action) => ({
            time: new Date().toISOString(),
            source: "automation",
            message: `${action.name} -> ${action.state}${action.reason ? ` • ${action.reason}` : ""}`,
        }))
        : [];
    renderAlertList("automation-actions", items, "No automation actions yet");
}

function updateModeCards(activeMode) {
    document.querySelectorAll(".mode-card").forEach((button) => {
        button.classList.toggle("active", button.dataset.controlMode === activeMode);
    });
}

function renderAutomation(snapshot = {}) {
    appState.automationSnapshot = snapshot;

    const environment = snapshot.environment || {};
    const thresholds = snapshot.thresholds || {};
    const defense = snapshot.defense || {};
    const activeDevices = Array.isArray(snapshot.active_devices) ? snapshot.active_devices : [];

    setText("system-mode-status", snapshot.mode_label || "Self monitoring");
    setText("automation-status", snapshot.status || "Loading automation status");
    setText("automation-risk", snapshot.runtime_risk || "normal");
    setText(
        "automation-last-evaluated",
        snapshot.last_evaluated_at ? formatTimestamp(snapshot.last_evaluated_at) : "Waiting for updates",
    );
    setText("automation-active-devices", activeDevices.length || 0, "0");
    setText(
        "automation-reasons",
        Array.isArray(snapshot.last_reasons) && snapshot.last_reasons.length > 0
            ? snapshot.last_reasons.join(" • ")
            : "No AI reason yet",
    );
    setText(
        "defense-status",
        defense.armed
            ? `Armed • ${formatBooleanLabel(defense.auto_alarm, "Auto alarm", "Alarm manual")}`
            : "Disarmed",
    );
    setText(
        "manual-mode-hint",
        snapshot.mode === "manual"
            ? "Manual operating keeps direct control with the operator. AI stays advisory."
            : "Self monitoring is active. AI can trigger lighting, cooling, alarm, and defense actions automatically.",
    );

    const environmentMappings = {
        "environment-temperature": environment.temperature_c ?? "",
        "environment-light": environment.ambient_light ?? "",
        "environment-humidity": environment.humidity ?? "",
        "threshold-temperature-high": thresholds.temperature_high_c ?? "",
        "threshold-temperature-low": thresholds.temperature_low_c ?? "",
        "threshold-light-low": thresholds.ambient_light_low ?? "",
        "threshold-light-high": thresholds.ambient_light_high ?? "",
    };
    Object.entries(environmentMappings).forEach(([id, value]) => {
        const element = document.getElementById(id);
        if (element) {
            element.value = value;
        }
    });

    const riskField = document.getElementById("environment-risk");
    if (riskField) riskField.value = environment.security_risk || "normal";
    const defenseArmed = document.getElementById("defense-armed");
    if (defenseArmed) defenseArmed.checked = Boolean(defense.armed);
    const defenseAlarm = document.getElementById("defense-auto-alarm");
    if (defenseAlarm) defenseAlarm.checked = Boolean(defense.auto_alarm);
    const defenseMode = document.getElementById("defense-auto-defense");
    if (defenseMode) defenseMode.checked = Boolean(defense.auto_defense);

    const policy = snapshot.policy || {};
    setText("policy-retention", policy.retention_days ? `${policy.retention_days} days` : "--");
    setText("policy-access-scope", policy.access_scope || "--");
    setText("policy-audio", policy.audio_recording || "--");
    setText("policy-data-rights", policy.data_rights || "--");

    updateModeCards(snapshot.mode || "self_monitoring");
    renderAutomationActions(snapshot.last_actions || []);
}

async function fetchAutomation() {
    try {
        const payload = await requestJson("/api/automation");
        renderAutomation(payload);
    } catch (error) {
        console.warn("Automation fetch failed:", error);
    }
}

async function saveAutomation(event) {
    event.preventDefault();

    const payload = {
        mode: appState.automationSnapshot?.mode || "self_monitoring",
        environment: {
            temperature_c: document.getElementById("environment-temperature")?.value,
            ambient_light: document.getElementById("environment-light")?.value,
            humidity: document.getElementById("environment-humidity")?.value,
            security_risk: document.getElementById("environment-risk")?.value,
        },
        thresholds: {
            temperature_high_c: document.getElementById("threshold-temperature-high")?.value,
            temperature_low_c: document.getElementById("threshold-temperature-low")?.value,
            ambient_light_low: document.getElementById("threshold-light-low")?.value,
            ambient_light_high: document.getElementById("threshold-light-high")?.value,
        },
        defense: {
            armed: Boolean(document.getElementById("defense-armed")?.checked),
            auto_alarm: Boolean(document.getElementById("defense-auto-alarm")?.checked),
            auto_defense: Boolean(document.getElementById("defense-auto-defense")?.checked),
        },
    };

    try {
        setAutomationFeedback("Saving automation settings...");
        const response = await requestJson("/api/automation", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(payload),
        });
        renderAutomation(response);
        setAutomationFeedback("Automation settings saved.");
        await Promise.all([fetchHealth(), fetchDevices(), fetchSmartAlerts()]);
    } catch (error) {
        setAutomationFeedback(error.message || "Unable to save automation settings.", true);
    }
}

async function setAutomationMode(mode) {
    try {
        const response = await requestJson("/api/automation", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({ mode }),
        });
        renderAutomation(response);
        setAutomationFeedback(`${response.mode_label || "Automation"} is active.`);
        await Promise.all([fetchHealth(), fetchDevices(), fetchSmartAlerts()]);
    } catch (error) {
        setAutomationFeedback(error.message || "Unable to switch automation mode.", true);
    }
}

function renderAdminUsers(users) {
    const container = document.getElementById("admin-users");
    if (!container) return;

    container.innerHTML = "";

    if (!Array.isArray(users) || users.length === 0) {
        container.textContent = "No users found";
        return;
    }

    users.forEach((user) => {
        const row = document.createElement("div");
        row.className = "admin-user-row";

        const avatar = document.createElement("div");
        avatar.className = "mini-avatar";
        avatar.textContent = user.initials || "SA";
        if (typeof user.avatar_seed === "number") {
            avatar.style.background = `linear-gradient(135deg, hsl(${user.avatar_seed} 82% 62%), hsl(${(user.avatar_seed + 30) % 360} 78% 68%))`;
        }

        const meta = document.createElement("div");
        meta.className = "admin-user-meta";

        const displayName = document.createElement("strong");
        displayName.textContent = user.display_name || user.email;

        const details = document.createElement("span");
        details.className = "panel-note";
        details.textContent = `${user.email} • ${user.last_login_at ? `Last login ${formatTimestamp(user.last_login_at)}` : "No login yet"}`;

        meta.appendChild(displayName);
        meta.appendChild(details);

        const controls = document.createElement("div");
        controls.className = "admin-user-controls";

        const roleBadge = document.createElement("span");
        roleBadge.className = `role-pill role-pill-${user.role}`;
        roleBadge.textContent = user.role;

        const select = document.createElement("select");
        select.className = "inline-select role-select";
        select.innerHTML = `
            <option value="admin">admin</option>
            <option value="user">user</option>
        `;
        select.value = user.role;
        select.disabled = user.email === appState.currentUser;
        select.addEventListener("change", async () => {
            await updateUserRole(user.email, select.value, roleBadge, select);
        });

        controls.appendChild(roleBadge);
        controls.appendChild(select);

        row.appendChild(avatar);
        row.appendChild(meta);
        row.appendChild(controls);
        container.appendChild(row);
    });
}

function renderAdminActivity(items) {
    const container = document.getElementById("admin-activity");
    if (!container) return;

    container.innerHTML = "";

    if (!Array.isArray(items) || items.length === 0) {
        container.textContent = "No activity yet";
        return;
    }

    items.slice(0, 20).forEach((item) => {
        const row = document.createElement("p");
        const actor = item.actor_email || "system";
        const source = item.source ? ` [${item.source}]` : "";
        const target = item.target_name ? ` -> ${item.target_name}` : "";
        row.textContent = `${formatTimestamp(item.created_at)} - ${actor}${source} - ${item.action}${target}`;
        container.appendChild(row);
    });
}

async function updateUserRole(email, role, roleBadge, select) {
    const previousRole = roleBadge?.textContent || select?.value || "user";

    try {
        const payload = await requestJson(`/api/admin/users/${encodeURIComponent(email)}/role`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({ role }),
        });
        const updatedRole = payload.user?.role || role;
        if (roleBadge) {
            roleBadge.textContent = updatedRole;
            roleBadge.className = `role-pill role-pill-${updatedRole}`;
        }
        if (select) {
            select.value = updatedRole;
        }
        await fetchAdminSummary();
    } catch (error) {
        window.alert(error.message);
        if (select) {
            select.value = previousRole;
        }
    }
}

async function fetchAdminSummary() {
    if (!appState.isAdmin) return;

    try {
        const data = await requestJson("/api/admin/summary");
        setText("admin-user-count", data.user_count);
        setText("admin-admin-count", data.admin_count);
        setText("admin-device-count", data.device_count);
        setText("admin-db-path", data.database_path);
        renderAdminUsers(data.users || []);
        renderAdminActivity(data.activity || []);
    } catch (error) {
        console.warn("Admin summary fetch failed:", error);
    }
}

async function refreshOperationalViews() {
    await Promise.all([
        fetchHealth(),
        fetchAutomation(),
        fetchCameras(),
        fetchCameraAlert(),
        fetchFaceRegistry(),
        fetchSmartAlerts(),
        fetchDevices(),
        fetchProfile(),
        appState.isAdmin ? fetchAdminSummary() : Promise.resolve(),
    ]);
}

function bindEvents() {
    document.querySelectorAll(".tab-btn").forEach((button) => {
        button.addEventListener("click", () => openTab(button.dataset.tab));
    });
    document.querySelectorAll(".mode-card").forEach((button) => {
        button.addEventListener("click", async () => {
            const mode = button.dataset.controlMode;
            if (!mode) return;
            await setAutomationMode(mode);
        });
    });

    document.getElementById("refresh-health")?.addEventListener("click", fetchHealth);
    document.getElementById("refresh-alerts")?.addEventListener("click", async () => {
        await fetchCameraAlert();
        await fetchSmartAlerts();
    });
    document.getElementById("micBtn")?.addEventListener("click", startListening);
    document.getElementById("wake-word-toggle")?.addEventListener("click", toggleWakeWordMode);
    document.getElementById("start-local-camera")?.addEventListener("click", startLocalCamera);
    document.getElementById("stop-local-camera")?.addEventListener("click", () => {
        stopLocalCamera();
        setText("camera-status", "Using server AI camera feed");
        setText("camera-stream-source", "server");
        refreshServerCameraFeed();
    });
    document.getElementById("camera-add-form")?.addEventListener("submit", addServerCamera);
    document.getElementById("camera-source")?.addEventListener("change", switchCameraSource);
    document.getElementById("camera-prev")?.addEventListener("click", async () => {
        await cycleServerCamera("previous");
    });
    document.getElementById("camera-next")?.addEventListener("click", async () => {
        await cycleServerCamera("next");
    });
    document.getElementById("camera-refresh")?.addEventListener("click", refreshServerCameraFeed);
    document.getElementById("mobile-camera-register")?.addEventListener("click", async () => {
        await setupMobileCameraBridge(false);
    });
    document.getElementById("mobile-camera-link")?.addEventListener("click", async () => {
        await setupMobileCameraBridge(true);
    });
    document.getElementById("server-camera-select")?.addEventListener("change", async (event) => {
        if (!event.target.value) return;
        await switchActiveServerCamera(event.target.value);
    });
    
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
        document.body.classList.toggle("light-mode");
    });
    
    document.getElementById("assistant-mode")?.addEventListener("change", (event) => {
        setText("assistantModeStatus", formatAssistantMode(event.target.value), "Hybrid");
    });
    document.getElementById("automation-form")?.addEventListener("submit", saveAutomation);
    document.getElementById("profile-photo-form")?.addEventListener("submit", uploadProfilePhoto);
    document.getElementById("delete-profile-photo-btn")?.addEventListener("click", deleteProfilePhoto);
    document.getElementById("profile-form")?.addEventListener("submit", saveProfile);
    document.getElementById("delete-profile-btn")?.addEventListener("click", deleteProfile);
    document.getElementById("face-form")?.addEventListener("submit", saveFaceRegistryEntry);
    document.getElementById("face-retrain")?.addEventListener("click", retrainFaces);
    document.getElementById("open-profile-modal")?.addEventListener("click", () => setProfileModalOpen(true));
    document.getElementById("open-profile-modal-secondary")?.addEventListener("click", () => setProfileModalOpen(true));
    document.getElementById("side-open-profile-modal")?.addEventListener("click", () => setProfileModalOpen(true));
    document.getElementById("close-profile-modal")?.addEventListener("click", () => setProfileModalOpen(false));
    document.querySelectorAll("[data-close-profile-modal]").forEach((element) => {
        element.addEventListener("click", () => setProfileModalOpen(false));
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            setProfileModalOpen(false);
        }
    });

    document.querySelectorAll(".assistant-quick-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            const query = button.dataset.query;
            if (!query) return;
            await sendAssistantQuery(query);
        });
    });

    document.getElementById("assistant-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const input = document.getElementById("assistant-input");
        const query = input?.value.trim();
        if (!query) return;
        await sendAssistantQuery(query);
        if (input) input.value = "";
    });

    document.getElementById("add-device-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const input = document.getElementById("new-device-name");
        const name = input?.value.trim();
        if (!name) {
            window.alert("Enter a device name");
            return;
        }
        await addDevice(name);
        if (input) input.value = "";
    });
}

function startPolling() {
    refreshOperationalViews();

    window.setInterval(() => {
        refreshOperationalViews();
    }, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
    ensureAssistantIntro();
    setText("assistantModeStatus", formatAssistantMode(getAssistantMode()), "Hybrid");
    setAssistantActionText("No manual action yet");
    setProfileFeedback("");
    setAutomationFeedback("");
    setMobileCameraFeedback("");
    updateWakeWordUi("Wake word idle");
    bindEvents();
    startPolling();
});

window.addEventListener("beforeunload", () => {
    stopWakeWordMode();
    stopLocalCamera(false);
});
