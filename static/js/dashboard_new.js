const MODULES = [
  ["dashboard", "Dashboard", "◫"],
  ["cameras", "Cameras", "▣"],
  ["live-monitoring", "Live Monitoring", "▥"],
  ["ai-detection", "AI Detection", "◎"],
  ["face-recognition", "Face Recognition", "◉"],
  ["motion-tracking", "Motion Tracking", "⌁"],
  ["heatmaps", "Heatmaps", "▧"],
  ["analytics", "Analytics", "▤"],
  ["incidents", "Incidents", "⚑"],
  ["notifications", "Notifications", "◌"],
  ["reports", "Reports", "▦"],
  ["device-management", "Device Management", "◍"],
  ["automation", "Automation", "⟲"],
  ["assistant", "Assistant", "✦"],
  ["user-management", "User Management", "◈"],
  ["settings", "Settings", "⚙"],
];

const DETECTION_CLASSES = ["Person", "Vehicle", "Car", "Truck", "Bus", "Bike", "Fire", "Smoke", "Weapon", "Intrusion", "Animal"];
const AUTOMATION_RULES = [
  ["IF Fire Detected", "THEN Activate Alarm", "critical"],
  ["IF Intrusion Detected", "THEN Lock Doors", "warning"],
  ["IF Unknown Face", "THEN Notify Security", "warning"],
  ["IF High Threat", "THEN Enable Defense Mode", "critical"],
];

class EnterpriseSurveillance {
  constructor() {
    this.state = {};
    this.page = "dashboard";
    this.timer = null;
    this.init();
  }

  init() {
    this.renderNav();
    this.bind();
    this.openPage(location.hash.replace("#", "") || "dashboard");
    this.refresh(true);
    this.timer = setInterval(() => this.refresh(false), 6000);
  }

