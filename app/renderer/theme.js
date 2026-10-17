// Sets html[data-theme] before the stylesheet paints. The main process resolves System / Light /
// Dark (Settings → General) through nativeTheme and passes the result in the URL; later changes,
// including the OS switching appearance, arrive through window.v2l.onTheme.
const root = document.documentElement;
const initial = new URLSearchParams(location.search).get("theme");
root.dataset.theme = initial === "dark" ? "dark" : "light";
window.v2l?.onTheme((theme) => { root.dataset.theme = theme === "dark" ? "dark" : "light"; });
