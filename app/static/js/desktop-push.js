(async () => {
    const button = document.querySelector("[data-desktop-push]");
    if (!button) return;

    const status = document.querySelector("[data-desktop-push-status]");
    let subscription;
    let registration;
    let config;

    const sendSubscription = async (method, data) => {
        const response = await fetch("/admin/web-push/subscription", {
            method,
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": button.dataset.csrf
            },
            body: data ? JSON.stringify(data) : undefined
        });

        if (!response.ok || response.redirected) {
            throw new Error(
                "Das Push-Abonnement konnte nicht gespeichert werden."
            );
        }
    };

    const publicKeyBytes = value => {
        const encoded = value.replace(/-/g, "+").replace(/_/g, "/");
        return Uint8Array.from(
            atob(encoded + "=".repeat((4 - encoded.length % 4) % 4)),
            char => char.charCodeAt(0)
        );
    };

    button.disabled = true;

    try {
        if (
            !window.isSecureContext
            || !("serviceWorker" in navigator)
            || !("PushManager" in window)
        ) {
            throw new Error(
                "Dieser Browser unterstützt Desktop-Benachrichtigungen hier nicht."
            );
        }

        const response = await fetch("/admin/web-push/config", {
            credentials: "same-origin"
        });

        if (!response.ok || response.redirected) {
            throw new Error("Bitte erneut anmelden.");
        }

        config = await response.json();

        if (!config.enabled) {
            throw new Error(
                "Desktop-Benachrichtigungen sind noch nicht eingerichtet."
            );
        }

        await navigator.serviceWorker.register(
            "/push-worker.js",
            { scope: "/" }
        );
        registration = await navigator.serviceWorker.ready;
        subscription = await registration.pushManager.getSubscription();

        if (subscription && Notification.permission === "granted") {
            await sendSubscription("POST", subscription.toJSON());
        } else {
            subscription = null;
        }

        button.textContent = subscription
            ? "Desktop-Benachrichtigungen deaktivieren"
            : "Desktop-Benachrichtigungen aktivieren";
        button.disabled = false;
    } catch (error) {
        status.textContent = error.message;
    }

    button.addEventListener("click", async () => {
        button.disabled = true;

        try {
            if (subscription) {
                await sendSubscription("DELETE");
                await subscription.unsubscribe();
                subscription = null;
                status.textContent =
                    "Desktop-Benachrichtigungen sind deaktiviert.";
            } else {
                const permission = await Notification.requestPermission();

                if (permission !== "granted") {
                    throw new Error(
                        "Bitte Benachrichtigungen in den Browser-Einstellungen erlauben."
                    );
                }

                subscription = await registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: publicKeyBytes(config.public_key)
                });

                try {
                    await sendSubscription("POST", subscription.toJSON());
                } catch (error) {
                    await subscription.unsubscribe();
                    subscription = null;
                    throw error;
                }

                status.textContent =
                    "Desktop-Benachrichtigungen sind aktiviert.";
            }

            button.textContent = subscription
                ? "Desktop-Benachrichtigungen deaktivieren"
                : "Desktop-Benachrichtigungen aktivieren";
        } catch (error) {
            status.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });
})();