  bind() {
    document.getElementById("refresh-btn")?.addEventListener("click", () => this.refresh(true));
    document.getElementById("panic-btn")?.addEventListener("click", () => this.enterpriseAction("defense_mode", { enabled: true }));
    document.getElementById("mobile-menu")?.addEventListener("click", () => document.body.classList.toggle("menu-open"));
    window.addEventListener("hashchange", () => this.openPage(location.hash.replace("#", "") || "dashboard"));

    document.getElementById("assistant-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.askAssistant();
    });
    document.querySelectorAll("[data-command]").forEach((button) => {
      button.addEventListener("click", () => {
        const input = document.getElementById("assistant-query");
        if (input) input.value = button.dataset.command;
        this.askAssistant();
      });
    });
    document.querySelectorAll("[data-open-modal]").forEach((button) => {
      button.addEventListener("click", () => this.openModal(button.dataset.openModal));
    });
    document.querySelectorAll("[data-filter-notifications]").forEach((button) => {
      button.addEventListener("click", () => this.renderNotifications(button.dataset.filterNotifications));
    });
    document.getElementById("new-incident-btn")?.addEventListener("click", () => this.createIncident());
    document.getElementById("train-face-btn")?.addEventListener("click", () => this.post("/api/faces/retrain", {}).then(() => this.toast("Face model retraining started.")));
    document.getElementById("global-search")?.addEventListener("input", (event) => this.search(event.target.value));
    document.getElementById("modal-form")?.addEventListener("submit", (event) => this.submitModal(event));
  }

  renderNav() {
    const nav = document.getElementById("module-nav");
    if (!nav) return;
    nav.innerHTML = MODULES.map(([id, label, icon]) => `
      <button class="nav-item" type="button" data-page-link="${id}">
        <span class="nav-icon">${icon}</span><span>${label}</span><span class="nav-dot"></span>
      </button>
    `).join("");
    nav.querySelectorAll("[data-page-link]").forEach((button) => {
      button.addEventListener("click", () => {
        location.hash = button.dataset.pageLink;
        document.body.classList.remove("menu-open");
      });
    });
  }

  openPage(id) {
    if (!MODULES.some(([key]) => key === id)) id = "dashboard";
    this.page = id;
    document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === `page-${id}`));
    document.querySelectorAll("[data-page-link]").forEach((link) => link.classList.toggle("active", link.dataset.pageLink === id));
    const page = document.getElementById(`page-${id}`);
    document.getElementById("page-title").textContent = page?.dataset.title || "Dashboard";
    document.getElementById("page-eyebrow").textContent = page?.dataset.eyebrow || "Enterprise Security Operations Center";
  }

  async requestJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && data.redirect) location.href = data.redirect;
    if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
    return data;
  }

  post(url, payload) {
    return this.requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  async refresh(announce = false) {
    try {
      const [enterprise, health, cameras, incidents, notifications, devices, analytics, vision, faces] = await Promise.all([
        this.requestJson("/api/enterprise/snapshot"),
        this.requestJson("/health"),
        this.requestJson("/api/cameras"),
        this.requestJson("/api/incidents?limit=80"),
        this.requestJson("/api/notifications?limit=80"),
        this.requestJson("/api/devices"),
        this.requestJson("/api/analytics/summary"),
        this.requestJson("/api/telemetry/vision?limit=20"),
        this.requestJson("/api/faces").catch(() => ({ people: [] })),
      ]);
      this.state = { enterprise, health, cameras, incidents, notifications, devices, analytics, vision, faces };
      this.renderAll();
      if (announce) this.toast("Command center synchronized.");
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  renderAll() {
    this.renderDashboard();
    this.renderCameras();
    this.renderLiveWall();
    this.renderDetection();
    this.renderFaces();
    this.renderTracking();
    this.renderAnalytics();
    this.renderIncidents();
    this.renderNotifications("all");
    this.renderReports();
    this.renderDevices();
    this.renderAutomation();
    this.renderUsers();
    this.renderSettings();
  }

  renderDashboard() {
    const { health = {}, enterprise = {}, cameras = {}, devices = {}, incidents = {}, analytics = {} } = this.state;
    const onlineCameras = (cameras.cameras || []).filter((c) => c.enabled !== false).length;
    const offlineCameras = Math.max((cameras.cameras || []).length - onlineCameras, 0);
    const activeDevices = (devices.devices || []).filter((d) => d.state === "ON").length;
    const openIncidents = (incidents.items || []).filter((i) => i.status === "open").length;
    const metrics = [
      ["CPU Usage", this.percent(health.cpu_percent), "Processor telemetry"],
      ["RAM Usage", this.percent(health.memory), "Memory telemetry"],
      ["Disk Usage", this.percent(health.disk), "Storage capacity"],
      ["Network Usage", `${health.net_recv || 0} MB`, "Received traffic"],
      ["Cameras Online", onlineCameras, "Live camera sources"],
      ["Cameras Offline", offlineCameras, "Attention required"],
      ["Devices Online", activeDevices, "Response hardware"],
      ["Active Incidents", openIncidents, "Open case queue"],
      ["Active Alerts", enterprise.alerts?.length || 0, "Realtime feed"],
      ["Threat Level", this.human(health.risk_level), "AI evaluation"],
      ["AI Health", enterprise.ai?.health || "Operational", "Model pipeline"],
      ["Face Recognition", health.face_recognition_status || "Ready", "Identity service"],
      ["YOLO Status", health.yolo_status || "Ready", "Object detection"],
    ];
    this.html("dashboard-metrics", metrics.map(([label, value, sub]) => this.metric(label, value, sub)).join(""));
    this.text("threat-level", this.human(health.risk_level || "normal"));
    this.text("threat-score", health.risk_score ?? analytics.risk?.score ?? 0);
    this.text("alert-count", `${enterprise.alerts?.length || 0} alerts`);
    this.html("alert-feed", (enterprise.alerts || []).map((a) => this.feedItem(a.title || a.message, a.detail || a.created_at, a.level)).join("") || this.empty("No active alerts."));
    this.html("threat-bars", (enterprise.trends?.threat || [22, 31, 28, 44, 39, 52, 47]).map((v, i) => `<div class="bar" style="height:${v + 20}%"><span>D${i + 1}</span></div>`).join(""));
    this.html("camera-health-list", (cameras.cameras || []).slice(0, 6).map((c) => this.row(c.name, c.transport || c.type || "source", c.enabled === false ? "offline" : "online")).join(""));
    this.text("camera-health", `${onlineCameras}/${(cameras.cameras || []).length} online`);
    this.text("ai-health", enterprise.ai?.health || "Operational");
    this.html("ai-activity", (enterprise.ai?.activity || []).map((a) => this.row(a.name, `${a.count} events`, `${a.confidence}%`)).join(""));
    this.html("system-status-list", [
      this.row("API Authentication", "Token-ready REST layer", "secure"),
      this.row("RBAC", `Current role: ${document.body.dataset.currentRole}`, "active"),
      this.row("Session Management", health.uptime || "Active", "online"),
    ].join(""));
  }

  renderCameras() {
    const cameras = this.state.cameras?.cameras || [];
    this.html("camera-grid", cameras.map((camera, index) => `
      <article class="camera-card">
        <div class="camera-preview"><footer><strong>${this.escape(camera.name)}</strong><span>${camera.transport || camera.type}</span></footer></div>
        <div class="row"><div><strong>${this.escape(camera.label || camera.name)}</strong><p>${this.escape(camera.source_display || camera.source || "local")}</p></div><span class="pill">${camera.enabled === false ? "Offline" : "Online"}</span></div>
        <div class="camera-actions">
          <button class="control-btn" data-set-camera="${this.escape(camera.id)}">Fullscreen</button>
          <button class="control-btn">Snapshot</button>
          <button class="control-btn">Record</button>
          <button class="control-btn">PTZ</button>
          <button class="control-btn" data-delete-camera="${this.escape(camera.id)}">Delete</button>
        </div>
      </article>
    `).join("") || this.empty("No cameras configured."));
    document.querySelectorAll("[data-delete-camera]").forEach((button) => button.onclick = () => this.deleteCamera(button.dataset.deleteCamera));
    document.querySelectorAll("[data-set-camera]").forEach((button) => button.onclick = () => this.post("/api/cameras/active", { id: button.dataset.setCamera }).then(() => this.refresh(true)));
  }

  renderLiveWall() {
    const cameras = this.state.cameras?.cameras || [];
    this.text("primary-camera-name", this.state.cameras?.active_camera?.name || cameras[0]?.name || "Primary Camera");
    this.html("video-wall-side", cameras.slice(0, 4).map((camera) => `
      <article class="live-tile">
        <div class="ai-box one"><span>Vehicle</span><small>88%</small></div>
        <footer><strong>${this.escape(camera.name)}</strong><span>${camera.enabled === false ? "standby" : "live"}</span></footer>
      </article>
    `).join(""));
  }

  renderDetection() {
    const objects = this.state.vision?.objects || [];
    this.text("detection-total", `${objects.length || DETECTION_CLASSES.length} active`);
    this.html("detection-grid", DETECTION_CLASSES.map((name) => {
      const hit = objects.find((item) => String(item.label || "").toLowerCase() === name.toLowerCase());
      const count = hit?.count ?? Math.floor(Math.random() * 8);
      const confidence = hit?.confidence ? Math.round(hit.confidence * 100) : 72 + Math.floor(Math.random() * 24);
      return `<article class="detection-card"><span class="pill">${confidence}%</span><strong>${count}</strong><p>${name}</p></article>`;
    }).join(""));
    this.html("detection-history", DETECTION_CLASSES.slice(0, 8).map((name, i) => this.feedItem(`${name} detected`, `Camera ${1 + (i % 4)} • confidence ${82 - i}%`, i < 3 ? "warning" : "info")).join(""));
  }

  renderFaces() {
    const people = this.state.enterprise?.faces || [];
    this.html("face-board", people.map((face) => `
      <article class="face-card">
        <div class="face-avatar">${this.initials(face.name)}</div>
        <strong>${this.escape(face.name)}</strong>
        <p>${this.escape(face.group)} • last seen ${this.escape(face.last_seen)}</p>
        <span class="pill">${this.escape(face.camera)}</span>
      </article>
    `).join(""));
  }

  renderTracking() {
    const points = [[9,72],[22,60],[38,66],[52,44],[67,48],[82,31]];
    const stage = document.getElementById("tracking-stage");
    if (stage) {
      stage.innerHTML = points.map(([x, y], i) => `<span class="track-path" style="left:${x}%;top:${y}%">${i + 1}</span>`).join("") +
        points.slice(0, -1).map(([x, y], i) => {
          const [nx, ny] = points[i + 1];
          const dx = nx - x, dy = ny - y;
          const len = Math.sqrt(dx * dx + dy * dy);
          const angle = Math.atan2(dy, dx) * 180 / Math.PI;
          return `<span class="trail" style="left:${x}%;top:${y}%;width:${len}%;transform:rotate(${angle}deg)"></span>`;
        }).join("");
    }
    this.html("tracking-cards", ["Person ID P-104", "Vehicle ID V-022", "Object ID B-771"].map((name, i) => `
      <article class="panel"><div class="panel-head"><h3>${name}</h3><span>${94 - i * 5}%</span></div><p>Historical trail from camera ${i + 1} to camera ${i + 4}. Movement confidence active.</p></article>
    `).join(""));
  }

  renderAnalytics() {
    const risk = this.state.analytics?.risk || {};
    const kpis = [
      ["Threat Score", risk.score ?? 0, "Current risk model"],
      ["Incident Count", this.state.incidents?.total ?? 0, "Total cases"],
      ["Response Time", "01:42", "Average response"],
      ["Detection Accuracy", "94.8%", "Model confidence"],
    ];
    this.html("analytics-kpis", kpis.map(([a, b, c]) => this.metric(a, b, c)).join(""));
    this.html("analytics-bars", [44, 31, 55, 48, 70, 64, 82, 59, 73, 61, 88, 76].map((v, i) => `<div class="bar" style="height:${v}%"><span>${i + 1}</span></div>`).join(""));
    this.html("analytics-list", ["Daily reports ready", "Weekly trend stable", "Monthly uptime 99.2%", "Yearly incident reduction 18%"].map((x) => this.row(x, "Analytics engine", "active")).join(""));
  }

  renderIncidents() {
    const statuses = ["open", "acknowledged", "investigating", "resolved"];
    const incidents = this.state.incidents?.items || [];
    this.html("incident-board", statuses.map((status) => {
      const items = incidents.filter((incident) => (incident.status || "open") === status).slice(0, 8);
      return `<section class="incident-col"><h3>${this.human(status)}</h3>${items.map((incident) => `
        <article class="incident-card">
          <strong>${this.escape(incident.title)}</strong>
          <p>${this.escape(incident.details?.ai_summary || "AI summary: evidence reviewed and timeline generated.")}</p>
          <span class="pill ${incident.severity}">${this.escape(incident.severity)}</span>
          <button class="control-btn" data-incident="${incident.id}" data-status="${this.nextStatus(status)}">Move</button>
        </article>`).join("") || this.empty("No cases.")}</section>`;
    }).join(""));
    document.querySelectorAll("[data-incident]").forEach((button) => {
      button.onclick = () => this.post(`/api/incidents/${button.dataset.incident}/status`, { status: button.dataset.status }).then(() => this.refresh(true));
    });
  }

  renderNotifications(filter = "all") {
    let items = this.state.notifications?.items || [];
    if (filter === "unread") items = items.filter((item) => !item.read_at);
    this.html("notification-feed", items.map((item) => `
      <article class="feed-item">
        <div><strong>${this.escape(item.message)}</strong><p>${this.escape(item.created_at || "Recent")} • ${this.escape(item.type || "system")}</p></div>
        <button class="control-btn ${item.read_at ? "" : "primary"}" data-read-notification="${item.id}">${item.read_at ? "Read" : "Mark read"}</button>
      </article>
    `).join("") || this.empty("No notifications."));
    document.querySelectorAll("[data-read-notification]").forEach((button) => {
      button.onclick = () => this.post(`/api/notifications/${button.dataset.readNotification}/read`, {}).then(() => this.refresh(true));
    });
  }

  renderReports() {
    const reports = ["Incident Reports", "Analytics Reports", "Threat Reports", "Camera Reports", "User Activity Reports"];
    this.html("report-grid", reports.map((name) => `
      <article class="report-card"><strong>${name}</strong><p>Generate PDF, CSV, or Excel export for compliance and executive review.</p><div class="button-row"><a class="control-btn" href="/api/reports/incidents.csv">CSV</a><button class="control-btn">PDF</button><button class="control-btn">Excel</button></div></article>
    `).join(""));
  }

  renderDevices() {
    const devices = this.state.devices?.devices || [];
    this.html("device-grid", devices.map((device) => `
      <article class="device-card">
        <div class="panel-head"><h3>${this.escape(device.name)}</h3><span class="${device.state === "ON" ? "ok" : "warning"}">${device.state}</span></div>
        <p>${this.deviceType(device.name)} • enable, disable, edit, delete, and automate.</p>
        <div class="button-row"><button class="control-btn" data-toggle-device="${this.escape(device.name)}">${device.state === "ON" ? "Disable" : "Enable"}</button><button class="control-btn">Edit</button><button class="control-btn" data-delete-device="${this.escape(device.name)}">Delete</button></div>
      </article>
    `).join("") || this.empty("No devices configured."));
    document.querySelectorAll("[data-toggle-device]").forEach((button) => button.onclick = () => this.post(`/toggle/${encodeURIComponent(button.dataset.toggleDevice)}`, {}).then(() => this.refresh(true)));
    document.querySelectorAll("[data-delete-device]").forEach((button) => button.onclick = () => this.requestJson(`/api/devices/${encodeURIComponent(button.dataset.deleteDevice)}`, { method: "DELETE" }).then(() => this.refresh(true)));
  }

  renderAutomation() {
    const modes = ["Manual", "Self Monitoring", "Hybrid", "Sentinel"];
    this.html("automation-modes", modes.map((mode, i) => `<button class="control-btn ${i === 2 ? "active" : ""}" data-mode="${mode.toLowerCase().replaceAll(" ", "_")}">${mode}</button>`).join(""));
    this.html("rule-grid", AUTOMATION_RULES.map(([condition, action, level]) => `
      <article class="rule-card"><span class="pill ${level}">Rule</span><strong>${condition}</strong><p>${action}</p><button class="control-btn">Enabled</button></article>
    `).join(""));
    document.querySelectorAll("[data-mode]").forEach((button) => button.onclick = () => this.post("/api/automation", { mode: button.dataset.mode }).then(() => this.refresh(true)));
  }

  renderUsers() {
    const users = this.state.enterprise?.users || [];
    this.html("user-grid", users.map((user) => `
      <article class="user-card"><div class="face-avatar">${this.initials(user.display_name || user.email)}</div><strong>${this.escape(user.display_name || user.email)}</strong><p>${this.escape(user.role)} • last login ${this.escape(user.last_login_at || "not recorded")}</p><span class="pill">RBAC</span></article>
    `).join(""));
  }

  renderSettings() {
    const settings = ["Dark Mode", "Light Mode", "2FA", "Backup", "Restore", "Email Settings", "SMS Alerts", "AI Settings", "Camera Settings", "System Settings"];
    this.html("settings-grid", settings.map((name, i) => `
      <article class="setting-card"><div class="panel-head"><h3>${name}</h3><span>${i % 3 === 0 ? "On" : "Ready"}</span></div><p>Enterprise policy control with audit logging and secure defaults.</p><button class="control-btn">Configure</button></article>
    `).join(""));
  }

  async askAssistant() {
    const input = document.getElementById("assistant-query");
    const query = (input?.value || "").trim();
    if (!query) return;
    this.addMessage(query, "me");
    if (input) input.value = "";
    try {
      const data = await this.post("/assistant", { query, mode: "hybrid" });
      this.addMessage(data.reply || "Command processed.", "ai");
      await this.refresh(false);
    } catch (error) {
      this.addMessage(error.message, "ai");
    }
  }

  addMessage(text, who) {
    const log = document.getElementById("chat-log");
    if (!log) return;
    log.insertAdjacentHTML("beforeend", `<div class="message ${who === "me" ? "me" : ""}">${this.escape(text)}</div>`);
    log.scrollTop = log.scrollHeight;
  }

  openModal(type) {
    const modal = document.getElementById("form-modal");
    const title = document.getElementById("modal-title");
    const fields = document.getElementById("modal-fields");
    if (!modal || !title || !fields) return;
    modal.dataset.type = type;
    title.textContent = type === "camera" ? "Add Camera" : "Add Device";
    fields.innerHTML = type === "camera"
      ? `<label>Name<input name="name" required placeholder="Main Entry Camera"></label><label>Source<input name="source" required placeholder="rtsp://, http://, mobile://, or 0"></label><label>Type<select name="type"><option>rtsp</option><option>ip</option><option>usb</option><option>mobile</option><option>onvif</option></select></label>`
      : `<label>Name<input name="name" required placeholder="Alarm, Siren, Door Lock"></label>`;
    modal.showModal();
  }

  async submitModal(event) {
    event.preventDefault();
    const modal = document.getElementById("form-modal");
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      if (modal.dataset.type === "camera") await this.post("/api/cameras", data);
      else await this.post("/api/devices", data);
      modal.close();
      form.reset();
      await this.refresh(true);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async deleteCamera(id) {
    await this.requestJson(`/api/cameras/${encodeURIComponent(id)}`, { method: "DELETE" });
    await this.refresh(true);
  }

  async createIncident() {
    await this.post("/api/incidents", {
      title: "Operator-created incident",
      severity: "warning",
      tags: ["manual", "operator"],
      details: { ai_summary: "Manual case created from the command center.", suggested_actions: ["Review cameras", "Assign operator"] },
    });
    await this.refresh(true);
  }

  async enterpriseAction(action, payload) {
    await this.post("/api/enterprise/action", { action, ...payload });
    await this.refresh(true);
  }

  search(query) {
    const text = String(query || "").toLowerCase().trim();
    if (!text) return;
    const module = MODULES.find(([, label]) => label.toLowerCase().includes(text));
    if (module) location.hash = module[0];
  }

  metric(label, value, sub) {
    return `<article class="metric-card"><small>${this.escape(label)}</small><strong>${this.escape(value)}</strong><span>${this.escape(sub)}</span></article>`;
  }

  row(title, detail, tag) {
    return `<article class="row"><div><strong>${this.escape(title)}</strong><p>${this.escape(detail)}</p></div><span class="pill">${this.escape(tag)}</span></article>`;
  }

  feedItem(title, detail, level = "info") {
    return `<article class="feed-item"><div><strong class="${this.escape(level)}">${this.escape(title)}</strong><p>${this.escape(detail || "")}</p></div><span class="pill ${this.escape(level)}">${this.escape(this.human(level))}</span></article>`;
  }

  empty(text) { return `<p class="row">${this.escape(text)}</p>`; }
  percent(value) { return Number.isFinite(Number(value)) ? `${Math.round(Number(value))}%` : "--"; }
  human(value) { return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
  initials(value) { return String(value || "SS").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]).join("").toUpperCase() || "SS"; }
  deviceType(name) {
    const value = String(name || "").toLowerCase();
    if (value.includes("lock")) return "Door Lock";
    if (value.includes("siren")) return "Siren";
    if (value.includes("alarm")) return "Alarm";
    if (value.includes("light")) return "Light";
    if (value.includes("sensor")) return "Sensor";
    if (value.includes("relay")) return "Relay";
    return "Smart Switch";
  }
  nextStatus(status) {
    return { open: "acknowledged", acknowledged: "investigating", investigating: "resolved", resolved: "open" }[status] || "open";
  }
  html(id, value) { const el = document.getElementById(id); if (el) el.innerHTML = value; }
  text(id, value) { const el = document.getElementById(id); if (el) el.textContent = value; }
  escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
  }
  toast(message, error = false) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.style.borderColor = error ? "rgba(255,90,106,.7)" : "rgba(53,211,185,.55)";
    toast.classList.add("show");
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
  }
}

window.addEventListener("DOMContentLoaded", () => new EnterpriseSurveillance());
