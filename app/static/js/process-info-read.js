document.querySelectorAll("[data-chat-read-url]").forEach(chat => {
    const visible = new Set();
    const acknowledged = new Set();
    let sending = false;
    let refreshing = false;

    const refreshStatus = async () => {
        if (refreshing || document.visibilityState !== "visible") return;

        refreshing = true;

        try {
            const response = await fetch(chat.dataset.chatStatusUrl, {
                credentials: "same-origin"
            });

            if (!response.ok || response.redirected) return;

            const data = await response.json();
            const messages = new Map(
                data.items.map(item => [item.id, item])
            );

            chat.querySelectorAll("[data-info-id]").forEach(row => {
                const message = messages.get(row.dataset.infoId);
                const target = row.querySelector("[data-read-receipts]");

                if (
                    !message
                    || !target
                    || message.version !== Number(row.dataset.infoVersion)
                ) {
                    return;
                }

                target.textContent = message.read_receipts.length
                    ? message.read_receipts.map(receipt => {
                        if (!receipt.reader_id) {
                            return `${receipt.reader_name}: Lesestatus nicht verfügbar`;
                        }

                        return receipt.is_read
                            ? `${receipt.reader_name}: gelesen am ${new Date(receipt.read_at).toLocaleString("de-DE")}`
                            : `${receipt.reader_name}: ungelesen`;
                    }).join(" · ")
                    : "Lesestatus noch nicht erfasst";
            });
        } catch {
        } finally {
            refreshing = false;
        }
    };

    const sendReads = async () => {
        if (
            sending
            || document.visibilityState !== "visible"
            || !document.hasFocus()
        ) {
            return;
        }

        const rows = [...visible].filter(row =>
            row.dataset.infoDirection === chat.dataset.chatReadDirection
            && !acknowledged.has(
                `${row.dataset.infoId}:${row.dataset.infoVersion}`
            )
        ).slice(0, 250);

        if (!rows.length) return;

        const items = rows.map(row => ({
            id: row.dataset.infoId,
            version: Number(row.dataset.infoVersion)
        }));

        sending = true;

        try {
            const response = await fetch(chat.dataset.chatReadUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": chat.dataset.chatCsrf
                },
                body: JSON.stringify({ items })
            });

            if (response.ok && !response.redirected) {
                items.forEach(item =>
                    acknowledged.add(`${item.id}:${item.version}`)
                );
                await refreshStatus();
            }
        } catch {
        } finally {
            sending = false;
        }
    };

    const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (
                entry.isIntersecting
                && entry.intersectionRatio >= 0.01
            ) {
                visible.add(entry.target);
            } else {
                visible.delete(entry.target);
            }
        });
    }, { threshold: 0.01 });

    chat.querySelectorAll("[data-info-id]").forEach(
        row => observer.observe(row)
    );

    window.setInterval(sendReads, 1000);
    window.setInterval(refreshStatus, 15000);
    window.addEventListener("focus", sendReads);
    document.addEventListener("visibilitychange", sendReads);
    refreshStatus();
});