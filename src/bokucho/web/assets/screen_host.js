export function createScreenHost(container, { persistentRoutes = new Set() } = {}) {
  const cached = new Map();
  let active = null;

  function dispose(entry) {
    if (entry.screen?.unmount) entry.screen.unmount();
    entry.host.remove();
  }

  function deactivate() {
    delete container.dataset.activeRoute;
    if (active === null) return;
    if (active.persistent) {
      active.host.hidden = true;
    } else {
      dispose(active);
    }
    active = null;
  }

  function show(route, screenModule, ctx) {
    if (active?.route === route) return active.screen;

    deactivate();
    let entry = cached.get(route);
    if (entry === undefined) {
      const host = document.createElement("div");
      host.dataset.route = route;
      container.append(host);
      entry = {
        route,
        host,
        screen: screenModule.mount(host, ctx),
        persistent: persistentRoutes.has(route),
      };
      if (entry.persistent) cached.set(route, entry);
    } else {
      // A kept screen holds whatever it read when it was last shown. Anything
      // changed elsewhere -- the chosen interface, a finished job -- is only
      // visible if it asks again on the way back in.
      Promise.resolve(entry.screen?.refresh?.()).catch((err) => ctx.showError(err));
    }

    entry.host.hidden = false;
    container.dataset.activeRoute = route;
    active = entry;
    return entry.screen;
  }

  function clear() {
    delete container.dataset.activeRoute;
    if (active !== null && !active.persistent) dispose(active);
    for (const entry of cached.values()) dispose(entry);
    cached.clear();
    active = null;
    container.replaceChildren();
  }

  return { show, hide: deactivate, clear };
}
