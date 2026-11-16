document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("ticketFilters");
    if (!form) {
        return;
    }

    const search = document.getElementById("ticketSearch");
    let searchTimer = null;
    let composing = false;
    const searchFocusKey = "ticketSearchFocus";

    try {
        const saved = JSON.parse(window.sessionStorage.getItem(searchFocusKey) || "null");
        window.sessionStorage.removeItem(searchFocusKey);
        if (search && saved?.value === search.value) {
            search.focus();
            search.setSelectionRange(saved.start, saved.end);
        }
    } catch {}

    function cancelSearch() {
        clearTimeout(searchTimer);
        searchTimer = null;
    }

    function submitFilters() {
        cancelSearch();
        form.requestSubmit();
    }

    function scheduleSearch() {
        cancelSearch();
        if (!composing) {
            searchTimer = setTimeout(submitFilters, 350);
        }
    }

    form.addEventListener("change", (event) => {
        if (event.target.matches('select, input[name="owner"]')) {
            submitFilters();
        }
    });
    form.addEventListener("submit", () => {
        cancelSearch();
        if (search && document.activeElement === search) {
            try {
                window.sessionStorage.setItem(searchFocusKey, JSON.stringify({
                    value: search.value, start: search.selectionStart, end: search.selectionEnd,
                }));
            } catch {}
        }
    });
    search?.addEventListener("input", scheduleSearch);
    search?.addEventListener("compositionstart", () => {
        composing = true;
        cancelSearch();
    });
    search?.addEventListener("compositionend", () => {
        composing = false;
        scheduleSearch();
    });

    const filter = document.getElementById("ticketOwnerFilter");
    if (!filter) {
        return;
    }

    const summary = filter.querySelector("summary");
    const checkboxes = Array.from(filter.querySelectorAll('input[name="owner"]'));

    function updateSummary() {
        const names = checkboxes.filter((checkbox) => checkbox.checked)
            .map((checkbox) => checkbox.closest("label").querySelector("span").textContent.trim());
        summary.textContent = names.length === 1
            ? names[0]
            : names.length ? `${names.length} ausgewählt` : "Sachbearbeiter auswählen";
        summary.title = names.join(", ");
        summary.setAttribute("aria-label", `Tickets nach Sachbearbeitern filtern: ${summary.textContent}`);
    }

    filter.addEventListener("change", (event) => {
        const changed = event.target;
        if (!checkboxes.includes(changed)) {
            return;
        }
        if (changed.checked) {
            checkboxes.forEach((checkbox) => {
                if (checkbox !== changed && (changed.value === "all" || checkbox.value === "all")) {
                    checkbox.checked = false;
                }
            });
        }
        updateSummary();
    });

    document.addEventListener("click", (event) => {
        if (!filter.contains(event.target)) {
            filter.open = false;
        }
    });

    filter.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            filter.open = false;
            summary.focus();
        }
    });

    updateSummary();
});
