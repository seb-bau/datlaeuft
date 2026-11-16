document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("commissionForm");
    if (!form) return;

    function attachmentCard(item, remove) {
        const column = document.createElement("div");
        column.className = "col-6 col-md-4 col-xl-3";
        const card = document.createElement("div");
        card.className = "border rounded p-2 h-100";
        const link = document.createElement("a");
        link.href = item.content_url;
        link.target = "_blank";
        link.rel = "noopener";
        const preview = document.createElement("div");
        preview.className = "rounded bg-body-tertiary text-center p-4";
        preview.style.height = "130px";
        const icon = document.createElement("i");
        icon.className = "bi " + (item.is_pdf ? "bi-file-earmark-pdf" : "bi-file-earmark") + " fs-1";
        icon.setAttribute("aria-hidden", "true");
        preview.appendChild(icon);
        const name = document.createElement("a");
        name.href = item.content_url;
        name.target = "_blank";
        name.rel = "noopener";
        name.className = "small text-truncate my-1";
        name.style.display = "block";
        name.textContent = item.filename;
        name.title = item.filename;
        if (item.thumbnail_url) {
            const image = document.createElement("img");
            image.src = item.thumbnail_url;
            image.alt = item.filename;
            image.className = "img-fluid rounded w-100";
            image.style.height = "130px";
            image.style.objectFit = "contain";
            preview.hidden = true;
            link.appendChild(image);
            image.addEventListener("error", () => {
                image.hidden = true;
                preview.hidden = false;
                name.textContent = `${item.filename} – Vorschau nicht verfügbar`;
                name.classList.remove("text-truncate");
            });
        }
        link.appendChild(preview);
        const type = document.createElement("div");
        type.className = "small text-muted mb-1";
        type.textContent = item.type_label || "Datei";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-sm btn-outline-danger";
        button.textContent = "Entfernen";
        button.setAttribute("aria-label", `${item.filename} aus dem Auftrag entfernen`);
        button.addEventListener("click", remove);
        card.append(link, name, type, button);
        column.appendChild(card);
        return column;
    }

    const upload = document.getElementById("photoUpload");
    const uploads = document.getElementById("uploadedPhotoPreviews");
    let uploadUrls = [];
    function renderUploads() {
        uploadUrls.forEach(url => URL.revokeObjectURL(url));
        uploadUrls = [];
        uploads.replaceChildren();
        Array.from(upload.files).forEach((file, index) => {
            const url = URL.createObjectURL(file);
            uploadUrls.push(url);
            uploads.appendChild(attachmentCard({filename: file.name, thumbnail_url: url, content_url: url, type_label: "Bild"}, () => {
                const transfer = new DataTransfer();
                Array.from(upload.files).forEach((item, itemIndex) => {
                    if (itemIndex !== index) transfer.items.add(item);
                });
                upload.files = transfer.files;
                renderUploads();
            }));
        });
    }
    if (upload && uploads) upload.addEventListener("change", renderUploads);
    window.addEventListener("pagehide", () => uploadUrls.forEach(url => URL.revokeObjectURL(url)));

    const selection = document.getElementById("ticketAttachmentSelection");
    if (!selection) return;
    const ready = document.getElementById("ticketAttachmentSelectionReady");
    const gallery = document.getElementById("ticketAttachmentGallery");
    const status = document.getElementById("ticketAttachmentStatus");
    const retry = document.getElementById("retryTicketAttachments");
    const restore = document.getElementById("restoreTicketAttachments");
    const initial = JSON.parse(document.getElementById("initialTicketAttachmentSelection").textContent);
    let selected = initial === null ? null : new Set(initial);
    let attachments = [];
    let isLoading = false;

    function notifyState() {
        form.dispatchEvent(new Event("ticket-attachments-updated"));
    }

    function renderSelection() {
        gallery.replaceChildren();
        let count = 0;
        for (const attachment of attachments) {
            if (!selected.has(attachment.file_guid)) continue;
            count += 1;
            const card = attachmentCard(attachment, () => {
                selected.delete(attachment.file_guid);
                renderSelection();
            });
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "ticket_attachment_guid";
            input.value = attachment.file_guid;
            card.appendChild(input);
            gallery.appendChild(card);
        }
        status.textContent = attachments.length ? `${count} von ${attachments.length} Ticketanhängen werden übernommen.` : "Keine Anhänge im Ticket vorhanden.";
        restore.classList.toggle("d-none", count === attachments.length);
    }

    async function loadAttachments() {
        if (isLoading) return;
        isLoading = true;
        ready.value = "";
        retry.classList.add("d-none");
        status.textContent = "Ticketanhänge werden geladen …";
        notifyState();
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(selection.dataset.attachmentsUrl, {signal: controller.signal, headers: {Accept: "application/json"}});
            const payload = await response.json();
            if (!response.ok || !Array.isArray(payload.items)) throw new Error(payload.message || "Die Ticketanhänge konnten nicht geladen werden.");
            attachments = payload.items;
            if (selected === null) selected = new Set(attachments.map(attachment => attachment.file_guid));
            renderSelection();
            ready.value = "true";
        } catch (error) {
            status.textContent = error.name === "AbortError"
                ? "Das Laden der Ticketanhänge hat zu lange gedauert. Bitte erneut versuchen."
                : error.name === "Error" ? error.message : "Die Ticketanhänge konnten nicht geladen werden. Bitte erneut versuchen.";
            retry.classList.remove("d-none");
        } finally {
            clearTimeout(timeout);
            isLoading = false;
            notifyState();
        }
    }

    restore.addEventListener("click", () => {
        selected = new Set(attachments.map(attachment => attachment.file_guid));
        renderSelection();
    });
    retry.addEventListener("click", loadAttachments);
    form.addEventListener("submit", event => {
        if (ready.value !== "true") {
            event.preventDefault();
            status.focus();
        }
    });
    loadAttachments();
});
