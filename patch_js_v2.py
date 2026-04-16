import re

js_path = "static/js/dashboard.js"
with open(js_path, "r") as f:
    content = f.read()

# 1. Update fetchHealth to calculate Eco and AI Insights
health_pattern = r'setText\("device-active", data\.active_device_count \?\? 0, "0"\);'
health_patch = """setText("device-active", data.active_device_count ?? 0, "0");

        // V2 Eco Calculation
        let activeCount = data.active_device_count || 0;
        let ecoPower = activeCount * 42; // mock 42W average
        setText("eco-power-usage", ecoPower + " W");

        // V2 AI Insights
        let insightText = "Monitoring optimal operational baselines.";
        let mode = data.system_mode || "";
        if (mode === 'sentinel') {
            insightText = "Sentinel protocol active. Environmental systems locked for maximum defense.";
        } else if (activeCount > 4) {
            insightText = "High energy footprint detected. Consider routing non-essential nodes to sleep state.";
        } else if (activeCount === 0) {
            insightText = "Environment stands dormant. Deep sleep cycles initiated to preserve energy.";
        }
        setText("ai-insight-text", insightText);
        setText("ai-insight-sub", "Based on telemetry and behavioral pattern tracking.");
"""
content = content.replace('setText("device-active", data.active_device_count ?? 0, "0");', health_patch)

# 2. Update sidebar navigation logic
# The old logic used data-tab and classList.toggle on .tab-panel
# That still works, but I want to make sure it handles the sidebar layout.
# Actually the old JS already handles document.querySelectorAll(".tab-panel") so it will work fine!

with open(js_path, "w") as f:
    f.write(content)
