/* Menu bar actions (mac_app.MENU_LAYOUT) press the same buttons as the window. */
(() => {
  const special = {
    tutorial: () => window.MixerTutorial?.start(),
    about: () => window.zuckerOpenAbout?.(),
  };
  window.zuckerMenu = (action) => {
    if (special[action]) { special[action](); return true; }
    const button = document.getElementById(action);
    if (!button) return false;
    if (button.disabled || button.hidden) {
      if (typeof showToast === 'function') showToast(`${button.textContent.trim()} is not available right now.`);
      return false;
    }
    button.click();
    return true;
  };
})();
