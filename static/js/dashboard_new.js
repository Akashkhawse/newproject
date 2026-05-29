class SmartAIDashboard {
  constructor() {
    this.isAdmin = document.body.dataset.isAdmin === "1";
    this.refreshIntervalMs = 5000;
    this.timer = null;
    this.clockTimer = null;
    this.videoAgentEnabled = false;
    this.lastVideoAgentSpeech = "";
    this.lastVideoAgentSpokenAt = 0;
    this.init();
  }

  init() {
    this.bindActions();
    this.bindNavigation();
    this.startClock();
    this.refresh();
    this.timer = window.setInterval(() => this.refresh(), this.refreshIntervalMs);
  }

  async requestJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && payload.redirect) {
      window.location.href = payload.redirect;
      throw new Error("Session expired.");
    }
    if (!response.ok) {
      throw new Error(payload.error || `Request failed (${response.status})`);
    }
    return payload;
  }

  bindActions() {
    document.getElementById("refresh-dashboard")?.addEventListener("click", () => this.refresh(true));
    document.getElementById("device-list")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-device-name]");
      if (button) this.toggleDevice(button.dataset.deviceName);
    });
    document.getElementById("assistant-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.askAssistant();
    });
    document.getElementById("search-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.searchRecords();
    });
    document.getElementById("discover-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.runDiscover();
    });
    document.getElementById("journey-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.loadJourney();
    });
    document.getElementById("intelligence")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-intelligence-action]");
      if (button) this.updateCameraIntelligence(button.dataset.intelligenceAction);
    });
    document.getElementById("access-control")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-access-event]");
      if (button) this.createAccessEvent(button.dataset.accessEvent);
    });
    document.getElementById("video-agent-toggle")?.addEventListener("click", () => this.toggleVideoAgent());
    document.getElementById("video-agent-speak")?.addEventListener("click", () => this.speakVideoAgent(true));
  }

  bindNavigation() {
    const links = [...document.querySelectorAll("[data-section-link]")];
    document.querySelectorAll('a[href^="#"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        const targetId = link.getAttribute("href")?.slice(1);
        const target = targetId ? document.getElementById(targetId) : null;
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        links.forEach((item) => {
          item.classList.toggle("active", item.dataset.sectionLink === targetId);
        });
        if (window.history?.replaceState) {
          window.history.replaceState(null, "", `#${targetId}`);
        }
      });
    });

    const sections = links
      .map((link) => document.getElementById(link.dataset.sectionLink))
      .filter(Boolean);
    const root = document.getElementById("dashboard-scroll");
    if (!root || !("IntersectionObserver" in window)) return;

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        links.forEach((link) => {
          link.classList.toggle("active", link.dataset.sectionLink === entry.target.id);
        });
      });
    }, { root, threshold: 0.35 });
    sections.forEach((section) => observer.observe(section));
  }

  async refresh(announce = false) {
    const requests = [
      this.loadHealth(),
      this.loadCameras(),
      this.loadAlerts(),
      this.loadIncidents(),
      this.loadVision(),
      this.loadAnalytics(),
      this.loadCameraIntelligence(),
      this.loadAccessControl(),
      this.loadVideoAgent(),
      this.loadAutomation(),
      this.loadDevices(),
      this.loadSecurity(),
    ];
    if (this.isAdmin) requests.push(this.loadAdmin());
    await Promise.allSettled(requests);
    if (announce) this.notify("Dashboard updated.");
  }

  async loadHealth() {
    const data = await this.requestJson("/health");
    this.text("cpu-value", this.percent(data.cpu_percent));
    this.text("memory-value", this.percent(data.memory));
    this.text("device-value", `${data.active_device_count || 0}/${data.device_count || 0}`);
    this.text("camera-value", data.camera_count ?? "--");
    this.text("open-incident-value", data.open_incidents ?? "--");
    this.text("incident-count", `${data.open_incidents ?? "--"} open`);
    this.text("risk-score-value", data.risk_score ?? "--");
    this.text("hero-risk-score", data.risk_score ?? "--");
    this.text("hero-open-incidents", data.open_incidents ?? "--");
    this.text("hero-camera-count", data.camera_count ?? "--");
    this.text("hero-active-devices", `${data.active_device_count || 0}/${data.device_count || 0}`);
    this.text("risk-level-label", this.humanize(data.risk_level || "normal"));
    this.text("system-status", data.alert || "Operational");
    this.text("yolo-status", data.yolo_status || (data.yolo_enabled ? "Enabled" : "Disabled"));
    this.text("face-status", data.face_recognition_status || (data.face_recognition_enabled ? "Enabled" : "Disabled"));
    this.text("risk-updated", data.time || "--");
    this.text("quick-uptime", data.uptime || "--");
    this.text("health-storage", this.percent(data.disk));
    this.text("health-api-status", "Online");
    this.text("system-live-badge", "Online");
    this.text("header-live-state", data.alert && data.alert !== "✅ Normal" ? "Attention needed" : "Live monitoring");
  }

  async loadCameras() {
    const data = await this.requestJson("/api/cameras");
    const cameras = Array.isArray(data.cameras) ? data.cameras : Array.isArray(data.list) ? data.list : [];
    const active = data.active_camera || {};
    this.text("active-camera-name", active.name || "No camera selected");
    this.text("active-camera-source", active.source_display || "No source");
    this.text("camera-roster-count", `${cameras.length} configured`);
    this.text("camera-stream-detail", data.camera_available ? "Online" : data.camera_status || "Waiting");
    this.text("hero-camera-state", data.camera_available ? "online" : "standby");

    const container = document.getElementById("camera-list");
    if (!container) return;
    if (!cameras.length) {
      container.innerHTML = '<p class="empty-state">No camera profiles configured.</p>';
      return;
    }
    container.innerHTML = cameras.slice(0, 6).map((camera) => `
      <article class="registry-row">
        <span class="registry-dot ${camera.id === active.id ? "active" : ""}"></span>
        <div>
          <strong>${this.escape(camera.name || "Camera")}</strong>
          <p>${this.escape(camera.transport || camera.type || "source")}</p>
        </div>
      </article>
    `).join("");
  }

  async loadAlerts() {
    const data = await this.requestJson("/alerts");
    const items = Array.isArray(data.items) ? data.items.slice(0, 5) : [];
    this.text("alert-count", `${data.summary?.total ?? items.length} alerts`);
    const latest = data.latest_camera_alert || items[0] || {};
    this.text("hero-alert-summary", latest.message || latest.alert || "No alerts");
    const container = document.getElementById("alert-feed");
    if (!container) return;
    container.innerHTML = items.length ? items.map((item) => `
      <article class="alert-item">
        <span class="severity-mark ${this.escape(item.level || "info")}"></span>
        <div class="alert-content">
          <strong>${this.escape(item.message || item.title || "System event")}</strong>
          <p>${this.escape(item.created_at || item.time || "Recent event")}</p>
        </div>
      </article>
    `).join("") : '<p class="empty-state">No active alerts.</p>';
  }

  async loadIncidents() {
    const data = await this.requestJson("/api/incidents?limit=6");
    const items = Array.isArray(data.items) ? data.items : [];
    const container = document.getElementById("incident-list");
    if (!container) return;
    container.innerHTML = items.length ? items.map((item) => `
      <article class="incident-row">
        <div>
          <strong>${this.escape(item.title || "Incident")}</strong>
          <p>${this.escape(item.created_at || "")}</p>
        </div>
        <span class="severity-pill ${this.escape(item.severity || "info")}">${this.escape(item.severity || "info")}</span>
      </article>
    `).join("") : '<p class="empty-state">No incidents recorded.</p>';
  }

  async loadVision() {
    const data = await this.requestJson("/api/telemetry/vision?limit=10");
    const objects = Array.isArray(data.objects) ? data.objects : [];
    const container = document.getElementById("detection-feed");
    if (!container) return;
    container.innerHTML = objects.length ? objects.slice(0, 8).map((item) => `
      <article class="detection-item">
        <span class="detection-label">${this.escape(item.label || item.name || "Object")}</span>
        <span class="detection-confidence">${this.escape(this.detectionValue(item))}</span>
      </article>
    `).join("") : '<p class="empty-state">No objects detected in the active frame.</p>';
  }

  async loadAnalytics() {
    const data = await this.requestJson("/api/analytics/summary");
    const risk = data.risk || {};
    const recommendations = Array.isArray(risk.recommendations) ? risk.recommendations : [];
    const reasons = Array.isArray(risk.reasons) ? risk.reasons : [];
    this.text("analytics-updated", this.formatTime(data.time));
    this.text("analytics-risk-level", `${this.humanize(risk.level || "normal")} risk`);
    this.text("analytics-risk-reasons", reasons.length ? reasons.join(" • ") : "No active risk reasons.");
    this.text("risk-ring", risk.score ?? "--");
    this.setRiskTone(risk.level || "normal");

    const container = document.getElementById("recommendation-list");
    if (!container) return;
    container.innerHTML = recommendations.length ? recommendations.map((item) => `
      <article class="recommendation-item">
        <span></span>
        <p>${this.escape(item)}</p>
      </article>
    `).join("") : '<p class="empty-state">No recommendations right now.</p>';
  }

  async runDiscover() {
    const query = document.getElementById("discover-query")?.value.trim() || "";
    const container = document.getElementById("discover-results");
    if (!query || !container) return;
    container.innerHTML = '<p class="empty-state">Searching security timeline...</p>';
    try {
      const data = await this.requestJson("/api/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 12 }),
      });
      this.text("discover-count", `${data.total || 0} results`);
      const items = Array.isArray(data.items) ? data.items : [];
      container.innerHTML = items.length ? items.map((item) => `
        <article class="discover-item ${this.escape(item.severity || "info")}">
          <div>
            <strong>${this.escape(item.title || "Security record")}</strong>
            <p>${this.escape(item.subtitle || item.type || "")}</p>
          </div>
          <span>${this.escape(this.formatTime(item.time))}</span>
        </article>
      `).join("") : '<p class="empty-state">No matching records found.</p>';
    } catch (error) {
      container.textContent = error.message;
    }
  }

  async loadJourney() {
    const target = document.getElementById("journey-target")?.value.trim() || "person";
    const container = document.getElementById("journey-list");
    if (!container) return;
    try {
      const data = await this.requestJson(`/api/journeys?target=${encodeURIComponent(target)}&limit=12`);
      const path = Array.isArray(data.path) ? data.path : [];
      this.text("journey-count", `${data.total || 0} points`);
      container.innerHTML = path.length ? path.map((item, index) => `
        <article class="journey-item">
          <span>${index + 1}</span>
          <div>
            <strong>${this.escape(item.camera_name || item.camera_id || "Camera")}</strong>
            <p>${this.escape(this.formatTime(item.time))} - ${this.escape((item.matches || []).length)} match(es)</p>
          </div>
        </article>
      `).join("") : '<p class="empty-state">No movement path found yet.</p>';
    } catch (error) {
      container.textContent = error.message;
    }
  }

  async loadAccessControl() {
    const data = await this.requestJson("/api/access-control");
    const events = Array.isArray(data.events) ? data.events : [];
    this.text("access-state", data.lockdown ? "Lockdown" : `${data.doors?.length || 0} doors`);
    const container = document.getElementById("access-list");
    if (!container) return;
    container.innerHTML = events.length ? events.slice(0, 6).map((event) => `
      <article class="access-item ${this.escape(event.severity || "info")}">
        <div>
          <strong>${this.escape(this.humanize(event.event_type || "access"))}</strong>
          <p>${this.escape(event.door_name || "Door")} - ${this.escape(event.actor || "Unknown")}</p>
        </div>
        <span>${this.escape(this.formatTime(event.created_at))}</span>
      </article>
    `).join("") : '<p class="empty-state">No access events recorded.</p>';
  }

  async loadVideoAgent() {
    const data = await this.requestJson("/api/video-agent/status?lang=hinglish");
    this.videoAgentSnapshot = data;
    this.text("video-agent-speech", data.speech_text || "Video agent has no scene summary yet.");
    this.text("agent-behavior-label", this.humanize(data.behavior_label || "idle"));
    this.text("agent-risk-label", `Risk ${this.humanize(data.risk?.level || "normal")} - score ${data.risk?.score ?? "--"}`);
    this.text("video-agent-state", this.videoAgentEnabled ? "Voice live" : "Listening off");
    this.setAgentTone(data.risk?.level || "normal", data.behavior_label || "idle");

    if (this.videoAgentEnabled && data.should_speak) {
      this.speakVideoAgent(false);
    }
  }

  async createAccessEvent(eventType) {
    try {
      await this.requestJson("/api/access-control/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType, actor: "Operator simulation" }),
      });
      await Promise.allSettled([this.loadAccessControl(), this.loadIncidents(), this.loadAnalytics()]);
      this.notify("Access event linked with video context.");
    } catch (error) {
      this.notify(error.message, true);
    }
  }

  async loadAutomation() {
    const data = await this.requestJson("/api/automation");
    this.text("automation-mode", data.mode_label || data.mode || "--");
    this.text("automation-status", data.status || "No status available.");
    this.text("automation-risk", data.runtime_risk || "--");
    this.text("automation-armed", data.defense?.armed ? "Armed" : "Disarmed");
    this.text("automation-evaluated", this.formatTime(data.last_evaluated_at));
  }

  async loadCameraIntelligence() {
    const data = await this.requestJson("/api/camera-intelligence");
    const state = data.intelligence || {};
    const detection = state.detection || {};
    const activeDetection = Object.entries(detection)
      .filter(([, enabled]) => Boolean(enabled))
      .map(([name]) => name)
      .slice(0, 4);
    const patrol = state.patrol || {};
    const quiet = state.quiet_hours || {};

    this.cameraIntelligence = state;
    this.text("intelligence-mode", state.privacy_mode ? "Private" : "Monitoring");
    this.text("intelligence-privacy", state.privacy_mode ? "Enabled" : "Disabled");
    this.text("intelligence-sensitivity", `${state.sensitivity ?? "--"}%`);
    this.text("intelligence-detections", activeDetection.length ? activeDetection.join(", ") : "None");
    this.text("intelligence-patrol", patrol.enabled ? `${this.humanize(patrol.preset)} every ${patrol.interval_seconds}s` : "Disabled");
    this.text("intelligence-quiet", quiet.enabled ? `${quiet.start} to ${quiet.end}` : "Disabled");
    this.text("live-ai-policy", state.privacy_mode ? "Privacy mode" : patrol.enabled ? "Patrol active" : "Monitoring");
    this.renderZones(state.activity_zones || []);
  }

  async updateCameraIntelligence(action) {
    const state = this.cameraIntelligence || {};
    const payload = {};
    if (action === "privacy") {
      payload.privacy_mode = !state.privacy_mode;
    } else if (action === "patrol") {
      payload.patrol = { ...(state.patrol || {}), enabled: !(state.patrol || {}).enabled };
    } else if (action === "sensitivity") {
      const current = Number(state.sensitivity || 65);
      payload.sensitivity = current >= 90 ? 55 : current + 10;
      payload.detection = { ...(state.detection || {}), motion: true, person: true, face: true };
    } else if (action === "quiet") {
      payload.quiet_hours = { ...(state.quiet_hours || {}), enabled: !(state.quiet_hours || {}).enabled };
    } else {
      return;
    }

    try {
      this.setIntelligenceBusy(true);
      const data = await this.requestJson("/api/camera-intelligence", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      this.cameraIntelligence = data.intelligence || {};
      await this.loadCameraIntelligence();
      this.restartCameraFeed();
      this.notify("Camera AI policy updated.");
    } catch (error) {
      this.notify(error.message, true);
    } finally {
      this.setIntelligenceBusy(false);
    }
  }

  async loadDevices() {
    const data = await this.requestJson("/api/devices");
    const devices = Array.isArray(data.devices) ? data.devices : [];
    const container = document.getElementById("device-list");
    if (!container) return;
    container.innerHTML = devices.length ? devices.map((device) => {
      const name = String(device.name || "Device");
      const on = device.state === "ON";
      return `
        <article class="device-item">
          <div class="device-info">
            <div class="device-icon">${this.escape(name.slice(0, 1).toUpperCase())}</div>
            <div class="device-details">
              <h4>${this.escape(name)}</h4>
              <p class="device-status">${on ? "On" : "Off"}</p>
            </div>
          </div>
          <button class="toggle-switch ${on ? "on" : ""}" type="button" data-device-name="${this.escape(name)}" aria-label="Toggle ${this.escape(name)}" aria-pressed="${on}">
            <span class="toggle-knob"></span>
          </button>
        </article>
      `;
    }).join("") : '<p class="empty-state">No response devices configured.</p>';
  }

  async loadSecurity() {
    const data = await this.requestJson("/api/security/status");
    const container = document.getElementById("security-details");
    if (!container) return;
    container.innerHTML = `
      <div class="status-line"><span class="status-label">Two-factor setting</span><span class="status-value">${data.two_factor_enabled ? "Enabled" : "Disabled"}</span></div>
      <div class="status-line"><span class="status-label">Last login IP</span><span class="status-value mono">${this.escape(data.last_login_ip || "Not recorded")}</span></div>
      <div class="status-line"><span class="status-label">Session</span><span class="status-value">${data.session_issued_at ? "Active" : "Not recorded"}</span></div>
    `;
  }

  async loadAdmin() {
    const data = await this.requestJson("/api/admin/summary");
    this.text("admin-users", data.user_count ?? "--");
    this.text("admin-count", data.admin_count ?? "--");
    this.text("admin-devices", data.device_count ?? "--");
    this.text("admin-active-devices", data.active_device_count ?? "--");
  }

  async toggleDevice(deviceName) {
    try {
      await this.requestJson(`/toggle/${encodeURIComponent(deviceName)}`, { method: "POST" });
      await this.loadDevices();
      this.notify(`${deviceName} state updated.`);
    } catch (error) {
      this.notify(error.message, true);
    }
  }

  async askAssistant() {
    const input = document.getElementById("assistant-query");
    const output = document.getElementById("assistant-reply");
    const query = input?.value.trim() || "";
    if (!query || !output) return;
    output.textContent = "Preparing response...";
    try {
      const data = await this.requestJson("/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, mode: "hybrid" }),
      });
      output.textContent = data.reply || "No response received.";
      input.value = "";
      this.loadDevices();
    } catch (error) {
      output.textContent = error.message;
    }
  }

  toggleVideoAgent() {
    this.videoAgentEnabled = !this.videoAgentEnabled;
    const button = document.getElementById("video-agent-toggle");
    if (button) button.textContent = this.videoAgentEnabled ? "Disable voice agent" : "Enable voice agent";
    this.text("video-agent-state", this.videoAgentEnabled ? "Voice live" : "Listening off");
    if (this.videoAgentEnabled) this.speakVideoAgent(true);
  }

  speakVideoAgent(force = false) {
    const snapshot = this.videoAgentSnapshot || {};
    const text = String(snapshot.speech_text || "").trim();
    if (!text || !("speechSynthesis" in window)) return;
    const now = Date.now();
    if (!force && text === this.lastVideoAgentSpeech && now - this.lastVideoAgentSpokenAt < 20000) return;
    if (!force && now - this.lastVideoAgentSpokenAt < 12000) return;

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 0.95;
    utterance.pitch = 1.02;
    utterance.lang = "en-IN";
    window.speechSynthesis.speak(utterance);
    this.lastVideoAgentSpeech = text;
    this.lastVideoAgentSpokenAt = now;
  }

  async searchRecords() {
    const query = document.getElementById("search-query")?.value.trim() || "";
    const output = document.getElementById("search-result");
    if (!query || !output) return;
    try {
      const data = await this.requestJson(`/api/search?q=${encodeURIComponent(query)}`);
      output.textContent = `${data.total || 0} results found for "${query}".`;
    } catch (error) {
      output.textContent = error.message;
    }
  }

  notify(message, error = false) {
    const notice = document.createElement("div");
    notice.className = `toast ${error ? "error" : ""}`;
    notice.textContent = message;
    document.body.appendChild(notice);
    window.setTimeout(() => notice.remove(), 2800);
  }

  startClock() {
    const update = () => {
      const now = new Date();
      this.text("live-clock", now.toLocaleString());
    };
    update();
    this.clockTimer = window.setInterval(update, 1000);
  }

  text(id, value) {
    const target = document.getElementById(id);
    if (target) target.textContent = String(value ?? "--");
  }

  percent(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${Math.round(number)}%` : "--";
  }

  detectionValue(item) {
    const confidence = Number(item.confidence);
    if (Number.isFinite(confidence)) return `${Math.round(confidence * 100)}%`;
    return item.count ? `x${item.count}` : "Active";
  }

  formatTime(value) {
    if (!value) return "--";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  renderZones(zones) {
    const container = document.getElementById("zone-preview");
    if (!container) return;
    if (!zones.length) {
      container.innerHTML = '<p class="empty-state">No activity zones configured.</p>';
      return;
    }
    container.innerHTML = zones.map((zone) => `
      <article class="zone-card ${zone.enabled ? "active" : ""}">
        <div>
          <strong>${this.escape(zone.name || "Zone")}</strong>
          <p>${zone.enabled ? "Watching" : "Paused"} - x${zone.x} y${zone.y} w${zone.w} h${zone.h}</p>
        </div>
        <span>${zone.enabled ? "Active" : "Off"}</span>
      </article>
    `).join("");
  }

  humanize(value) {
    return String(value || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  setRiskTone(level) {
    const ring = document.getElementById("risk-ring");
    if (!ring) return;
    ring.className = `risk-ring ${String(level || "normal").toLowerCase()}`;
  }

  setAgentTone(level, behavior) {
    const orb = document.getElementById("agent-orb");
    if (!orb) return;
    orb.className = `agent-orb ${String(level || "normal").toLowerCase()} ${String(behavior || "idle").toLowerCase()}`;
  }

  restartCameraFeed() {
    const feed = document.querySelector(".camera-feed");
    if (!feed) return;
    feed.src = `/camera_feed?ts=${Date.now()}`;
  }

  setIntelligenceBusy(isBusy) {
    document.querySelectorAll("[data-intelligence-action]").forEach((button) => {
      button.disabled = Boolean(isBusy);
      button.setAttribute("aria-busy", isBusy ? "true" : "false");
    });
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character]));
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.smartAIDashboard = new SmartAIDashboard();
});
