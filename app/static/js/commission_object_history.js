document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("commissionForm");
    const summary = document.getElementById("objectSummary");
    const modal = document.getElementById("objectCommissionModal");
    if (!form?.dataset.objectCommissionsUrl || !summary || !modal) return;
    const list = document.getElementById("objectCommissionList");
    const status = document.getElementById("objectCommissionStatus");
    const selection = document.getElementById("objectCommissionSelection");
    const view = document.getElementById("objectCommissionView");
    const previous = document.getElementById("objectCommissionPrevious");
    const next = document.getElementById("objectCommissionNext");
    const pageLabel = document.getElementById("objectCommissionPage");
    const refresh = document.getElementById("objectCommissionRefresh");
    const levelControls = document.getElementById("objectCommissionLevels");
    const levelLabels = {license_agreement: "Mietvertrag", use_unit: "Wohnung", building: "Gebäude", economic_unit: "Wirtschaftseinheit"};
    let activeLevels = new Set();
    let selectedItem = null;
    let historyButton = null;
    let counts = null;
    let selectionVersion = 0;
    let requestVersion = 0;
    let countVersion = 0;
    let page = 1;

    function element(tag, className, text = "") {
        const node = document.createElement(tag);
        node.className = className;
        node.textContent = text;
        return node;
    }

    async function fetchHistory(item, parameters) {
        const url = new URL(form.dataset.objectCommissionsUrl, window.location.origin);
        url.searchParams.set("object_type", item.object_type);
        url.searchParams.set("object_id", item.object_id);
        for (const [key, value] of Object.entries(parameters)) url.searchParams.set(key, value);
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 12000);
        try {
            const response = await fetch(url.toString(), {signal: controller.signal});
            if (!response.ok) {
                const payload = await response.json().catch(() => ({}));
                throw new Error(payload.message || "Die Aufträge konnten nicht geladen werden. Bitte erneut versuchen.");
            }
            return await response.json().catch(() => {
                throw new Error("Die Aufträge konnten nicht geladen werden. Bitte erneut versuchen.");
            });
        } catch (error) {
            if (error instanceof TypeError) throw new Error("Die Aufträge konnten nicht geladen werden. Bitte erneut versuchen.");
            throw error;
        } finally {
            clearTimeout(timeout);
        }
    }

    function updateButton(error = false) {
        if (!historyButton) return;
        historyButton.className = "btn btn-sm mt-2 " + (counts?.open_count > 0 ? "btn-outline-warning text-body" : "btn-outline-secondary");
        historyButton.replaceChildren(element("i", "bi bi-list-ul me-2"), element("span", "", "Aufträge anzeigen"));
        if (error || !counts) {
            historyButton.appendChild(element("span", "small ms-2", error ? "Prüfung fehlgeschlagen" : "Prüfung läuft …"));
            return;
        }
        historyButton.appendChild(element("span", counts.open_count > 0 ? "badge text-bg-warning ms-2" : "badge text-bg-light ms-2", `${counts.open_count} offen`));
        historyButton.appendChild(element("span", "badge text-bg-light ms-2", `${counts.total} insgesamt`));
    }

    function renderLevels() {
        levelControls.replaceChildren();
        const levels = Object.keys(levelLabels);
        for (const name of levels.slice(levels.indexOf(selectedItem.object_type))) {
            if (name !== selectedItem.object_type && !selectedItem[name]) continue;
            const active = activeLevels.has(name);
            const label = [levelLabels[name], selectedItem[name]?.id_num, name === selectedItem.object_type ? "(Auswahl)" : null].filter(Boolean).join(" ");
            const button = element("button", "btn btn-sm " + (active ? "btn-primary" : "btn-outline-secondary"), label);
            button.type = "button";
            button.setAttribute("aria-pressed", String(active));
            button.disabled = active && activeLevels.size === 1;
            button.addEventListener("click", () => {
                if (activeLevels.has(name)) {
                    if (activeLevels.size === 1) return;
                    activeLevels.delete(name);
                } else {
                    activeLevels.add(name);
                }
                renderLevels();
                return loadPage(1);
            });
            levelControls.appendChild(button);
        }
    }

    function renderItem(item) {
        const card = element("article", "list-group-item py-3");
        const heading = element("div", "d-flex flex-wrap justify-content-between align-items-center gap-2");
        const link = element("a", "fw-semibold", item.number ? `Auftrag ${item.number}` : "Auftrag ohne Wowiport-Nummer");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = "Auftrag in einem neuen Tab öffnen";
        heading.append(link, element("span", item.is_open ? "badge text-bg-warning" : "badge text-bg-secondary", item.is_open ? "Offen" : "Abgeschlossen"));
        card.append(heading, element("h3", "h6 mt-2 mb-1", item.title));
        card.appendChild(element("div", "small text-muted", [
            item.status, `Erstellt: ${item.created_at}`, `Sachbearbeiter: ${item.created_by}`,
            item.craftsman ? `Handwerker: ${item.craftsman}` : "Handwerker: nicht zugewiesen",
        ].join(" · ")));
        if (item.object_label) card.appendChild(element("div", "small fw-semibold mt-1", item.object_label));
        if (item.description) card.appendChild(element("p", "commission-history-description small mt-2 mb-0", item.description));
        return card;
    }

    function applyCounts(payload, currentSelection, currentRequest) {
        if (currentSelection !== selectionVersion || currentRequest < countVersion) return;
        counts = payload.selection_counts || payload;
        countVersion = currentRequest;
        updateButton();
    }

    async function loadPage(requestedPage = 1, initial = false) {
        if (!selectedItem) return;
        const currentSelection = selectionVersion;
        const currentRequest = ++requestVersion;
        const item = selectedItem;
        previous.disabled = true;
        next.disabled = true;
        pageLabel.textContent = "";
        list.replaceChildren();
        status.className = "text-muted";
        status.textContent = "Aufträge werden geladen …";
        try {
            const payload = await fetchHistory(item, {view: view.value, page: requestedPage, levels: [...activeLevels].join(",")});
            applyCounts(payload, currentSelection, currentRequest);
            if (currentSelection !== selectionVersion || currentRequest !== requestVersion) return;
            if (initial && view.value === "open" && payload.open_count === 0 && payload.total > 0) {
                view.value = "all";
                return loadPage(1);
            }
            page = payload.page;
            const exactSelection = activeLevels.size === 1 && activeLevels.has(selectedItem.object_type);
            status.textContent = payload.total
                ? `${payload.open_count} offene von ${payload.total} vorhandenen Aufträgen ${exactSelection ? "zu genau dieser Auswahl" : "auf den gewählten Ebenen"}.`
                : (exactSelection ? "Zu genau dieser Auswahl sind noch keine Aufträge vorhanden." : "Auf den gewählten Ebenen sind noch keine Aufträge vorhanden.");
            if (payload.items.length) {
                list.replaceChildren(...payload.items.map(renderItem));
            } else if (view.value === "open" && payload.total) {
                list.appendChild(element("div", "list-group-item text-muted", "Es sind keine offenen Aufträge vorhanden. Unter „Alle Aufträge“ finden Sie die abgeschlossenen Aufträge."));
            }
            pageLabel.textContent = payload.total ? `Seite ${page} von ${payload.pages}` : "";
            previous.disabled = page <= 1;
            next.disabled = page >= payload.pages;
        } catch (error) {
            if (currentSelection !== selectionVersion || currentRequest !== requestVersion) return;
            status.className = "text-danger";
            status.textContent = error.name === "AbortError" ? "Das Laden der Aufträge dauert zu lange. Bitte erneut versuchen." : error.message;
            if (!counts) updateButton(true);
        }
    }

    summary.addEventListener("commission-object-selected", async event => {
        selectedItem = event.detail;
        activeLevels = new Set([selectedItem.object_type]);
        renderLevels();
        counts = null;
        countVersion = 0;
        const currentSelection = ++selectionVersion;
        requestVersion += 1;
        const currentRequest = requestVersion;
        historyButton = element("button", "");
        historyButton.type = "button";
        historyButton.setAttribute("data-bs-toggle", "modal");
        historyButton.setAttribute("data-bs-target", "#objectCommissionModal");
        historyButton.setAttribute("aria-live", "polite");
        summary.appendChild(historyButton);
        const scope = {
            license_agreement: ["Vertrag", selectedItem.license_agreement?.id_num],
            use_unit: ["Wohnung", selectedItem.use_unit?.id_num],
            building: ["Gebäude", selectedItem.building?.id_num],
            economic_unit: ["Wirtschaftseinheit", selectedItem.economic_unit?.id_num],
        }[selectedItem.object_type];
        selection.textContent = [selectedItem.label, scope?.filter(Boolean).join(" ")].filter(Boolean).join(" · ");
        updateButton();
        try {
            const payload = await fetchHistory(selectedItem, {summary: "true"});
            applyCounts(payload, currentSelection, currentRequest);
        } catch {
            if (currentSelection === selectionVersion && currentRequest >= countVersion) updateButton(true);
        }
    });
    modal.addEventListener("show.bs.modal", () => {
        activeLevels = new Set([selectedItem.object_type]);
        renderLevels();
        view.value = counts?.open_count === 0 ? "all" : "open";
        loadPage(1, true);
    });
    modal.addEventListener("hidden.bs.modal", () => { requestVersion += 1; });
    view.addEventListener("change", () => loadPage(1));
    previous.addEventListener("click", () => loadPage(page - 1));
    next.addEventListener("click", () => loadPage(page + 1));
    refresh.addEventListener("click", () => loadPage(page));
});
