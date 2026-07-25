(() => {
  "use strict";

  const body = document.body;
  if (!body.classList.contains("workspace-page") && !body.classList.contains("detail-page")) return;

  const isWorkspaceHome = body.classList.contains("workspace-page");
  const main = document.querySelector(".app-main");
  const toggles = document.querySelectorAll(".sidebar-toggle");

  toggles.forEach((toggle) => {
    toggle.addEventListener("click", () => {
      const open = body.classList.toggle("sidebar-open");
      toggles.forEach((button) => button.setAttribute("aria-expanded", String(open)));
    });
  });

  if (main) {
    main.addEventListener("click", (event) => {
      if (event.target.closest(".sidebar-toggle")) return;
      if (!body.classList.contains("sidebar-open")) return;
      body.classList.remove("sidebar-open");
      toggles.forEach((toggle) => toggle.setAttribute("aria-expanded", "false"));
    });
  }

  // Detail pages share the shell/sidebar but do not use hash SPA section switching.
  if (!isWorkspaceHome) return;

  const links = document.querySelectorAll('.sidebar-nav a[href^="#"], [data-section-link]');
  const sections = Array.from(main.querySelectorAll(":scope > section.band"));

  function targetSection(hash) {
    const id = (hash || "#overview").slice(1);
    const target = document.getElementById(id) || document.getElementById("overview");
    return target.closest("section.band") || target;
  }

  function activate(hash, updateHistory = false) {
    const requestedTarget = document.getElementById((hash || "#overview").slice(1));
    const section = targetSection(hash);
    const sidebarLinks = Array.from(document.querySelectorAll('.sidebar-nav a[href^="#"]'));
    const exactSidebarLink = sidebarLinks.find((link) => link.hash === hash);
    const fallbackSidebarLink = sidebarLinks.find((link) => targetSection(link.hash) === section);
    const activeSidebarLink = exactSidebarLink || fallbackSidebarLink;
    sections.forEach((candidate) => {
      candidate.hidden = candidate !== section;
    });
    const disclosure = requestedTarget ? requestedTarget.querySelector(":scope > .project-disclosure") : null;
    if (disclosure) disclosure.open = true;
    links.forEach((link) => {
      const active = link === activeSidebarLink;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    if (updateHistory && window.location.hash !== hash) history.pushState(null, "", hash);
    body.classList.remove("sidebar-open");
    toggles.forEach((toggle) => toggle.setAttribute("aria-expanded", "false"));
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  links.forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!link.hash) return;
      event.preventDefault();
      activate(link.hash, true);
    });
  });

  window.addEventListener("hashchange", () => activate(window.location.hash));

  function bindFilter(inputSelector, itemSelector, attribute) {
    const input = document.querySelector(inputSelector);
    if (!input) return;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLowerCase();
      document.querySelectorAll(itemSelector).forEach((item) => {
        const value = (item.getAttribute(attribute) || "").toLowerCase();
        item.hidden = Boolean(query) && !value.includes(query);
      });
    });
  }

  bindFilter("#project-filter", "[data-project-search]", "data-project-search");
  bindFilter("#data-source-filter", "[data-source-search]", "data-source-search");
  activate(window.location.hash || "#overview");
})();
