function setFeedback(message) {
    window.AuthUI?.setFeedback("registerFeedback", message);
}

function scorePassword(password) {
    let score = 0;

    if (!password) return score;
    if (password.length >= 8) score += 1;
    if (/[A-Z]/.test(password)) score += 1;
    if (/[0-9]/.test(password)) score += 1;
    if (/[^A-Za-z0-9]/.test(password)) score += 1;

    return score;
}

function updateStrength() {
    const password = document.getElementById("password");
    const strengthBar = document.getElementById("strengthBar");
    if (!password || !strengthBar) return;

    const score = scorePassword(password.value || "");
    const percent = Math.round((score / 4) * 100);

    strengthBar.style.width = `${percent}%`;
    if (score <= 1) {
        strengthBar.style.background = "linear-gradient(90deg,#ff6b6b,#ff9a6b)";
    } else if (score === 2) {
        strengthBar.style.background = "linear-gradient(90deg,#ffc857,#ffd54f)";
    } else if (score === 3) {
        strengthBar.style.background = "linear-gradient(90deg,#a1e887,#36e2b3)";
    } else {
        strengthBar.style.background = "linear-gradient(90deg,#52e3ff,#19b5d6)";
    }
}

function togglePassword() {
    window.AuthUI?.togglePassword("password", "toggle-password");
    updateStrength();
}

async function submitRegister(event) {
    event.preventDefault();

    const fullName = document.getElementById("full_name")?.value.trim() || "";
    const email = document.getElementById("email")?.value.trim() || "";
    const password = document.getElementById("password")?.value || "";

    setFeedback("");
    if (!email || !password) {
        setFeedback("Please enter email and password.");
        return;
    }
    if (password.length < 8) {
        setFeedback("Password must be at least 8 characters.");
        return;
    }

    try {
        const { response, data } = await window.AuthUI.postJson("/register", {
            full_name: fullName,
            email,
            password,
        });

        if (!response.ok) {
            setFeedback(data.error || "Registration failed.");
            return;
        }

        window.location.href = data.redirect || "/";
    } catch (error) {
        setFeedback("Network error. Please try again.");
    }
}

document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("toggle-password")?.addEventListener("click", togglePassword);
    document.getElementById("password")?.addEventListener("input", updateStrength);
    document.getElementById("registerForm")?.addEventListener("submit", submitRegister);
    updateStrength();
});
