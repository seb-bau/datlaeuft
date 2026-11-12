document.addEventListener("DOMContentLoaded", () => {
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
