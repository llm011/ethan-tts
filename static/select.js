/* Custom select: replaces the OS-drawn popup with a styled listbox.
 *
 * The native <select> stays in the DOM as the data source, so existing code
 * keeps reading and writing `.value` and listening for `change`. It is hidden
 * visually; this module renders the trigger and popup and mirrors state back.
 */
(function () {
  "use strict";

  const OPEN_CLASS = "cs-open";
  let openInstance = null;

  function place(popup, trigger) {
    const rect = trigger.getBoundingClientRect();
    const gap = 6;
    const margin = 12;
    // Long voice names must not push the popup past the viewport edge.
    const maxWidth = Math.max(160, window.innerWidth - margin * 2);
    popup.style.maxWidth = `${maxWidth}px`;
    popup.style.minWidth = `${Math.min(Math.max(rect.width, 170), maxWidth)}px`;
    const left = Math.max(margin, Math.min(rect.left, window.innerWidth - popup.offsetWidth - margin));
    popup.style.left = `${left}px`;
    const below = window.innerHeight - rect.bottom;
    const needed = popup.offsetHeight + gap + 12;
    if (below < needed && rect.top > needed) {
      popup.style.top = `${rect.top - popup.offsetHeight - gap}px`;
      popup.dataset.flip = "up";
    } else {
      popup.style.top = `${rect.bottom + gap}px`;
      popup.dataset.flip = "down";
    }
  }

  function build(select) {
    if (select.dataset.customSelect === "ready") return;

    const wrap = document.createElement("div");
    // Do not copy the native classes: .input/.voice-select carry the old
    // chevron background, which would paint a second arrow on the wrapper.
    wrap.className = "cs-select";
    if (select.id === "synth-voice") wrap.classList.add("cs-lg");

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cs-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");

    const label = document.createElement("span");
    label.className = "cs-label";

    const chevron = document.createElement("span");
    chevron.className = "cs-chevron";
    chevron.innerHTML =
      '<svg width="10" height="6" viewBox="0 0 10 6" aria-hidden="true">' +
      '<path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round"/></svg>';

    trigger.append(label, chevron);
    wrap.append(trigger);

    select.parentNode.insertBefore(wrap, select);
    wrap.append(select);
    select.classList.add("cs-native");
    select.setAttribute("tabindex", "-1");
    // Keep the element focusable for assistive tech but out of the tab order.
    select.dataset.customSelect = "ready";

    const popup = document.createElement("div");
    popup.className = "cs-popup";
    popup.setAttribute("role", "listbox");
    popup.hidden = true;
    document.body.append(popup);

    const state = { active: -1, options: [] };

    function options() {
      return Array.from(select.options).filter((o) => !o.disabled);
    }

    function currentIndex() {
      return options().findIndex((o) => o.value === select.value);
    }

    function paintTrigger() {
      const opt = select.selectedOptions[0] || select.options[0];
      const empty = !select.options.length;
      label.textContent = empty
        ? (select.dataset.placeholder || "暂无可选项")
        : (opt ? opt.textContent.trim() : "");
      trigger.title = label.textContent;
      label.classList.toggle("cs-placeholder", empty || !select.value);
      trigger.disabled = select.disabled || empty;
      wrap.classList.toggle("cs-disabled", select.disabled);
      trigger.disabled = select.disabled;
    }

    function paintPopup(keepActive) {
      const items = options();
      state.options = items;
      popup.innerHTML = "";
      items.forEach((opt, i) => {
        const row = document.createElement("div");
        row.className = "cs-option";
        row.setAttribute("role", "option");
        row.dataset.value = opt.value;
        const full = opt.textContent.trim();
        const text = document.createElement("span");
        text.textContent = full;
        // Truncated options still need a way to read the whole name.
        row.title = full;
        row.append(text);
        if (opt.value === select.value) {
          row.classList.add("cs-selected");
          row.setAttribute("aria-selected", "true");
          const tick = document.createElement("span");
          tick.className = "cs-tick";
          tick.textContent = "✓";
          row.append(tick);
        }
        row.addEventListener("mouseenter", () => setActive(i, false));
        row.addEventListener("click", (event) => {
          event.stopPropagation();
          choose(opt.value);
        });
        popup.append(row);
      });
      const index = keepActive ? Math.min(state.active, items.length - 1) : currentIndex();
      setActive(index < 0 ? 0 : index, true);
    }

    function setActive(index, scroll) {
      const rows = popup.querySelectorAll(".cs-option");
      if (!rows.length) return;
      state.active = Math.max(0, Math.min(index, rows.length - 1));
      rows.forEach((row, i) => row.classList.toggle("cs-active", i === state.active));
      if (scroll) rows[state.active].scrollIntoView({ block: "nearest" });
    }

    function open() {
      if (select.disabled || !popup.hidden) return;
      if (openInstance && openInstance !== api) openInstance.close();
      openInstance = api;
      paintPopup(false);
      popup.hidden = false;
      popup.classList.add("cs-visible");
      wrap.classList.add(OPEN_CLASS);
      trigger.setAttribute("aria-expanded", "true");
      // Measure after display so placement uses real dimensions.
      place(popup, trigger);
      window.addEventListener("scroll", reposition, true);
      window.addEventListener("resize", reposition, true);
    }

    function reposition() {
      if (!popup.hidden) place(popup, trigger);
    }

    function close() {
      if (popup.hidden) return;
      popup.hidden = true;
      popup.classList.remove("cs-visible");
      wrap.classList.remove(OPEN_CLASS);
      trigger.setAttribute("aria-expanded", "false");
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition, true);
      if (openInstance === api) openInstance = null;
    }

    function choose(value) {
      if (select.value !== value) {
        select.value = value;
        select.dispatchEvent(new Event("input", { bubbles: true }));
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      paintTrigger();
      close();
      if (!select.disabled) trigger.focus();
    }

    const api = { open, close, refresh: paintTrigger, wrap };

    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      popup.hidden ? open() : close();
    });

    trigger.addEventListener("keydown", (event) => {
      const items = options();
      const isOpen = !popup.hidden;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!isOpen) return open();
        setActive(state.active + (event.key === "ArrowDown" ? 1 : -1), true);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (!isOpen) return open();
        const opt = items[state.active];
        if (opt) choose(opt.value);
      } else if (event.key === "Escape") {
        if (isOpen) { event.preventDefault(); close(); }
      } else if (event.key === "Home" || event.key === "End") {
        if (!isOpen) return;
        event.preventDefault();
        setActive(event.key === "Home" ? 0 : items.length - 1, true);
      } else if (event.key === "Tab") {
        close();
      }
    });

    popup.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", close);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });

    // Mirror programmatic .value writes (app.js assigns it directly).
    const proto = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
    Object.defineProperty(select, "value", {
      configurable: true,
      get() { return proto.get.call(select); },
      set(next) {
        proto.set.call(select, next);
        paintTrigger();
        if (!popup.hidden) paintPopup(false);
      },
    });

    // Mirror option list rewrites (app.js sets innerHTML on the select).
    new MutationObserver(() => {
      paintTrigger();
      if (!popup.hidden) paintPopup(false);
    }).observe(select, { childList: true, subtree: true, characterData: true });

    select.addEventListener("change", paintTrigger);
    paintTrigger();

    api.close = close;
    select._customSelect = api;
  }

  function enhance(root) {
    (root || document).querySelectorAll("select").forEach(build);
  }

  window.CustomSelect = { enhance };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => enhance());
  } else {
    enhance();
  }
})();
