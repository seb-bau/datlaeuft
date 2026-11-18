document.addEventListener("DOMContentLoaded", () => {
  const modal = document.getElementById("invoiceCoverLetterModal");
  if (!modal) return;
  const form = document.getElementById("invoiceCoverLetterForm");
  const frame = document.getElementById("coverLetterPreview");
  const hint = document.getElementById("coverLetterPreviewHint");
  const feedback = document.getElementById("coverLetterMessage");
  const remove = document.getElementById("coverLetterRemove");
  const preview = document.getElementById("coverLetterPreviewButton");
  const save = document.getElementById("coverLetterSave");
  const link = document.getElementById("invoiceCorrespondenceLink");
  let previewUrl = null;
  let busy = false;
  let ready = false;
  let generation = 0;

  function message(text, error = false) {
    feedback.textContent = text;
    feedback.className = "small mt-3 " + (error ? "text-danger" : "text-success");
  }

  function buttons() {
    preview.disabled = save.disabled = remove.disabled = busy || !ready;
    form.elements.title.disabled = form.elements.text.disabled = busy || !ready;
  }

  async function jsonResponse(response) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || response.redirected) throw new Error(data.message || "Der Vorgang konnte nicht ausgeführt werden. Bitte die Seite neu laden.");
    return data;
  }

  function clearPreview() {
    frame.removeAttribute("src");
    frame.hidden = true;
    hint.hidden = false;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  }

  modal.addEventListener("show.bs.modal", async () => {
    const current = ++generation;
    clearPreview();
    ready = false;
    buttons();
    message("Anschreiben wird geladen …");
    try {
      const data = await jsonResponse(await fetch(modal.dataset.url, {headers: {Accept: "application/json"}}));
      if (current !== generation) return;
      form.elements.title.value = data.title;
      form.elements.text.value = data.text;
      remove.hidden = !data.saved;
      document.getElementById("invoiceCoverLetterTitle").textContent = data.saved ? "Anschreiben bearbeiten" : "Anschreiben erstellen";
      ready = true;
      message("");
    } catch (error) { if (current === generation) message(error.message, true); }
    finally { if (current === generation) buttons(); }
  });

  modal.addEventListener("hidden.bs.modal", () => {
    generation += 1;
    clearPreview();
  });

  form.addEventListener("input", () => {
    clearPreview();
    message("Änderungen noch nicht gespeichert. Bitte die Vorschau aktualisieren.");
  });

  async function run(action) {
    if (busy || !ready) return;
    if (action !== "remove" && !form.reportValidity()) return;
    const data = new FormData(form);
    const current = generation;
    busy = true;
    buttons();
    message(action === "preview" ? "PDF-Vorschau wird erstellt …" : "Anschreiben wird gespeichert …");
    try {
      if (action === "preview") {
        const response = await fetch(modal.dataset.previewUrl, {method: "POST", body: data});
        if (!response.ok || response.redirected || !(response.headers.get("Content-Type") || "").includes("application/pdf")) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.message || "Die PDF-Vorschau konnte nicht erstellt werden.");
        }
        const blob = await response.blob();
        if (current !== generation) return;
        clearPreview();
        previewUrl = URL.createObjectURL(blob);
        frame.src = previewUrl;
        frame.hidden = false;
        hint.hidden = true;
        message("Vorschau aktualisiert. Das Anschreiben ist noch nicht gespeichert.");
      } else {
        const result = await jsonResponse(await fetch(modal.dataset.url, action === "remove"
          ? {method: "DELETE", headers: {"X-CSRFToken": data.get("csrf_token"), Accept: "application/json"}}
          : {method: "POST", body: data}));
        link.hidden = action === "remove";
        remove.hidden = action === "remove";
        if (current !== generation) return;
        if (action === "remove") clearPreview();
        message(result.message);
      }
    } catch (error) { if (current === generation) message(error.message, true); }
    finally { busy = false; buttons(); }
  }

  preview.addEventListener("click", () => run("preview"));
  form.addEventListener("submit", event => { event.preventDefault(); run("save"); });
  remove.addEventListener("click", () => { if (window.confirm("Das Anschreiben von dieser Rechnung entfernen?")) run("remove"); });
});
