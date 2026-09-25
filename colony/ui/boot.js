// Applies the saved theme, text size and hand-mixed colours before first paint.
// The modules in js/ load after parsing, so without this the page shows a
// frame of the default palette first. js/appearance.js applies and checks the
// same settings.
(function () {
  try {
    var root = document.documentElement;
    var theme = new URLSearchParams(location.search).get("theme")
      || localStorage.getItem("colony-theme");
    if (theme) root.dataset.theme = theme;
    // 1.15 is DEFAULT_SCALE in js/appearance.js.
    var scale = Number(localStorage.getItem("colony-scale")) || 1.15;
    root.style.setProperty("--ui-scale", String(scale));
    var vars = JSON.parse(localStorage.getItem("colony-vars") || "null") || {};
    for (var name in vars) {
      if (name.indexOf("--") === 0) root.style.setProperty(name, vars[name]);
    }
  } catch (_) {
    // No storage: the defaults stand, and js/appearance.js says nothing either.
  }
})();
