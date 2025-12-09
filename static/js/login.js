function setFeedback(message) {
    window.AuthUI?.setFeedback("loginFeedback", message);
}

function togglePassword() {
    window.AuthUI?.togglePassword("password", "toggle-password");
}

async function submitLogin(event) {
    event.preventDefault();

    const email = document.getElementById("email")?.value.trim() || "";
    const password = document.getElementById("password")?.value || "";

    setFeedback("");
    if (!email || !password) {
        setFeedback("Please enter email and password.");
        return;
    }

    try {
        const { response, data } = await window.AuthUI.postJson("/login", { email, password });

        if (!response.ok) {
            if (data.redirect && response.status === 400) {
                window.location.href = data.redirect;
                return;
            }
            setFeedback(data.error || "Login failed.");
            return;
        }

        window.location.href = data.redirect || "/";
    } catch (error) {
        setFeedback("Network error. Please try again.");
    }
}

document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("toggle-password")?.addEventListener("click", togglePassword);
    document.getElementById("loginForm")?.addEventListener("submit", submitLogin);
});
