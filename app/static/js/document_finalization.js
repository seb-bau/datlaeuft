document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-document-finalize]").forEach(button => {
    const form = button.form;
    if (!form) return;
    let submitting = false;
    form.addEventListener("submit", event => {
      if (event.submitter !== button) return;
      if (submitting) {
        event.preventDefault();
        return;
      }
      if (event.defaultPrevented) return;
      if (!window.confirm(button.dataset.confirmMessage)) {
        event.preventDefault();
        return;
      }
      setTimeout(() => {
        if (event.defaultPrevented) return;
        submitting = true;
        button.disabled = true;
        button.textContent = button.dataset.pendingText;
      }, 0);
    });
  });
});
