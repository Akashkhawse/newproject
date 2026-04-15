import re

css_path = "static/css/dashboard.css"
with open(css_path, "r") as f:
    content = f.read()

# 1. Update root variables and add light mode
# Replace :root { ... } with our new root and dark/light modes
root_pattern = r":root\s*\{.*?\}"
new_root = """
:root {
    /* Base Variables (Dark Mode Default) */
    --bg: #081119;
    --bg-soft: #101c26;
    --panel: rgba(10, 22, 31, 0.7);
    --panel-strong: rgba(13, 28, 38, 0.85);
    --panel-muted: rgba(16, 34, 45, 0.6);
    --line: rgba(104, 176, 206, 0.18);
    --line-strong: rgba(104, 176, 206, 0.3);
    --ink: #eef7fb;
    --ink-soft: #93afbc;
    --cyan: #4bd4ff;
    --cyan-soft: rgba(75, 212, 255, 0.14);
    --amber: #ffb35c;
    --amber-soft: rgba(255, 179, 92, 0.14);
    --mint: #51e2a6;
    --mint-soft: rgba(81, 226, 166, 0.14);
    --danger: #ff6b7f;
    --danger-soft: rgba(255, 107, 127, 0.14);
    
    /* Neumorphic/Glassmorphic Effects */
    --shadow-lg: 0 30px 80px rgba(0, 0, 0, 0.6);
    --shadow-md: 0 15px 40px rgba(0, 0, 0, 0.4);
    --glass-border: rgba(255, 255, 255, 0.05);
    
    --radius-xl: 32px;
    --radius-lg: 24px;
    --radius-md: 16px;
    
    --blur-strong: blur(24px);
    --blur-md: blur(16px);
}

body.light-mode {
    --bg: #f5f7fa;
    --bg-soft: #e4e8f0;
    --panel: rgba(255, 255, 255, 0.85);
    --panel-strong: rgba(255, 255, 255, 0.95);
    --panel-muted: rgba(240, 244, 248, 0.7);
    --line: rgba(30, 60, 90, 0.1);
    --line-strong: rgba(30, 60, 90, 0.2);
    --ink: #111827;
    --ink-soft: #4b5563;
    --glass-border: rgba(0, 0, 0, 0.05);
    --shadow-lg: 0 20px 60px rgba(13, 28, 38, 0.08);
    --shadow-md: 0 10px 30px rgba(13, 28, 38, 0.05);
}
"""
content = re.sub(root_pattern, new_root, content, flags=re.DOTALL, count=1)

# 2. Update Typography
content = content.replace('font-family: "IBM Plex Sans", sans-serif;', 'font-family: "Inter", sans-serif;')
content = content.replace('font-family: "Rajdhani", sans-serif;', 'font-family: "Outfit", sans-serif;')

# 3. Add Voice Visualizer CSS
visualizer_css = """
/* Voice Visualizer */
.voice-visualizer {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    height: 40px;
    margin-bottom: 12px;
}
.voice-visualizer.hidden {
    display: none;
}
.voice-visualizer .bar {
    width: 6px;
    background: var(--cyan);
    border-radius: 99px;
    height: 8px;
    animation: voice-bounce 1s infinite ease-in-out;
}
.voice-visualizer .bar:nth-child(1) { animation-delay: -0.4s; }
.voice-visualizer .bar:nth-child(2) { animation-delay: -0.2s; background: var(--amber); height: 16px; }
.voice-visualizer .bar:nth-child(3) { animation-delay: -0s; background: var(--mint); height: 24px; }
.voice-visualizer .bar:nth-child(4) { animation-delay: -0.2s; background: var(--amber); height: 16px; }
.voice-visualizer .bar:nth-child(5) { animation-delay: -0.4s; }

@keyframes voice-bounce {
    0%, 100% {
        transform: scaleY(0.4);
    }
    50% {
        transform: scaleY(1.2);
    }
}
"""
content += "\n" + visualizer_css

with open(css_path, "w") as f:
    f.write(content)

# Update auth.css as well
auth_path = "static/css/auth.css"
with open(auth_path, "r") as f:
    auth_content = f.read()
auth_content = auth_content.replace('font-family: "Manrope", sans-serif;', 'font-family: "Inter", sans-serif;')
auth_content = auth_content.replace('font-family: "Sora", sans-serif;', 'font-family: "Outfit", sans-serif;')
with open(auth_path, "w") as f:
    f.write(auth_content)
