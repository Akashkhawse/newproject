class SmartAIDashboard {
  constructor() {
    this.isAdmin = document.body.dataset.isAdmin === "1";
    this.refreshIntervalMs = 5000;
    this.timer = null;
    this.init();
  }

  init() {
    this.bindActions();
    this.bindNavigation();
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
  }

  bindNavigation() {
    const links = [...document.querySelectorAll("[data-section-link]")];
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
    this.text("system-status", data.alert || "Operational");
    this.text("yolo-status", data.yolo_status || (data.yolo_enabled ? "Enabled" : "Disabled"));
    this.text("face-status", data.face_recognition_status || (data.face_recognition_enabled ? "Enabled" : "Disabled"));
    this.text("risk-updated", data.time || "--");
    this.text("quick-uptime", data.uptime || "--");
    this.text("health-storage", this.percent(data.disk));
    this.text("health-api-status", "Online");
    this.text("system-live-badge", "Online");
  }

  async loadCameras() {
    const data = await this.requestJson("/api/cameras");
    const cameras = Array.isArray(data.list) ? data.list : [];
    const active = data.active_camera || {};
    this.text("active-camera-name", active.name || "No camera selected");
    this.text("active-camera-source", active.source_display || "No source");
    this.text("camera-roster-count", `${cameras.length} configured`);

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

  async loadAutomation() {
    const data = await this.requestJson("/api/automation");
    this.text("automation-mode", data.mode_label || data.mode || "--");
    this.text("automation-status", data.status || "No status available.");
    this.text("automation-risk", data.runtime_risk || "--");
    this.text("automation-armed", data.defense?.armed ? "Armed" : "Disarmed");
    this.text("automation-evaluated", this.formatTime(data.last_evaluated_at));
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
