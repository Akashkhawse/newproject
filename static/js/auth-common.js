window.AuthUI = {
    setFeedback(elementId, message) {
        const feedback = document.getElementById(elementId);
        if (!feedback) return;

        if (!message) {
            feedback.hidden = true;
            feedback.textContent = "";
            return;
        }

        feedback.hidden = false;
        feedback.textContent = message;
    },

    togglePassword(inputId, buttonId) {
        const password = document.getElementById(inputId);
        const button = document.getElementById(buttonId);
        if (!password || !button) return;

        const showPassword = password.type === "password";
        password.type = showPassword ? "text" : "password";
        button.textContent = showPassword ? "Hide" : "Show";
    },

    async postJson(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(payload),
        });

        let data = {};
        try {
            data = await response.json();
        } catch (error) {
            data = {};
        }

        return { response, data };
    },
};
