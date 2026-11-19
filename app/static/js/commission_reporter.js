document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("commissionForm");
    const search = document.getElementById("reporterSearch");
    if (!form || !search) return;

    const field = document.getElementById("reporterField");
    const text = document.getElementById("reporter");
    const agreement = document.getElementById("reporterAgreement");
    const contractor = document.getElementById("reporterContractor");
    const results = document.getElementById("reporterResults");
    const status = document.getElementById("reporterStatus");
    const selection = document.getElementById("reporterSelection");
    const clear = document.getElementById("clearReporter");
    let revision = 0;
    let selectionRevision = 0;
    let timer = null;
    let loadingContact = false;

    function available() {
        return ["building", "economic_unit"].includes(document.getElementById("objectType").value);
    }

    function message(value, error = false) {
        status.textContent = value;
        status.classList.toggle("text-danger", error);
    }

    function updateSelection() {
        const selected = Boolean(agreement.value && contractor.value);
        selection.classList.toggle("d-none", !selected);
    }

    function clearResults() {
        results.replaceChildren();
        results.setAttribute("aria-busy", "false");
        search.setAttribute("aria-expanded", "false");
    }

    function updateScope() {
        const enabled = available();
        field.classList.toggle("d-none", !enabled);
        for (const input of [search, text, agreement, contractor]) input.disabled = !enabled;
        if (!enabled) {
            revision += 1;
            selectionRevision += 1;
            loadingContact = false;
            clearTimeout(timer);
            search.value = "";
            clearResults();
            message("");
        }
        updateSelection();
    }

    async function selectTenant(item, tenant) {
        const currentRevision = ++selectionRevision;
        loadingContact = true;
        message("Kontaktdaten des Melders werden geladen …");
        try {
            const url = form.dataset.reporterContactUrl.replace(/\/0\/0$/, `/${item.object_id}/${tenant.contractor_id}`);
            const response = await fetch(url);
            const payload = await response.json();
            if (!available() || currentRevision !== selectionRevision) return;
            if (!response.ok || !payload.item?.text || payload.item.agreement_id !== item.object_id || payload.item.contractor_id !== tenant.contractor_id) {
                throw new Error(payload.message || "Die Kontaktdaten des Melders konnten nicht geladen werden.");
            }
            agreement.value = String(payload.item.agreement_id);
            contractor.value = String(payload.item.contractor_id);
            text.value = payload.item.text;
            search.value = "";
            revision += 1;
            clearTimeout(timer);
            clearResults();
            updateSelection();
            message("");
        } catch (error) {
            if (available() && currentRevision === selectionRevision) {
                message(error instanceof Error ? error.message : "Die Kontaktdaten des Melders konnten nicht geladen werden.", true);
            }
        } finally {
            if (currentRevision === selectionRevision) loadingContact = false;
        }
    }

    async function loadResults(query, currentRevision) {
        results.setAttribute("aria-busy", "true");
        message("Suche läuft …");
        try {
            const url = new URL(form.dataset.reporterSearchUrl, window.location.origin);
            url.searchParams.set("q", query);
            const response = await fetch(url.toString());
            const payload = await response.json();
            if (!available() || currentRevision !== revision) return;
            if (!response.ok) throw new Error(payload.message || "Die Meldersuche ist fehlgeschlagen.");
            clearResults();
            const items = payload.items.filter(item => item.object_type === "license_agreement" && item.tenants?.length);
            for (const item of items) {
                const heading = document.createElement("div");
                heading.className = "list-group-item bg-body-tertiary";
                const title = document.createElement("div");
                title.className = "fw-semibold";
                title.textContent = `Mietvertrag ${item.license_agreement?.id_num || item.label}`;
                const details = document.createElement("div");
                details.className = "small text-body-secondary";
                details.textContent = [item.use_unit?.address, item.use_unit?.description].filter(Boolean).join(" · ");
                heading.append(title, details);
                results.appendChild(heading);
                for (const tenant of item.tenants) {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "list-group-item list-group-item-action ps-4 py-2";
                    button.textContent = tenant.name;
                    button.setAttribute("aria-label", `${tenant.name} als Melder auswählen, Mietvertrag ${item.license_agreement?.id_num || item.label}`);
                    button.addEventListener("click", () => selectTenant(item, tenant));
                    results.appendChild(button);
                }
            }
            search.setAttribute("aria-expanded", items.length ? "true" : "false");
            message(items.length ? "" : "Keine passenden Mietverträge mit aktuell gültigen Mietern gefunden.");
        } catch (error) {
            if (!available() || currentRevision !== revision) return;
            clearResults();
            message(error instanceof Error ? error.message : "Die Meldersuche ist fehlgeschlagen.", true);
        }
    }

    function scheduleSearch(delay = 300) {
        clearTimeout(timer);
        const currentRevision = ++revision;
        selectionRevision += 1;
        loadingContact = false;
        clearResults();
        message("");
        const query = search.value.trim();
        if (available() && query.length >= 2) {
            timer = setTimeout(() => loadResults(query, currentRevision), delay);
        }
    }

    search.addEventListener("input", () => scheduleSearch());
    search.addEventListener("keydown", event => {
        if (event.key === "Enter") {
            event.preventDefault();
            scheduleSearch(0);
        }
        if (event.key === "Escape") {
            clearTimeout(timer);
            revision += 1;
            clearResults();
            message("");
        }
    });
    clear.addEventListener("click", () => {
        revision += 1;
        selectionRevision += 1;
        loadingContact = false;
        clearTimeout(timer);
        text.value = agreement.value = contractor.value = search.value = "";
        clearResults();
        updateSelection();
        message("");
        search.focus();
    });
    form.addEventListener("submit", event => {
        if (loadingContact) {
            event.preventDefault();
            message("Bitte warten, bis die Kontaktdaten des Melders geladen wurden.");
        }
    });
    document.getElementById("objectSummary").addEventListener("commission-object-selected", updateScope);
    updateScope();
});
