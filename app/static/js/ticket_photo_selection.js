document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("commissionForm");
    if (!form) return;

    function photoCard(filename, imageUrl, contentUrl, remove) {
        const column = document.createElement("div");
        column.className = "col-6 col-md-4 col-xl-3";
        const card = document.createElement("div");
        card.className = "border rounded p-2 h-100";
        const link = document.createElement("a");
        link.href = contentUrl;
        link.target = "_blank";
        link.rel = "noopener";
        const image = document.createElement("img");
        image.src = imageUrl;
        image.alt = filename;
        image.className = "img-fluid rounded w-100";
        image.style.height = "130px";
        image.style.objectFit = "contain";
        link.appendChild(image);
        const name = document.createElement("div");
        name.className = "small text-truncate my-1";
        name.textContent = filename;
        name.title = filename;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-sm btn-outline-danger";
        button.textContent = "Entfernen";
        button.setAttribute("aria-label", `${filename} aus dem Auftrag entfernen`);
        button.addEventListener("click", remove);
        image.addEventListener("error", () => {
            image.hidden = true;
            name.textContent = `${filename} – Vorschau nicht verfügbar`;
            name.classList.remove("text-truncate");
        });
        card.append(link, name, button);
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
            uploads.appendChild(photoCard(file.name, url, url, () => {
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

    const selection = document.getElementById("ticketPhotoSelection");
    if (!selection) return;
    const ready = document.getElementById("ticketPhotoSelectionReady");
    const gallery = document.getElementById("ticketPhotoGallery");
    const status = document.getElementById("ticketPhotoStatus");
    const retry = document.getElementById("retryTicketPhotos");
    const restore = document.getElementById("restoreTicketPhotos");
    const initial = JSON.parse(document.getElementById("initialTicketPhotoSelection").textContent);
    let selected = initial === null ? null : new Set(initial);
    let photos = [];
    let isLoading = false;

    function notifyState() {
        form.dispatchEvent(new Event("ticket-photos-updated"));
    }

    function renderSelection() {
        gallery.replaceChildren();
        let count = 0;
        for (const photo of photos) {
            if (!selected.has(photo.file_guid)) continue;
            count += 1;
            const card = photoCard(photo.filename, photo.thumbnail_url, photo.content_url, () => {
                selected.delete(photo.file_guid);
                renderSelection();
            });
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "ticket_photo_guid";
            input.value = photo.file_guid;
            card.appendChild(input);
            gallery.appendChild(card);
        }
        status.textContent = photos.length ? `${count} von ${photos.length} Ticketfotos werden übernommen.` : "Keine Fotos im Ticket vorhanden.";
        restore.classList.toggle("d-none", count === photos.length);
    }

    async function loadPhotos() {
        if (isLoading) return;
        isLoading = true;
        ready.value = "";
        retry.classList.add("d-none");
        status.textContent = "Ticketfotos werden geladen …";
        notifyState();
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(selection.dataset.photosUrl, {signal: controller.signal, headers: {Accept: "application/json"}});
            const payload = await response.json();
            if (!response.ok || !Array.isArray(payload.items)) throw new Error(payload.message || "Die Ticketfotos konnten nicht geladen werden.");
            photos = payload.items;
            if (selected === null) selected = new Set(photos.map(photo => photo.file_guid));
            renderSelection();
            ready.value = "true";
        } catch (error) {
            status.textContent = error.name === "AbortError"
                ? "Das Laden der Ticketfotos hat zu lange gedauert. Bitte erneut versuchen."
                : error.name === "Error" ? error.message : "Die Ticketfotos konnten nicht geladen werden. Bitte erneut versuchen.";
            retry.classList.remove("d-none");
        } finally {
            clearTimeout(timeout);
            isLoading = false;
            notifyState();
        }
    }

    restore.addEventListener("click", () => {
        selected = new Set(photos.map(photo => photo.file_guid));
        renderSelection();
    });
    retry.addEventListener("click", loadPhotos);
    form.addEventListener("submit", event => {
        if (ready.value !== "true") {
            event.preventDefault();
            status.focus();
        }
    });
    loadPhotos();
});
