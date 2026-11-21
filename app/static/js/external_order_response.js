(() => {
  const form = document.getElementById('orderResponseForm');
  if (!form) return;
  const label = document.getElementById('responseTextLabel');
  const intro = document.getElementById('responseIntro');
  const update = () => {
    const accepted = form.elements.decision.value === 'accept';
    label.textContent = `${accepted ? 'Weitere Mitteilungen' : 'Ablehnungsgrund'} (optional)`;
    intro.textContent = accepted
      ? 'Vielen Dank, dass Sie den Auftrag übernehmen möchten. Bitte bestätigen Sie Ihre Entscheidung.'
      : 'Bitte bestätigen Sie Ihre Entscheidung zum Auftrag.';
  };
  form.addEventListener('change', update);
  update();
})();
