const ROUTES = new Set(["jobs", "jobs/new", "workers", "system"]);
const DEFAULT_ROUTE = "jobs";

export function normalizeRoute(hash = window.location.hash) {
  const value = String(hash || "").replace(/^#/, "").trim().toLowerCase();
  return ROUTES.has(value) ? value : DEFAULT_ROUTE;
}

export function createNavigation({ onRouteChange = () => {} } = {}) {
  let currentRoute = DEFAULT_ROUTE;

  function moveProfileEditor() {
    const editor = document.getElementById("modelProfileEditor");
    const host = document.getElementById("modelProfilesHost");
    if (editor && host && editor.parentElement !== host) {
      editor.open = true;
      host.append(editor);
    }
  }

  function render(route, { focus = false } = {}) {
    currentRoute = ROUTES.has(route) ? route : DEFAULT_ROUTE;
    document.body.dataset.route = currentRoute;
    document.querySelectorAll("[data-view]").forEach((element) => {
      element.hidden = element.dataset.view !== currentRoute;
    });
    document.querySelectorAll("[data-route-link]").forEach((link) => {
      const selected = link.dataset.routeLink === currentRoute
        || (currentRoute === "jobs/new" && link.dataset.routeLink === "jobs");
      if (selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    const newJobPanel = document.getElementById("newJobPanel");
    if (newJobPanel) newJobPanel.open = currentRoute === "jobs/new";
    onRouteChange(currentRoute);
    if (focus) {
      const visible = Array.from(document.querySelectorAll(`[data-view="${currentRoute}"]`))
        .find((element) => !element.hidden);
      const heading = visible && visible.querySelector("h1, h2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
      }
    }
  }

  function syncFromHash({ focus = false } = {}) {
    const route = normalizeRoute();
    if (window.location.hash !== `#${route}`) {
      window.history.replaceState(null, "", `#${route}`);
    }
    render(route, { focus });
  }

  function navigate(route) {
    const next = ROUTES.has(route) ? route : DEFAULT_ROUTE;
    if (normalizeRoute() === next && window.location.hash === `#${next}`) {
      render(next, { focus: true });
      return;
    }
    window.location.hash = next;
  }

  function init() {
    moveProfileEditor();
    window.addEventListener("hashchange", () => syncFromHash({ focus: true }));
    syncFromHash();
  }

  return {
    get route() { return currentRoute; },
    init,
    navigate,
    render,
  };
}
