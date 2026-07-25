(() => {
  "use strict";

  const SQL_KEYWORDS = new Set([
    "and", "as", "asc", "by", "case", "desc", "distinct", "else", "end", "from", "full", "group",
    "having", "in", "inner", "is", "join", "left", "like", "limit", "not", "null", "on", "or", "order",
    "outer", "right", "select", "then", "union", "when", "where", "with"
  ]);
  const PYTHON_KEYWORDS = new Set([
    "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else",
    "except", "false", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "none",
    "nonlocal", "not", "or", "pass", "raise", "return", "true", "try", "while", "with", "yield"
  ]);
  const PYTHON_BUILTINS = new Set(["dict", "enumerate", "float", "int", "len", "list", "max", "min", "range", "set", "str", "sum", "tuple", "zip"]);

  function escapeHtml(value) {
    return value.replace(/[&<>]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[character]));
  }

  function tokenClass(token, language) {
    const lowered = token.toLowerCase();
    if ((language === "sql" && token.startsWith("--")) || (language === "python" && token.startsWith("#"))) return "comment";
    if (/^[rubf]*(?:'''|\"\"\"|'|\")/i.test(token)) return "string";
    if (/^\$[A-Za-z_]/.test(token)) return "parameter";
    if (/^\d/.test(token)) return "number";
    if (language === "python" && PYTHON_BUILTINS.has(lowered)) return "builtin";
    if ((language === "sql" ? SQL_KEYWORDS : PYTHON_KEYWORDS).has(lowered)) return "keyword";
    return "plain";
  }

  function highlight(source, language) {
    const pattern = language === "python"
      ? /(#.*$|(?:[rubf]*)(?:'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]\w*\b)/gim
      : /(--.*$|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\$[A-Za-z_]\w*|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]\w*\b)/gim;
    let output = "";
    let cursor = 0;
    for (const match of source.matchAll(pattern)) {
      output += escapeHtml(source.slice(cursor, match.index));
      const className = tokenClass(match[0], language);
      const escaped = escapeHtml(match[0]);
      output += className === "plain" ? escaped : `<span class="token-${className}">${escaped}</span>`;
      cursor = match.index + match[0].length;
    }
    return `${output}${escapeHtml(source.slice(cursor))}\n`;
  }

  function formatSql(source) {
    return source.replace(/\t/g, "  ").replace(/[ \t]+$/gm, "").trimEnd() + "\n";
  }

  function formatPython(source) {
    return source.replace(/\t/g, "    ").replace(/[ \t]+$/gm, "").trimEnd() + "\n";
  }

  function mountEditor(textarea) {
    if (textarea.dataset.editorMounted === "true") return;
    textarea.dataset.editorMounted = "true";
    const shell = document.createElement("div");
    shell.className = "code-editor-shell";
    const toolbar = document.createElement("div");
    toolbar.className = "code-editor-toolbar";
    const languageLabel = document.createElement("span");
    const formatButton = document.createElement("button");
    formatButton.type = "button";
    formatButton.className = "small secondary";
    formatButton.textContent = "Format";
    toolbar.append(languageLabel, formatButton);
    const surface = document.createElement("div");
    surface.className = "code-editor-surface";
    const highlightLayer = document.createElement("pre");
    highlightLayer.setAttribute("aria-hidden", "true");
    textarea.parentNode.insertBefore(shell, textarea);
    shell.append(toolbar, surface);
    surface.append(highlightLayer, textarea);
    textarea.classList.add("code-editor-input");

    const form = textarea.closest("form");
    const languageSelect = form ? form.querySelector('select[name="language"]') : null;
    const currentLanguage = () => (languageSelect ? languageSelect.value : textarea.dataset.language || "sql");
    const render = () => {
      const language = currentLanguage();
      languageLabel.textContent = language.toUpperCase();
      highlightLayer.innerHTML = highlight(textarea.value, language);
    };
    const syncScroll = () => {
      highlightLayer.scrollTop = textarea.scrollTop;
      highlightLayer.scrollLeft = textarea.scrollLeft;
    };
    const syncHeight = () => {
      surface.style.height = `${Math.max(textarea.offsetHeight, 220)}px`;
    };

    textarea.addEventListener("input", render);
    textarea.addEventListener("scroll", syncScroll);
    textarea.addEventListener("keydown", (event) => {
      if (event.key === "Tab") {
        event.preventDefault();
        const start = textarea.selectionStart;
        textarea.setRangeText("  ", start, textarea.selectionEnd, "end");
        render();
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s" && form) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    formatButton.addEventListener("click", () => {
      textarea.value = currentLanguage() === "python" ? formatPython(textarea.value) : formatSql(textarea.value);
      render();
      textarea.focus();
    });
    if (languageSelect) languageSelect.addEventListener("change", render);
    if (window.ResizeObserver) new ResizeObserver(syncHeight).observe(textarea);
    syncHeight();
    render();
  }

  const editors = document.querySelectorAll("textarea[data-code-editor]");
  if (window.IntersectionObserver) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        mountEditor(entry.target);
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "320px 0px" });
    editors.forEach((editor) => observer.observe(editor));
  } else {
    editors.forEach(mountEditor);
  }
})();
