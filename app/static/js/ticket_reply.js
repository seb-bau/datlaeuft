document.addEventListener("DOMContentLoaded", () => {
    const modal = document.getElementById("ticketReplyModal");
    if (!modal) return;
    const form = modal.querySelector("form");
    const recipient = document.getElementById("ticketReplyRecipient");
    const suggestions = document.getElementById("ticketReplyRecipients");
    const responseText = document.getElementById("ticketReplyText");
    const status = document.getElementById("ticketReplyStatus");
    let requestId = 0;
    modal.addEventListener("show.bs.modal", async event => {
        const button = event.relatedTarget;
        if (!button) return;
        const currentRequestId = ++requestId;
        form.action = button.dataset.replyUrl;
        recipient.value = "";
        responseText.value = "";
        suggestions.replaceChildren();
        document.getElementById("ticketReplyTitle").textContent = `Kein Auftrag – Antwort zu Ticket ${button.dataset.ticketNumber}`;
        status.textContent = "Empfänger wird geladen …";
        try {
            const response = await fetch(button.dataset.recipientsUrl);
            const payload = await response.json();
            if (currentRequestId !== requestId) return;
            if (!response.ok) throw new Error(payload.message || "Empfänger konnte nicht geladen werden.");
            const addresses = payload.recipients || [];
            for (const address of addresses) {
                const option = document.createElement("option");
                option.value = address;
                suggestions.appendChild(option);
            }
            if (addresses.length === 1 && !recipient.value) recipient.value = addresses[0];
            status.textContent = addresses.length ? "Bitte den Empfänger vor dem Senden prüfen." : "Keine eindeutige Empfängeradresse gefunden. Bitte die Adresse des Mieters eintragen.";
        } catch (error) {
            if (currentRequestId === requestId) status.textContent = error.message;
        }
    });
    modal.addEventListener("hidden.bs.modal", () => { requestId += 1; });
});
