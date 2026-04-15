import re

js_path = "static/js/dashboard.js"
with open(js_path, "r") as f:
    content = f.read()

# 1. Add sentinel to assistantModeLabels
if "sentinel:" not in content:
    content = content.replace('self_monitoring: "Self monitoring",', 'self_monitoring: "Self monitoring",\n    sentinel: "Sentinel",')

# 2. visualizer toggle
# Look for requestJson("/assistant",
func_pattern = r'const data = await requestJson\("/assistant", \{'
func_patch = """
        document.getElementById('voice-visualizer')?.classList.remove('hidden');
        const data = await requestJson("/assistant", {"""
content = content.replace('const data = await requestJson("/assistant", {', func_patch)

# And add the hide after it resolves, which is around 'if (!appState.geminiConfigured...' or after 'speak(data.reply);'
speak_pattern = r'speak\(data\.reply\);'
speak_patch = """speak(data.reply);
        document.getElementById('voice-visualizer')?.classList.add('hidden');"""
content = content.replace('speak(data.reply);', speak_patch)

# And on error
error_pattern = r'if \(aiReply\) aiReply\.textContent = error\.message \|\| "Error talking to assistant\.";'
error_patch = """if (aiReply) aiReply.textContent = error.message || "Error talking to assistant.";
        document.getElementById('voice-visualizer')?.classList.add('hidden');"""
content = content.replace('if (aiReply) aiReply.textContent = error.message || "Error talking to assistant.";', error_patch)


# 3. add theme toggle listener at the end of the script before the final brace or DOMContentLoaded block
dom_content_loaded = r'document\.addEventListener\("DOMContentLoaded", \(\) => \{'
# We find where DOMContentLoaded ends or put it inside it.
# Let's just find `document.getElementById("assistant-mode")?.addEventListener` and put it right before it
event_listener_pattern = r'document\.getElementById\("assistant-mode"\)\?\.addEventListener'
theme_toggle = """
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
        document.body.classList.toggle("light-mode");
    });
    
    document.getElementById("assistant-mode")?.addEventListener"""
content = content.replace('document.getElementById("assistant-mode")?.addEventListener', theme_toggle)


with open(js_path, "w") as f:
    f.write(content)
