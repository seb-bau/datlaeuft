document.addEventListener("DOMContentLoaded", () => {
  const modal = document.getElementById("documentServicePositionModal");
  if (!modal) return;
  const items = JSON.parse(document.getElementById("documentServicePositionData").textContent);
  const craft = document.getElementById("servicePositionCraft");
  const position = document.getElementById("servicePositionItem");
  const add = document.getElementById("servicePositionAdd");
  const status = document.getElementById("servicePositionStatus");
  const error = document.getElementById("servicePositionError");
  const offerCraft = document.getElementById("craftActivity");
  const positions = document.getElementById("offerPositions") || document.getElementById("invoicePositions");

  function selectedItem() {
    if (!craft.value || !position.value) return null;
    return items.find(item => String(item.id) === position.value && String(item.craft_activity_id) === craft.value);
  }

  function refresh() {
    error.hidden = true;
    add.disabled = true;
    position.replaceChildren(new Option(craft.value ? "Bitte Leistungsposition auswählen" : "Bitte zuerst ein Gewerk auswählen", ""));
    const filtered = craft.value ? items.filter(item => String(item.craft_activity_id) === craft.value) : [];
    filtered.forEach(item => position.add(new Option([item.code, item.title].filter(Boolean).join(" · "), String(item.id))));
    position.disabled = !filtered.length;
    status.textContent = !craft.value ? "Bitte zuerst ein Gewerk auswählen." : filtered.length ? `${filtered.length} Leistungspositionen verfügbar.` : "Für dieses Gewerk sind keine Leistungspositionen verfügbar.";
  }

  modal.addEventListener("show.bs.modal", () => {
    craft.value = "";
    refresh();
  });
  modal.addEventListener("shown.bs.modal", () => craft.focus());
  craft.addEventListener("change", refresh);
  position.addEventListener("change", () => { add.disabled = !selectedItem(); });
  add.addEventListener("click", () => {
    const item = selectedItem();
    if (!item) return;
    if (modal.dataset.documentType === "offer") {
      const existing = Array.from(positions.querySelectorAll('[name="position_task_template_id"]'))
        .map(input => items.find(entry => String(entry.id) === input.value)).filter(Boolean);
      if (existing.some(entry => entry.craft_activity_id !== item.craft_activity_id)) {
        error.textContent = "Die vorhandenen Leistungspositionen gehören zu einem anderen Gewerk. Bitte dieses Gewerk auswählen oder die vorhandenen Leistungspositionen entfernen.";
        error.hidden = false;
        return;
      }
      offerCraft.value = String(item.craft_activity_id);
      offerCraft.dispatchEvent(new Event("change", {bubbles: true}));
    }
    createPositionRow({sourceType: "catalog", taskTemplateId: item.id, description: item.title, articleNumber: item.code});
    positions.lastElementChild.dispatchEvent(new Event("input", {bubbles: true}));
    bootstrap.Modal.getOrCreateInstance(modal).hide();
  });
  refresh();
});
