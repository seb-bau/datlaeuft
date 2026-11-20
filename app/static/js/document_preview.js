document.addEventListener("DOMContentLoaded", () => {
  const modal = document.getElementById("documentPreviewModal");
  if (!modal) return;
  const frame = document.getElementById("documentPreviewFrame");
  const loading = document.getElementById("documentPreviewLoading");
  const errorMessage = document.getElementById("documentPreviewError");
  const open = document.getElementById("documentPreviewOpen");
  let controller;
  let objectURL;

  function clearPreview() {
    controller?.abort();
    frame.removeAttribute("src");
    open.removeAttribute("href");
    frame.hidden = true;
    open.hidden = true;
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = undefined;
  }

  document.querySelectorAll("[data-document-preview]").forEach(button => {
    button.addEventListener("click", async () => {
      const form = button.dataset.previewForm ? document.getElementById(button.dataset.previewForm) : null;
      if (form && !form.reportValidity()) return;
      clearPreview();
      const activeController = new AbortController();
      controller = activeController;
      errorMessage.hidden = true;
      loading.hidden = false;
      document.getElementById("documentPreviewTitle").textContent = button.dataset.previewTitle || "PDF-Vorschau";
      bootstrap.Modal.getOrCreateInstance(modal).show();
      let body;
      if (form) {
        body = new FormData(form);
        const allowedFiles = new Set((button.dataset.previewFiles || "").split(",").map(name => name.trim()).filter(Boolean));
        for (const [name, value] of Array.from(body.entries())) {
          if (value instanceof File && !allowedFiles.has(name)) body.delete(name);
        }
      }
      try {
        const response = await fetch(button.dataset.previewUrl, {
          method: form ? "POST" : "GET", body, signal: activeController.signal, headers: {Accept: "application/pdf, application/json"}
        });
        const contentType = response.headers.get("Content-Type") || "";
        if (!response.ok || response.redirected || !contentType.includes("application/pdf")) {
          const data = contentType.includes("application/json") ? await response.json() : {};
          throw new Error(data.error || "Die PDF-Vorschau konnte nicht erstellt werden. Bitte Eingaben und Anmeldung prüfen.");
        }
        const blob = await response.blob();
        if (activeController.signal.aborted) return;
        if (!blob.size) throw new Error("Die PDF-Vorschau ist leer.");
        objectURL = URL.createObjectURL(blob);
        frame.src = objectURL;
        open.href = objectURL;
        frame.hidden = false;
        open.hidden = false;
      } catch (error) {
        if (activeController.signal.aborted) return;
        errorMessage.textContent = error.message;
        errorMessage.hidden = false;
      } finally {
        if (controller === activeController) loading.hidden = true;
      }
    });
  });
  modal.addEventListener("hidden.bs.modal", clearPreview);
  window.addEventListener("pagehide", clearPreview);
});
