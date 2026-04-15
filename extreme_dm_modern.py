import re

css_path = "static/css/dashboard.css"
with open(css_path, "r") as f:
    content = f.read()

# Replace :root { ... }
root_pattern = r":root\s*\{.*?\}"

extreme_root = """
:root {
    /* Extreme DM Modern Variables */
    --bg: #000000;
    --bg-soft: #09090b;
    
    --panel: rgba(24, 24, 27, 0.65);
    --panel-strong: rgba(24, 24, 27, 0.85);
    --panel-muted: rgba(24, 24, 27, 0.4);
    
    --line: rgba(168, 85, 247, 0.2); 
    --line-strong: rgba(168, 85, 247, 0.5);
    
    --ink: #fafafa;
    --ink-soft: #a1a1aa;
    
    /* Neon Accents */
    --cyan: #06b6d4;
    --cyan-soft: rgba(6, 182, 212, 0.15);
    
    --amber: #f59e0b;
    --amber-soft: rgba(245, 158, 11, 0.15);
    
    --mint: #10b981;
    --mint-soft: rgba(16, 185, 129, 0.15);
    
    --danger: #ef4444;
    --danger-soft: rgba(239, 68, 68, 0.15);
    
    /* Glow Effects */
    --glow-cyan: 0 0 10px rgba(6, 182, 212, 0.5), 0 0 20px rgba(6, 182, 212, 0.3);
    --glow-purple: 0 0 10px rgba(168, 85, 247, 0.5), 0 0 20px rgba(168, 85, 247, 0.3);
    --glow-danger: 0 0 10px rgba(239, 68, 68, 0.5), 0 0 20px rgba(239, 68, 68, 0.3);
    
    --shadow-lg: 0 20px 40px rgba(0, 0, 0, 0.8);
    --shadow-md: 0 10px 20px rgba(0, 0, 0, 0.6);
    --glass-border: rgba(168, 85, 247, 0.15); /* Purple tint glass border */
    
    --radius-xl: 16px;
    --radius-lg: 12px;
    --radius-md: 8px;
    
    /* Extreme Blur */
    --blur-strong: blur(40px);
    --blur-md: blur(20px);
}

body.light-mode {
    --bg: #f4f4f5;
    --bg-soft: #e4e4e7;
    --panel: rgba(255, 255, 255, 0.7);
    --panel-strong: rgba(255, 255, 255, 0.9);
    --panel-muted: rgba(255, 255, 255, 0.5);
    --line: rgba(168, 85, 247, 0.1);
    --line-strong: rgba(168, 85, 247, 0.3);
    --ink: #09090b;
    --ink-soft: #52525b;
    --glass-border: rgba(168, 85, 247, 0.1);
}

/* Base Body Update for DM Modern */
body {
    background-color: var(--bg);
    color: var(--ink);
    font-family: 'Inter', sans-serif;
    background-image: 
        radial-gradient(circle at 10% 20%, rgba(168, 85, 247, 0.05) 0%, transparent 20%),
        radial-gradient(circle at 90% 80%, rgba(6, 182, 212, 0.05) 0%, transparent 20%);
    background-attachment: fixed;
}

h1, h2, h3, h4, h5, h6, .brand-logo, .panel-title, .metric-value, .stat-value {
    font-family: 'Outfit', sans-serif;
    letter-spacing: -0.02em;
}

/* Neon Glow Utilities */
.status-pill.threat-high, .status-pill.threat-critical {
    box-shadow: var(--glow-danger);
    animation: neon-pulse-danger 2s infinite alternate;
}

@keyframes neon-pulse-danger {
    0% { box-shadow: 0 0 5px rgba(239, 68, 68, 0.4); }
    100% { box-shadow: 0 0 20px rgba(239, 68, 68, 0.8), 0 0 30px rgba(239, 68, 68, 0.6); }
}

/* Voice Visualizer Update */
.voice-visualizer .bar {
    width: 4px;
    border-radius: 2px;
}
.voice-visualizer .bar:nth-child(even) {
    background: #a855f7;
    box-shadow: 0 0 8px #a855f7;
}
.voice-visualizer .bar:nth-child(odd) {
    background: #06b6d4;
    box-shadow: 0 0 8px #06b6d4;
}

/* Scan Grid HUD Style */
.scan-grid {
    background-size: 40px 40px;
    background-image: 
        linear-gradient(to right, rgba(168, 85, 247, 0.05) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(168, 85, 247, 0.05) 1px, transparent 1px);
    opacity: 0.8;
}
.scan-glow {
    background: radial-gradient(circle at center, rgba(168,85,247, 0.15) 0%, transparent 60%);
}

/* Buttons Glow Hover */
.btn-primary:hover, button[type="submit"]:hover {
    box-shadow: var(--glow-purple);
    transform: translateY(-1px);
}
.tab-btn.active {
    box-shadow: inset 0 -2px 0 0 #a855f7, var(--glow-purple);
    color: #a855f7;
}
"""

content = re.sub(root_pattern, extreme_root, content, flags=re.DOTALL, count=1)

with open(css_path, "w") as f:
    f.write(content)


auth_path = "static/css/auth.css"
with open(auth_path, "r") as f:
    auth_content = f.read()

auth_content = re.sub(root_pattern, extreme_root, auth_content, flags=re.DOTALL, count=1)
with open(auth_path, "w") as f:
    f.write(auth_content)

