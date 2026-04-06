(() => {
  const INDEX_ATTR = "data-chrome-mcp-id";
  const STATE_KEY = "__chromeMcpIndex";
  const stateIndex = new Map();
  const generatedAt = Date.now().toString(36);
  const HEARTBEAT_MS = 15000;
  let sequence = 0;
  let heartbeatTimer = null;

  // ── Console capture ──────────────────────────────────────────────────────────
  const CONSOLE_BUFFER_SIZE = 200;
  const consoleBuffer = [];
  const CONSOLE_LEVELS = ["log", "info", "warn", "error", "debug"];
  for (const level of CONSOLE_LEVELS) {
    const original = console[level].bind(console);
    console[level] = (...args) => {
      original(...args);
      try {
        consoleBuffer.push({
          level,
          message: args.map((arg) => {
            try {
              return typeof arg === "object" && arg !== null ? JSON.stringify(arg) : String(arg);
            } catch (_e) {
              return String(arg);
            }
          }).join(" "),
          timestamp: Date.now(),
          url: location.href,
        });
        if (consoleBuffer.length > CONSOLE_BUFFER_SIZE) {
          consoleBuffer.shift();
        }
      } catch (_e) {
        // Never let the capture logic break the page.
      }
    };
  }

  function hashString(value) {
    let hash = 2166136261;
    for (let i = 0; i < value.length; i += 1) {
      hash ^= value.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (`0000000${(hash >>> 0).toString(36)}`).slice(-7);
  }

  function normalize(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function normalizeLower(value) {
    return normalize(value).toLowerCase();
  }

  function normalizeScope(value) {
    const scope = normalizeLower(value || "page");
    if (scope === "dialog" || scope === "dialog-interactive" || scope === "interactive" || scope === "page") {
      return scope;
    }
    return "page";
  }

  function uniqueJoin(values) {
    const seen = new Set();
    const output = [];
    for (const raw of values) {
      const value = normalize(raw);
      if (!value || seen.has(value)) {
        continue;
      }
      seen.add(value);
      output.push(value);
    }
    return output.join(" | ");
  }

  function isVisible(element) {
    if (!element) {
      return false;
    }
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  }

  function isVisibleWithinRoot(element, root) {
    if (!element || !root) {
      return false;
    }
    if (root === document) {
      return isVisible(element);
    }
    return root.contains(element) && isVisible(element);
  }

  function getPathSignature(element) {
    const parts = [];
    let current = element;
    while (current && current !== document.body && parts.length < 5) {
      const tag = current.tagName ? current.tagName.toLowerCase() : "node";
      const role = current.getAttribute?.("role") || "";
      const aria = current.getAttribute?.("aria-label") || "";
      const text = normalize(current.textContent).slice(0, 40);
      let index = 1;
      let sibling = current;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === current.tagName) {
          index += 1;
        }
      }
      parts.push([tag, role, aria, text, index].join(":"));
      current = current.parentElement;
    }
    return parts.reverse().join("|");
  }

  function getStableId(element) {
    const existing = element.getAttribute(INDEX_ATTR);
    if (existing) {
      return existing;
    }
    const signature = [
      location.href,
      element.tagName || "",
      element.id || "",
      element.name || "",
      element.type || "",
      element.getAttribute("role") || "",
      element.getAttribute("aria-label") || "",
      element.getAttribute("placeholder") || "",
      normalize(element.textContent).slice(0, 80),
      getPathSignature(element),
    ].join("|");
    const stableId = `mcp-${generatedAt}-${hashString(signature)}-${sequence += 1}`;
    element.setAttribute(INDEX_ATTR, stableId);
    return stableId;
  }

  function getVisibleDialog() {
    const selectors = [
      "[role='dialog'][aria-modal='true']",
      "dialog[open]",
      "[role='dialog']",
      ".jobs-easy-apply-modal",
      ".artdeco-modal[role='dialog']",
    ];
    for (const selector of selectors) {
      const dialog = Array.from(document.querySelectorAll(selector)).find((element) => isVisible(element));
      if (dialog) {
        return dialog;
      }
    }
    return null;
  }

  function getLabelText(element) {
    const candidates = [];
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) {
      if (element.labels && element.labels.length) {
        candidates.push(...Array.from(element.labels).map((label) => label.textContent || ""));
      }
    }
    const labelledBy = normalize(element.getAttribute?.("aria-labelledby") || "");
    if (labelledBy) {
      for (const id of labelledBy.split(/\s+/)) {
        const node = document.getElementById(id);
        if (node) {
          candidates.push(node.textContent || "");
        }
      }
    }
    if (element.id) {
      const externalLabel = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
      if (externalLabel) {
        candidates.push(externalLabel.textContent || "");
      }
    }
    const closestLabel = element.closest("label");
    if (closestLabel) {
      candidates.push(closestLabel.textContent || "");
    }

    // LinkedIn-specific label patterns
    const linkedInLabelSelectors = [
      "[data-test-form-builder-field-label]",
      ".fb-single-line-text__label",
      ".fb-dropdown__label",
      ".fb-text-selectable__label",
      ".fb-form-element-label",
    ];
    const container = element.closest(".fb-form-element, .jobs-easy-apply-form-element, [data-test-form-element]") || element.parentElement;
    if (container) {
      for (const sel of linkedInLabelSelectors) {
        const labelEl = container.querySelector(sel);
        if (labelEl) {
          candidates.push(labelEl.textContent || "");
          break;
        }
      }
      // Also check for the nearest preceding <span> or <label> with visible text
      const precedingLabel = container.querySelector("label, span.visually-hidden, legend");
      if (precedingLabel && precedingLabel !== element) {
        candidates.push(precedingLabel.textContent || "");
      }
    }

    return uniqueJoin(candidates).slice(0, 200);
  }

  function setNativeValue(element, value) {
    const proto = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    descriptor?.set?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  }

  function setSelectValue(select, value) {
    const exactValue = select.querySelector(`option[value="${CSS.escape(value)}"]`);
    const normalizedValue = normalizeLower(value);
    if (exactValue) {
      select.value = exactValue.value;
      select.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      select.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      return true;
    }

    const option = Array.from(select.options).find((item) => {
      const text = normalizeLower(item.textContent || "");
      return text === normalizedValue || text.includes(normalizedValue);
    });
    if (!option) {
      return false;
    }
    select.value = option.value;
    select.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    select.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return true;
  }

  function findDropdownOption(value) {
    const normalizedValue = normalizeLower(value);
    // Search for role="option" elements that are currently visible in the DOM
    const optionCandidates = Array.from(document.querySelectorAll(
      "[role='option'], [role='listbox'] li, [role='listbox'] [role='presentation']"
    ));
    const visible = optionCandidates.filter((el) => isVisible(el));
    if (!visible.length) {
      return null;
    }
    // Exact text match first
    const exact = visible.find((el) => normalizeLower(el.textContent || "") === normalizedValue);
    if (exact) return exact;
    // Partial text match
    const partial = visible.find((el) => normalizeLower(el.textContent || "").includes(normalizedValue));
    return partial || null;
  }

  function openAndSelectDropdownOption(triggerElement, value) {
    // Step 1: click the trigger to open the dropdown
    clickElement(triggerElement);

    // Step 2: try to find the option immediately (synchronous render or already open)
    const option = findDropdownOption(value);
    if (option) {
      clickElement(option);
      return { ok: true };
    }

    // Dropdown may not have rendered yet - signal caller to retry
    return { ok: false, error: "dropdown_not_open_yet" };
  }

  function collectDropdownOptions(element) {
    // Collect visible role="option" children relevant to a combobox element.
    // First check if the element owns a listbox via aria-controls/aria-owns.
    const ownedIds = [
      element.getAttribute("aria-controls"),
      element.getAttribute("aria-owns"),
    ].filter(Boolean);

    let optionEls = [];
    for (const id of ownedIds) {
      const container = document.getElementById(id);
      if (container) {
        optionEls = Array.from(container.querySelectorAll("[role='option']")).filter((el) => isVisible(el));
        if (optionEls.length) break;
      }
    }

    // Fallback: look for visible role="option" elements anywhere
    if (!optionEls.length) {
      optionEls = Array.from(document.querySelectorAll("[role='option']")).filter((el) => isVisible(el));
    }

    return optionEls.map((el) => normalize(el.textContent || "")).filter(Boolean);
  }

  function collectInteractiveElements(root = document) {
    stateIndex.clear();
    const nodes = [...root.querySelectorAll([
      "button",
      "input",
      "textarea",
      "select",
      "a[href]",
      "[role='button']",
      "[role='checkbox']",
      "[role='radio']",
      "[role='combobox']",
      "[role='listbox']",
      "[role='switch']",
      "[aria-haspopup='listbox']",
      "[contenteditable='true']",
    ].join(","))];

    // Deduplicate in case selectors overlap (e.g. a button with role="combobox")
    const seen = new Set();
    const uniqueNodes = nodes.filter((el) => {
      if (seen.has(el)) return false;
      seen.add(el);
      return true;
    });

    const interactiveElements = [];

    for (const element of uniqueNodes) {
      if (!isVisibleWithinRoot(element, root)) {
        continue;
      }
      const stableId = getStableId(element);
      stateIndex.set(stableId, element);

      const rect = element.getBoundingClientRect();
      const text = normalize(element.innerText || element.textContent || "");
      const role = element.getAttribute("role") || "";

      // Collect available dropdown options for combobox/listbox elements
      let options;
      if (role === "combobox" || role === "listbox" || element.getAttribute("aria-haspopup") === "listbox") {
        const opts = collectDropdownOptions(element);
        if (opts.length) {
          options = opts;
        }
      }

      // Collect native <select> options
      if (element instanceof HTMLSelectElement) {
        options = Array.from(element.options).map((o) => normalize(o.textContent || "")).filter(Boolean);
      }

      // Radio/checkbox group name for grouping
      let groupName;
      if (element.getAttribute("type") === "radio" || element.getAttribute("type") === "checkbox" || role === "radio" || role === "checkbox") {
        const nameAttr = element.getAttribute("name");
        if (nameAttr) {
          groupName = nameAttr;
        } else {
          // Try to find a containing fieldset/legend
          const fieldset = element.closest("fieldset");
          const legend = fieldset ? fieldset.querySelector("legend") : null;
          if (legend) {
            groupName = normalize(legend.textContent || "");
          }
        }
      }

      const descriptor = {
        id: stableId,
        tagName: element.tagName.toLowerCase(),
        type: element.getAttribute("type") || "",
        role,
        label: getLabelText(element),
        name: element.getAttribute("name") || "",
        ariaLabel: element.getAttribute("aria-label") || "",
        placeholder: element.getAttribute("placeholder") || "",
        text: text.slice(0, 160),
        value: "value" in element ? String(element.value || "") : "",
        checked: "checked" in element ? Boolean(element.checked) : false,
        required: Boolean(element.required) || element.getAttribute("aria-required") === "true",
        disabled: Boolean(element.disabled),
        href: element.getAttribute("href") || "",
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      };
      if (options !== undefined) descriptor.options = options;
      if (groupName !== undefined) descriptor.groupName = groupName;
      interactiveElements.push(descriptor);
    }

    interactiveElements.sort((left, right) => {
      if (left.y !== right.y) return left.y - right.y;
      return left.x - right.x;
    });

    return interactiveElements;
  }

  function countVisibleInteractiveElements(root = document) {
    let count = 0;
    const seen = new Set();
    for (const element of root.querySelectorAll([
      "button",
      "input",
      "textarea",
      "select",
      "a[href]",
      "[role='button']",
      "[role='checkbox']",
      "[role='radio']",
      "[role='combobox']",
      "[role='listbox']",
      "[role='switch']",
      "[aria-haspopup='listbox']",
      "[contenteditable='true']",
    ].join(","))) {
      if (!seen.has(element) && isVisibleWithinRoot(element, root)) {
        seen.add(element);
        count += 1;
      }
    }
    return count;
  }

  function collectPageState(options = {}) {
    const scope = normalizeScope(options.scope);
    const dialog = getVisibleDialog();
    const textLimit = Number.isFinite(options.textLimit) ? Math.max(250, Math.min(Number(options.textLimit), 12000)) : 4000;
    const interactiveLimit = Number.isFinite(options.interactiveLimit) ? Math.max(1, Math.min(Number(options.interactiveLimit), 250)) : 250;
    const root = scope === "dialog" || scope === "dialog-interactive"
      ? (dialog || document)
      : document;
    const interactiveElements = collectInteractiveElements(root).slice(0, interactiveLimit);
    const textSource = root === document ? document.body : root;
    const bodyText = normalize(textSource?.innerText || document.body?.innerText || "");
    const dialogInteractiveCount = dialog ? countVisibleInteractiveElements(dialog) : 0;
    return {
      scope,
      url: location.href,
      title: document.title || "",
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
      },
      visibleTextExcerpt: bodyText.slice(0, textLimit),
      activeDialog: dialog ? {
        tagName: dialog.tagName.toLowerCase(),
        role: dialog.getAttribute("role") || "",
        ariaLabel: dialog.getAttribute("aria-label") || "",
        text: normalize(dialog.innerText || dialog.textContent || "").slice(0, textLimit),
        interactiveCount: dialogInteractiveCount,
      } : null,
      interactiveElements,
    };
  }

  function resolveElement(targetId) {
    if (!targetId) {
      return null;
    }
    const cached = stateIndex.get(targetId);
    if (cached && document.contains(cached)) {
      return cached;
    }
    const fallback = document.querySelector(`[${INDEX_ATTR}="${CSS.escape(targetId)}"]`);
    if (fallback) {
      stateIndex.set(targetId, fallback);
    }
    return fallback || null;
  }

  function collectCandidates(root = document) {
    stateIndex.clear();
    return collectInteractiveElements(root);
  }

  function scoreElementMatch(query, element, exact = false) {
    const normalizedQuery = normalizeLower(query);
    if (!normalizedQuery) {
      return 0;
    }

    const fields = [
      element.label,
      element.ariaLabel,
      element.text,
      element.placeholder,
      element.name,
      element.value,
      element.role,
      element.tagName,
      element.type,
      element.href,
    ].map(normalizeLower).filter(Boolean);

    if (!fields.length) {
      return 0;
    }

    if (exact) {
      return fields.some((field) => field === normalizedQuery) ? 1000 : 0;
    }

    if (fields.some((field) => field === normalizedQuery)) {
      return 220;
    }
    if (fields.some((field) => field.includes(normalizedQuery))) {
      return 140;
    }

    let score = 0;
    for (const term of normalizedQuery.split(/\s+/)) {
      if (!term) continue;
      if (fields.some((field) => field === term)) {
        score += 20;
      } else if (fields.some((field) => field.includes(term))) {
        score += 10;
      }
    }

    return score;
  }

  function resolveElementByQuery(query, options = {}) {
    const scope = normalizeScope(options.scope);
    const exact = Boolean(options.exact);
    const root = scope === "dialog" || scope === "dialog-interactive" ? (getVisibleDialog() || document) : document;
    const candidates = collectCandidates(root);
    let best = null;
    let bestScore = 0;
    for (const candidate of candidates) {
      const score = scoreElementMatch(query, candidate, exact);
      if (score <= 0) {
        continue;
      }
      if (!best || score > bestScore || (score === bestScore && (candidate.y < best.y || (candidate.y === best.y && candidate.x < best.x)))) {
        best = candidate;
        bestScore = score;
      }
    }
    return best ? resolveElement(best.id) || null : null;
  }

  function clickElement(element) {
    element.scrollIntoView({ block: "center", behavior: "instant" });
    if (typeof element.focus === "function") {
      try {
        element.focus({ preventScroll: true });
      } catch (_error) {
        element.focus();
      }
    }
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const eventTarget = document.elementFromPoint(x, y) || element;

    const pointerEvents = [
      ["pointerover", PointerEvent],
      ["mouseover", MouseEvent],
      ["pointerenter", PointerEvent],
      ["mouseenter", MouseEvent],
      ["pointerdown", PointerEvent],
      ["mousedown", MouseEvent],
      ["pointerup", PointerEvent],
      ["mouseup", MouseEvent],
      ["click", MouseEvent],
    ];

    const dispatchClickSequence = (target) => {
      for (const [eventName, EventType] of pointerEvents) {
        try {
          target.dispatchEvent(new EventType(eventName, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: x,
            clientY: y,
            button: 0,
            buttons: 1,
            pointerId: 1,
            pointerType: "mouse",
            isPrimary: true,
            view: window,
          }));
        } catch (error) {
          // Ignore bad synthetic-event shapes and keep trying lower-fidelity actions.
        }
      }
    };

    dispatchClickSequence(element);
    if (eventTarget !== element) {
      dispatchClickSequence(eventTarget);
    }

    if (element instanceof HTMLAnchorElement && element.href) {
      window.location.assign(element.href);
      return true;
    }

    if (typeof element.click === "function") {
      element.click();
      return true;
    }

    if (eventTarget !== element && typeof eventTarget.click === "function") {
      eventTarget.click();
      return true;
    }

    return false;
  }

  function performAction(action) {
    if (!action || typeof action !== "object") {
      return { ok: false, error: "missing_action" };
    }

    if (action.kind === "ping") {
      return { ok: true, kind: "pong" };
    }

    if (action.kind === "navigate") {
      const url = normalize(action.url || "");
      if (!url) {
        return { ok: false, error: "missing_url" };
      }
      window.location.assign(url);
      return { ok: true, kind: "navigate", url };
    }

    if (action.kind === "scroll") {
      const target = resolveElement(action.targetId);
      if (target) {
        if (typeof action.deltaY === "number") {
          target.scrollTop += Number(action.deltaY || 0);
          target.scrollLeft += Number(action.deltaX || 0);
        } else {
          target.scrollIntoView({ block: action.block || "center", behavior: "instant" });
        }
        return { ok: true, kind: "scroll", targetId: action.targetId || null };
      }
      window.scrollBy({
        top: Number(action.deltaY || 0),
        left: Number(action.deltaX || 0),
        behavior: "instant",
      });
      return { ok: true, kind: "scroll", targetId: null };
    }

    const element = resolveElement(action.targetId) || (action.query ? resolveElementByQuery(action.query, action) : null);
    if (!element) {
      return { ok: false, error: "target_not_found", targetId: action.targetId || null, query: action.query || null };
    }

    if (action.kind === "click") {
      return { ok: clickElement(element), kind: "click", targetId: action.targetId };
    }

    if (action.kind === "type") {
      const text = String(action.text || "");
      if (!text) {
        return { ok: false, error: "missing_text" };
      }
      element.focus();
      for (const char of text) {
        const keyOpts = { key: char, bubbles: true, cancelable: true, composed: true };
        element.dispatchEvent(new KeyboardEvent("keydown", keyOpts));
        element.dispatchEvent(new KeyboardEvent("keypress", keyOpts));
        if (element.isContentEditable) {
          document.execCommand("insertText", false, char);
        } else if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
          const start = element.selectionStart != null ? element.selectionStart : element.value.length;
          const end = element.selectionEnd != null ? element.selectionEnd : element.value.length;
          element.value = element.value.slice(0, start) + char + element.value.slice(end);
          element.selectionStart = element.selectionEnd = start + char.length;
          element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        }
        element.dispatchEvent(new KeyboardEvent("keyup", keyOpts));
      }
      element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      element.blur();
      return { ok: true, kind: "type", length: text.length };
    }

    if (action.kind === "upload_file") {
      if (!(element instanceof HTMLInputElement) || element.type !== "file") {
        return { ok: false, error: "target_not_a_file_input" };
      }
      const fileName = String(action.fileName || "file.txt");
      const mimeType = String(action.mimeType || "application/octet-stream");
      const content = action.content || "";
      try {
        let fileData;
        if (action.encoding === "base64") {
          const binary = atob(content);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i += 1) {
            bytes[i] = binary.charCodeAt(i);
          }
          fileData = bytes;
        } else {
          fileData = new TextEncoder().encode(content);
        }
        const file = new File([fileData], fileName, { type: mimeType });
        const dt = new DataTransfer();
        dt.items.add(file);
        element.files = dt.files;
        element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        return { ok: true, kind: "upload_file", fileName };
      } catch (err) {
        return { ok: false, error: String(err) };
      }
    }

    if (action.kind === "setValue") {
      const value = action.value == null ? "" : String(action.value);
      if (element instanceof HTMLSelectElement) {
        const ok = setSelectValue(element, value);
        return { ok, kind: "setValue", targetId: action.targetId, query: action.query || null };
      }
      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        const role = element.getAttribute("role") || "";
        const container = element.closest("[role='combobox'], [role='listbox'], [aria-haspopup='listbox']");
        if (container || role === "combobox" || role === "listbox") {
          // For combobox inputs: set the value then try to select from dropdown
          element.focus();
          setNativeValue(element, value);
          // Attempt to open and select matching option
          const result = openAndSelectDropdownOption(container || element, value);
          if (result.ok) {
            return { ok: true, kind: "setValue", targetId: action.targetId, query: action.query || null };
          }
          // No matching option found in dropdown, but the text value was still set
          element.blur();
          return { ok: true, kind: "setValue", targetId: action.targetId, query: action.query || null, note: result.error };
        }
        element.focus();
        setNativeValue(element, value);
        element.blur();
        return { ok: true, kind: "setValue", targetId: action.targetId, query: action.query || null };
      }
      if (element.isContentEditable) {
        element.textContent = value;
        element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        return { ok: true, kind: "setValue", targetId: action.targetId, query: action.query || null };
      }
      // Handle combobox divs/buttons (non-input elements with role="combobox")
      const role = element.getAttribute("role") || "";
      const hasPopup = element.getAttribute("aria-haspopup");
      if (role === "combobox" || role === "listbox" || hasPopup === "listbox") {
        const result = openAndSelectDropdownOption(element, value);
        return { ...result, kind: "setValue", targetId: action.targetId, query: action.query || null };
      }
      return { ok: false, error: "unsupported_target", targetId: action.targetId, query: action.query || null };
    }

    if (action.kind === "selectCustomOption") {
      const value = String(action.value || "");
      if (!value) {
        return { ok: false, error: "missing_value" };
      }
      const result = openAndSelectDropdownOption(element, value);
      return { ...result, kind: "selectCustomOption", targetId: action.targetId, query: action.query || null };
    }

    if (action.kind === "selectOption") {
      const value = String(action.value || "");
      if (!value) {
        return { ok: false, error: "missing_value" };
      }
      // Native <select>
      if (element instanceof HTMLSelectElement) {
        const ok = setSelectValue(element, value);
        if (!ok) {
          return { ok: false, error: "option_not_found", kind: "selectOption" };
        }
        return { ok: true, kind: "selectOption", targetId: action.targetId, query: action.query || null };
      }
      // Custom combobox / listbox
      const result = openAndSelectDropdownOption(element, value);
      return { ...result, kind: "selectOption", targetId: action.targetId, query: action.query || null };
    }

    if (action.kind === "clearAndType") {
      const text = String(action.text || "");
      if (!text) {
        return { ok: false, error: "missing_text" };
      }
      // Focus and select-all existing content
      clickElement(element);
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "a", ctrlKey: true, bubbles: true, cancelable: true, composed: true }));
      element.dispatchEvent(new KeyboardEvent("keyup", { key: "a", ctrlKey: true, bubbles: true, cancelable: true, composed: true }));

      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        element.select();
        // Replace selection by setting value directly
        setNativeValue(element, "");
      } else if (element.isContentEditable) {
        document.execCommand("selectAll", false);
        document.execCommand("delete", false);
      }

      // Now type the new text character by character
      for (const char of text) {
        const keyOpts = { key: char, bubbles: true, cancelable: true, composed: true };
        element.dispatchEvent(new KeyboardEvent("keydown", keyOpts));
        element.dispatchEvent(new KeyboardEvent("keypress", keyOpts));
        if (element.isContentEditable) {
          document.execCommand("insertText", false, char);
        } else if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
          const start = element.selectionStart != null ? element.selectionStart : element.value.length;
          const end = element.selectionEnd != null ? element.selectionEnd : element.value.length;
          element.value = element.value.slice(0, start) + char + element.value.slice(end);
          element.selectionStart = element.selectionEnd = start + char.length;
          element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        }
        element.dispatchEvent(new KeyboardEvent("keyup", keyOpts));
      }
      element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      element.blur();
      return { ok: true, kind: "clearAndType", length: text.length };
    }

    return { ok: false, error: "unsupported_kind", kind: action.kind };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "CHROME_MCP_COLLECT_STATE") {
      sendResponse({ ok: true, state: collectPageState(message.options || {}) });
      return false;
    }

    if (message?.type === "CHROME_MCP_PERFORM_ACTION") {
      sendResponse(performAction(message.action));
      return false;
    }

    if (message?.type === "CHROME_MCP_PING_PAGE") {
      sendResponse({ ok: true, pong: true, url: location.href });
      return false;
    }

    if (message?.type === "CHROME_MCP_GET_CONSOLE") {
      const limit = Math.max(1, Math.min(Number(message.limit) || 50, CONSOLE_BUFFER_SIZE));
      const level = message.level || null;
      const logs = level
        ? consoleBuffer.filter((entry) => entry.level === level).slice(-limit)
        : consoleBuffer.slice(-limit);
      sendResponse({ ok: true, logs, total: consoleBuffer.length });
      return false;
    }

    if (message?.type === "CHROME_MCP_CLEAR_CONSOLE") {
      consoleBuffer.length = 0;
      sendResponse({ ok: true });
      return false;
    }

    if (message?.type === "CHROME_MCP_GET_PAGE_TEXT") {
      const el = message.selector ? document.querySelector(message.selector) : document.body;
      const text = el ? (el.innerText || el.textContent || "") : "";
      sendResponse({ ok: true, text: text.slice(0, Number(message.limit) || 20000), url: location.href, title: document.title });
      return false;
    }

    return false;
  });

  function startHeartbeat() {
    if (heartbeatTimer !== null) {
      return;
    }
    heartbeatTimer = window.setInterval(() => {
      chrome.runtime.sendMessage({
        type: "CHROME_MCP_PAGE_HEARTBEAT",
        url: location.href,
        title: document.title || "",
      }).catch(() => {});
    }, HEARTBEAT_MS);
  }

  chrome.runtime.sendMessage({
    type: "CHROME_MCP_PAGE_READY",
    url: location.href,
    title: document.title || "",
  }).catch(() => {});

  startHeartbeat();
})();
