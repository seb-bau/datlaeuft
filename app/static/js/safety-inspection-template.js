(() => {
  const config = JSON.parse(document.getElementById("templateEditorConfig").textContent);
  const itemRows = config.rows;
  const tableBody = document.getElementById("itemTableBody");
  const form = document.getElementById("templateForm");
  const state = document.getElementById("templateTransferState");
  let busy = false;

  class TransferError extends Error {}

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  function actions(index) {
    if (!config.editable) return "";
    return `<td><div class="btn-group btn-group-sm">
      <button class="btn btn-outline-secondary" type="button" data-action="up" aria-label="Nach oben" ${index === 0 ? "disabled" : ""}>↑</button>
      <button class="btn btn-outline-secondary" type="button" data-action="down" aria-label="Nach unten" ${index === itemRows.length - 1 ? "disabled" : ""}>↓</button>
      <button class="btn btn-outline-danger" type="button" data-action="remove">Entfernen</button>
    </div></td>`;
  }

  function renderItems() {
    let number = 0;
    tableBody.innerHTML = itemRows.map((row, index) => {
      const disabled = config.editable ? "" : "disabled";
      const title = `<input class="form-control form-control-sm item-title" value="${escapeHtml(row.title)}" ${disabled} required>`;
      if (row.row_type === "heading") {
        return `<tr class="table-secondary" data-index="${index}">
          <td colspan="4"><div class="small fw-semibold mb-1">Überschrift</div>${title}</td>
          ${actions(index)}</tr>`;
      }
      number += 1;
      const flags = config.flags.map(([key, label]) => `<div class="form-check">
        <label class="form-check-label"><input class="form-check-input" type="checkbox" data-field="${key}" ${row[key] ? "checked" : ""} ${disabled}> ${escapeHtml(label)}</label>
      </div>`).join("");
      return `<tr data-index="${index}"><td class="fw-semibold text-center">${number}</td>
        <td>${title}</td><td>
          <textarea class="form-control form-control-sm mb-2 item-description" rows="2" placeholder="Beschreibung" ${disabled}>${escapeHtml(row.description)}</textarea>
          <textarea class="form-control form-control-sm item-help-text" rows="2" placeholder="Hinweis für den Prüfer" ${disabled}>${escapeHtml(row.help_text)}</textarea>
        </td><td>${flags}</td>${actions(index)}</tr>`;
    }).join("");
    document.getElementById("emptyItems").classList.toggle("d-none", itemRows.length > 0);
  }

  function readItems() {
    return Array.from(tableBody.querySelectorAll("tr")).map((element) => {
      const original = itemRows[Number(element.dataset.index)];
      const row = {
        ...original,
        row_type: original.row_type || "check",
        title: element.querySelector(".item-title").value.trim(),
        description: element.querySelector(".item-description")?.value.trim() ?? original.description ?? "",
        help_text: element.querySelector(".item-help-text")?.value.trim() ?? original.help_text ?? "",
      };
      config.flags.forEach(([key]) => {
        row[key] = row.row_type === "check" && Boolean(element.querySelector(`[data-field="${key}"]`)?.checked);
      });
      return row;
    });
  }

  function synchronizeItems() {
    itemRows.splice(0, itemRows.length, ...readItems());
  }

  function planData() {
    return {
      name: form.elements.name.value,
      description: form.elements.description.value,
      legal_reference: form.elements.legal_reference.value,
      change_note: form.elements.change_note.value,
      active: form.elements.active.checked,
    };
  }

  async function request(url, options) {
    let response;
    try {
      response = await fetch(url, {credentials: "same-origin", ...options});
    } catch {
      throw new TransferError("Die Verbindung ist fehlgeschlagen. Bitte erneut versuchen.");
    }
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch {}
      throw new TransferError(payload.error || "Die Übertragung ist fehlgeschlagen. Bitte die Seite neu laden.");
    }
    if ((response.headers.get("Content-Type") || "").includes("text/html")) {
      throw new TransferError("Bitte die Seite neu laden und gegebenenfalls erneut anmelden.");
    }
    return response;
  }

  async function transfer(operation) {
    if (busy) return;
    busy = true;
    const buttons = document.querySelectorAll("[data-export-format], #templateImportButton");
    buttons.forEach((button) => { button.disabled = true; });
    state.textContent = "Datei wird verarbeitet …";
    try {
      await operation();
    } catch (error) {
      state.textContent = error instanceof TransferError ? error.message : "Die Datei konnte nicht verarbeitet werden.";
    } finally {
      busy = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  document.querySelectorAll("[data-export-format]").forEach((button) => {
    button.addEventListener("click", () => transfer(async () => {
      const format = button.dataset.exportFormat;
      const response = await request(config.exportUrl.replace("FILE_FORMAT", format), {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": form.elements.csrf_token.value},
        body: JSON.stringify({plan: planData(), items: readItems()}),
      });
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `pruefdetailplan-v${config.versionNumber}.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      state.textContent = "Export erstellt.";
    }));
  });

  if (config.editable) {
    function addRow(rowType) {
      if (itemRows.length >= config.maxRows) {
        state.textContent = `Ein Plan darf höchstens ${config.maxRows} Zeilen enthalten.`;
        return;
      }
      synchronizeItems();
      const row = {stable_key: "", row_type: rowType, title: "", description: "", help_text: ""};
      config.flags.forEach(([key, , defaultValue]) => { row[key] = rowType === "check" && defaultValue; });
      itemRows.push(row);
      renderItems();
      tableBody.querySelector("tr:last-child .item-title").focus();
    }
    document.getElementById("addItemButton").addEventListener("click", () => addRow("check"));
    document.getElementById("addHeadingButton").addEventListener("click", () => addRow("heading"));
    tableBody.addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      synchronizeItems();
      const index = Number(button.closest("tr").dataset.index);
      const action = button.dataset.action;
      if (action === "remove") itemRows.splice(index, 1);
      if (action === "up" && index > 0) [itemRows[index - 1], itemRows[index]] = [itemRows[index], itemRows[index - 1]];
      if (action === "down" && index + 1 < itemRows.length) [itemRows[index + 1], itemRows[index]] = [itemRows[index], itemRows[index + 1]];
      renderItems();
    });
    document.getElementById("templateImportButton").addEventListener("click", () => transfer(async () => {
      const input = document.getElementById("templateImportFile");
      const file = input.files[0];
      if (!file) throw new TransferError("Bitte eine CSV- oder XLSX-Datei auswählen.");
      const payload = new FormData();
      payload.append("file", file);
      payload.append("csrf_token", form.elements.csrf_token.value);
      const response = await request(config.importUrl, {method: "POST", body: payload});
      const imported = await response.json();
      synchronizeItems();
      const append = document.getElementById("templateImportMode").value === "append";
      if (append && itemRows.length + imported.items.length > config.maxRows) {
        throw new TransferError(`Zusammen wären es mehr als ${config.maxRows} Zeilen. Bitte „Ersetzen“ wählen oder Einträge entfernen.`);
      }
      itemRows.splice(append ? itemRows.length : 0, append ? 0 : itemRows.length, ...imported.items);
      if (document.getElementById("templateImportPlan").checked && imported.plan) {
        ["name", "description", "legal_reference", "change_note"].forEach((key) => {
          form.elements[key].value = imported.plan[key] || "";
        });
        form.elements.active.checked = imported.plan.active;
      }
      renderItems();
      input.value = "";
      state.textContent = "Import übernommen. Zum Speichern bitte „Entwurf speichern“ wählen.";
    }));
    form.addEventListener("submit", (event) => {
      if (busy) {
        event.preventDefault();
        state.textContent = "Bitte die laufende Dateiübertragung abwarten.";
        return;
      }
      synchronizeItems();
      document.getElementById("itemsJson").value = JSON.stringify(itemRows);
    });
  }
  renderItems();
})();