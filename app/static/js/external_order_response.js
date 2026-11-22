(() => {
  const form = document.getElementById('orderResponseForm');
  if (!form) return;
  const label = document.getElementById('responseTextLabel');
  const intro = document.getElementById('responseIntro');
  const update = () => {
    const accepted = form.elements.decision.value === 'accept';
    label.textContent = accepted ? form.dataset.acceptLabel : form.dataset.rejectLabel;
    intro.textContent = accepted ? form.dataset.acceptIntro : form.dataset.rejectIntro;
  };
  form.addEventListener('change', update);
  update();
})();
