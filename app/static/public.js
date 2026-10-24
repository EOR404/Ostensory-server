(function () {
  "use strict";

  var messages = window.OSTENSORY_I18N || {};

  function t(key, values) {
    var text = messages[key] || key;
    if (values) {
      Object.keys(values).forEach(function (name) {
        text = text.replace(new RegExp("\\{" + name + "\\}", "g"), String(values[name]));
      });
    }
    return text;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        resolve();
      } catch (error) {
        reject(error);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  var imageShellSelector = ".asset-thumb, .album-cover, .album-compact-cover, .album-row-thumb, .detail-image-wrap, .carousel-slide, .about-avatar, .box-cover, .box-table-name";

  function markImage(img) {
    if (!img || img.dataset.loadingBound === "1") return;
    var shell = img.closest ? img.closest(imageShellSelector) : null;
    if (!shell) return;
    img.dataset.loadingBound = "1";
    shell.classList.add("media-shell");
    if (img.complete && img.naturalWidth) {
      shell.classList.add("is-loaded");
      return;
    }
    img.addEventListener("load", function () { shell.classList.add("is-loaded"); });
    img.addEventListener("error", function () {
      shell.classList.add("is-loaded", "is-broken");
    });
  }

  function initImageLoading(scope) {
    var root = scope || document;
    if (!root.querySelectorAll) return;
    Array.prototype.forEach.call(root.querySelectorAll("img"), markImage);
    if (root.tagName === "IMG") markImage(root);
  }

  function initThemeToggle() {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function currentTheme() {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function updateLabel() {
      var theme = currentTheme();
      button.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
    }

    function applyTheme(theme) {
      document.documentElement.setAttribute("data-theme", theme);
      try { window.localStorage.setItem("ostensory.theme", theme); } catch (error) { /* ignore */ }
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", theme === "light" ? "#f2eee5" : "#14120f");
      updateLabel();
    }

    updateLabel();
    button.addEventListener("click", function () {
      var next = currentTheme() === "light" ? "dark" : "light";
      var rect = button.getBoundingClientRect();
      document.documentElement.style.setProperty("--theme-x", (rect.left + rect.width / 2) + "px");
      document.documentElement.style.setProperty("--theme-y", (rect.top + rect.height / 2) + "px");
      if (!reduced && document.startViewTransition) {
        try {
          document.startViewTransition(function () { applyTheme(next); });
          return;
        } catch (error) { /* fall through */ }
      }
      applyTheme(next);
    });
  }

  function initBackToTop() {
    var button = document.querySelector("[data-back-to-top]");
    if (!button) return;
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var ticking = false;

    function update() {
      var visible = window.scrollY > 240;
      button.classList.toggle("is-visible", visible);
      button.setAttribute("aria-hidden", visible ? "false" : "true");
      button.tabIndex = visible ? 0 : -1;
      ticking = false;
    }

    function onScroll() {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }

    button.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: reduced ? "auto" : "smooth" });
    });
    window.addEventListener("scroll", onScroll, { passive: true });
    update();
  }

  var masonryFrame = 0;

  function masonryCards(grid) {
    var cards = Array.prototype.slice.call(grid.querySelectorAll(".asset-card"));
    var nextIndex = 0;
    cards.forEach(function (card) {
      var index = Number(card.getAttribute("data-masonry-index"));
      if (isFinite(index) && index >= nextIndex) nextIndex = index + 1;
    });
    cards.forEach(function (card) {
      if (!card.hasAttribute("data-masonry-index")) {
        card.setAttribute("data-masonry-index", String(nextIndex++));
      }
    });
    cards.sort(function (a, b) {
      return Number(a.getAttribute("data-masonry-index")) - Number(b.getAttribute("data-masonry-index"));
    });
    return cards;
  }

  function resetMasonry(grid) {
    if (!grid) return;
    var cards = masonryCards(grid);
    Array.prototype.forEach.call(grid.querySelectorAll(".masonry-column"), function (column) {
      column.parentNode.removeChild(column);
    });
    cards.forEach(function (card) { grid.appendChild(card); });
    grid.classList.remove("masonry-ready");
  }

  function layoutMasonryGrid(grid) {
    if (!grid || !grid.classList.contains("is-masonry")) return;
    var cards = masonryCards(grid);
    if (!cards.length) return;
    var width = grid.clientWidth;
    if (!width) return;
    var compact = window.innerWidth <= 560;
    var gap = compact ? 10 : 16;
    var minWidth = compact ? 150 : 260;
    var count = Math.max(1, Math.min(cards.length, Math.floor((width + gap) / (minWidth + gap))));
    Array.prototype.forEach.call(grid.querySelectorAll(".masonry-column"), function (column) {
      column.parentNode.removeChild(column);
    });
    var columns = [];
    var heights = [];
    for (var i = 0; i < count; i += 1) {
      var column = document.createElement("div");
      column.className = "masonry-column";
      columns.push(column);
      heights.push(0);
      grid.appendChild(column);
    }
    cards.forEach(function (card) {
      var shortest = 0;
      for (var i = 1; i < heights.length; i += 1) {
        if (heights[i] < heights[shortest]) shortest = i;
      }
      columns[shortest].appendChild(card);
      heights[shortest] += card.offsetHeight + gap;
    });
    grid.classList.add("masonry-ready");
  }

  function scheduleMasonry() {
    if (masonryFrame) return;
    masonryFrame = window.requestAnimationFrame(function () {
      masonryFrame = 0;
      Array.prototype.forEach.call(document.querySelectorAll(".asset-grid.is-masonry"), layoutMasonryGrid);
    });
  }

  function initMasonryLayouts() {
    Array.prototype.forEach.call(document.querySelectorAll(".asset-grid.is-masonry"), function (grid) {
      grid.addEventListener("load", function (event) {
        if (event.target && event.target.tagName === "IMG") scheduleMasonry();
      }, true);
      layoutMasonryGrid(grid);
    });
    window.addEventListener("resize", scheduleMasonry);
    window.addEventListener("load", function () { window.setTimeout(scheduleMasonry, 60); });
  }

  function initBrowseLayout() {
    var grid = document.querySelector("[data-browse-grid]");
    if (!grid) return;
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-browse-layout]"));
    var key = "ostensory.browseLayout.v2";
    var allowed = { grid: true, masonry: true };
    function apply(layout, persist) {
      if (!allowed[layout]) return;
      grid.classList.toggle("is-masonry", layout === "masonry");
      grid.setAttribute("data-layout", layout);
      if (layout === "masonry") window.requestAnimationFrame(function () { layoutMasonryGrid(grid); });
      else resetMasonry(grid);
      buttons.forEach(function (button) {
        var active = button.getAttribute("data-browse-layout") === layout;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      if (persist) {
        try { window.localStorage.setItem(key, layout); } catch (error) { /* ignore */ }
      }
    }
    var current = grid.getAttribute("data-layout") || "masonry";
    var explicit = "";
    try {
      explicit = new URLSearchParams(window.location.search).get("layout") || "";
    } catch (error) { explicit = ""; }
    if (allowed[explicit]) {
      current = explicit;
      try { window.localStorage.setItem(key, explicit); } catch (error) { /* ignore */ }
    } else {
      try {
        var saved = window.localStorage.getItem(key);
        if (saved && allowed[saved]) current = saved;
      } catch (error) { /* ignore */ }
    }
    apply(current, false);
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        apply(button.getAttribute("data-browse-layout"), true);
      });
    });
  }

  function initCarousel() {
    var root = document.querySelector("[data-carousel]");
    if (!root) return;
    var slides = Array.prototype.slice.call(root.querySelectorAll("[data-slide]"));
    var dots = Array.prototype.slice.call(root.querySelectorAll("[data-carousel-dot]"));
    if (!slides.length) return;
    slides[0].classList.add("is-active");
    if (slides.length < 2) return;
    var index = 0;
    var timer = null;
    var interval = Math.max(2000, Number(root.getAttribute("data-interval")) || 6000);
    function show(next) {
      index = (next + slides.length) % slides.length;
      slides.forEach(function (slide, i) { slide.classList.toggle("is-active", i === index); });
      dots.forEach(function (dot, i) { dot.classList.toggle("is-active", i === index); });
    }
    function restart() {
      if (timer) window.clearInterval(timer);
      timer = window.setInterval(function () { show(index + 1); }, interval);
    }
    slides.forEach(function (slide, i) {
      slide.classList.toggle("is-active", i === 0);
      slide.style.setProperty("--slide-index", String(i));
    });
    dots.forEach(function (dot, i) {
      dot.classList.toggle("is-active", i === 0);
      dot.addEventListener("click", function () { show(i); restart(); });
    });
    var prev = root.querySelector("[data-carousel-prev]");
    var next = root.querySelector("[data-carousel-next]");
    if (prev) prev.addEventListener("click", function () { show(index - 1); restart(); });
    if (next) next.addEventListener("click", function () { show(index + 1); restart(); });
    root.addEventListener("mouseenter", function () { if (timer) window.clearInterval(timer); });
    root.addEventListener("mouseleave", restart);
    restart();
  }

  function initInfiniteFeed() {
    var grid = document.querySelector("[data-infinite-feed]");
    if (!grid) return;
    var button = document.querySelector("[data-load-more]");
    var status = document.querySelector("[data-feed-status]");
    var footer = document.querySelector("[data-infinite-footer]");
    var page = Number(grid.getAttribute("data-page")) || 1;
    var pages = Number(grid.getAttribute("data-pages")) || 1;
    var perPage = Number(grid.getAttribute("data-per-page")) || 48;
    var url = grid.getAttribute("data-feed-url") || "/api/feed";
    var blur = grid.getAttribute("data-blur-nsfw") === "1";
    var loading = false;

    function badge(text, className) {
      var node = document.createElement("span");
      node.className = "badge" + (className ? " " + className : "");
      node.textContent = text;
      return node;
    }

    function makeCard(asset) {
      var article = document.createElement("article");
      article.className = "asset-card";
      if (asset.nsfw && blur) article.classList.add("is-nsfw");
      article.setAttribute("data-asset-id", String(asset.id));
      var link = document.createElement("a");
      link.className = "asset-thumb";
      link.href = asset.url;
      if (asset.locked) {
        link.classList.add("locked");
        var lock = document.createElement("span");
        lock.className = "lock-glyph";
        lock.textContent = "🔒";
        var label = document.createElement("span");
        label.className = "lock-label";
        label.textContent = t("common.requires_password");
        link.appendChild(lock);
        link.appendChild(label);
      } else {
        var img = document.createElement("img");
        img.src = asset.thumb;
        img.alt = asset.title;
        img.loading = "lazy";
        if (asset.width) img.width = asset.width;
        if (asset.height) img.height = asset.height;
        link.appendChild(img);
      }
      if (asset.nsfw) link.appendChild(badge("NSFW", "badge-nsfw"));
      if (asset.encrypted) link.appendChild(badge(t("common.encrypted"), "badge-lock"));
      article.appendChild(link);
      if (asset.nsfw && blur && !asset.locked) {
        var cover = document.createElement("button");
        cover.className = "nsfw-cover";
        cover.type = "button";
        cover.setAttribute("data-reveal-nsfw", "");
        var small = document.createElement("span");
        small.textContent = t("common.sensitive");
        var strong = document.createElement("strong");
        strong.textContent = t("common.click_to_show");
        cover.appendChild(small);
        cover.appendChild(strong);
        article.appendChild(cover);
      }
      var info = document.createElement("div");
      info.className = "asset-info";
      var title = document.createElement("a");
      title.className = "asset-title";
      title.href = asset.url;
      title.textContent = asset.title;
      info.appendChild(title);
      var meta = document.createElement("div");
      meta.className = "asset-meta";
      var size = document.createElement("span");
      size.textContent = (asset.width || "?") + "×" + (asset.height || "?");
      meta.appendChild(size);
      if (asset.lora_count) {
        var lora = document.createElement("span");
        lora.className = "meta-lora";
        lora.textContent = asset.lora_count + " LoRA";
        meta.appendChild(lora);
      }
      if (asset.kind === "comfyui") {
        var comfy = document.createElement("span");
        comfy.className = "dot-comfy";
        comfy.textContent = "ComfyUI";
        meta.appendChild(comfy);
      }
      info.appendChild(meta);
      var actions = document.createElement("div");
      actions.className = "asset-actions";
      var download = document.createElement("a");
      download.href = asset.download;
      download.title = t("common.download_original");
      download.textContent = t("common.download");
      var detail = document.createElement("a");
      detail.href = asset.url;
      detail.textContent = t("common.details");
      actions.appendChild(download);
      actions.appendChild(detail);
      info.appendChild(actions);
      article.appendChild(info);
      return article;
    }

    function setStatus(text, busy) {
      if (status) {
        status.textContent = text;
        status.dataset.loading = busy ? "1" : "0";
      }
    }

    function updateFooter() {
      if (button) button.hidden = page >= pages;
      if (footer && page >= pages) footer.classList.add("is-done");
    }

    function loadMore() {
      if (loading || page >= pages) return;
      loading = true;
      if (button) button.disabled = true;
      setStatus(t("browse.loading"), true);
      fetch(url + "?page=" + (page + 1) + "&per_page=" + perPage, {
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      }).then(function (payload) {
        (payload.assets || []).forEach(function (asset) {
          var card = makeCard(asset);
          grid.appendChild(card);
          initImageLoading(card);
        });
        scheduleMasonry();
        page = Number(payload.page) || page + 1;
        pages = Number(payload.pages) || pages;
        grid.setAttribute("data-page", String(page));
        updateFooter();
        setStatus(page >= pages ? t("browse.end") : t("browse.scroll_more"), false);
      }).catch(function () {
        setStatus(t("browse.load_failed"), false);
      }).finally(function () {
        loading = false;
        if (button) button.disabled = false;
      });
    }

    if (button) button.addEventListener("click", loadMore);
    updateFooter();
    if ("IntersectionObserver" in window) {
      var sentinel = document.createElement("div");
      sentinel.className = "feed-sentinel";
      sentinel.setAttribute("aria-hidden", "true");
      if (footer) footer.parentNode.insertBefore(sentinel, footer); else grid.after(sentinel);
      new IntersectionObserver(function (entries) {
        if (entries.some(function (entry) { return entry.isIntersecting; })) loadMore();
      }, { rootMargin: "500px 0px" }).observe(sentinel);
    }
  }

  function flashButton(button, text) {
    var old = button.textContent;
    button.textContent = text;
    window.setTimeout(function () { button.textContent = old; }, 1400);
  }

  document.addEventListener("click", function (event) {
    var reveal = event.target.closest("[data-reveal-nsfw]");
    if (reveal) {
      var card = reveal.closest(".asset-card, .image-detail, .unlock-card");
      if (card) card.classList.add("revealed");
      if (reveal.parentElement) reveal.parentElement.classList.add("revealed");
      return;
    }

    var copyButton = event.target.closest("[data-copy-target]");
    if (copyButton) {
      var target = document.querySelector(copyButton.getAttribute("data-copy-target"));
      if (!target) return;
      var targetText = "value" in target ? target.value : (target.textContent || "");
      copyText(targetText).then(function () {
        flashButton(copyButton, t("common.copied"));
      }).catch(function () {
        flashButton(copyButton, t("common.copy_failed"));
      });
      return;
    }

    var workflowButton = event.target.closest("[data-download-workflow]");
    if (workflowButton) {
      var payload = document.getElementById("workflow-json");
      if (!payload) return;
      try {
        var data = JSON.parse(payload.textContent || "{}");
        var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = "workflow.json";
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(function () { URL.revokeObjectURL(link.href); }, 2000);
        flashButton(workflowButton, t("image.downloaded"));
      } catch (error) {
        flashButton(workflowButton, t("image.invalid_json"));
      }
    }
  });

  function initFormScrollRestore() {
    var key = "ostensory.publicFormScroll";
    var pending = null;
    try {
      pending = JSON.parse(window.sessionStorage.getItem(key) || "null");
    } catch (error) { pending = null; }
    var restored = false;
    function restore() {
      if (restored || !pending) return;
      if (Date.now() - Number(pending.ts || 0) > 60000) return;
      if (window.location.pathname !== pending.path && window.location.pathname !== pending.target) return;
      restored = true;
      window.scrollTo(0, Math.max(0, Number(pending.y) || 0));
      try { window.sessionStorage.removeItem(key); } catch (error) { /* ignore */ }
    }
    if (pending) {
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(restore);
      });
      window.addEventListener("load", function () { window.setTimeout(restore, 30); }, { once: true });
    }
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || form.tagName !== "FORM") return;
      if (String(form.method || "get").toLowerCase() !== "post") return;
      if (form.getAttribute("target")) return;
      var y = window.pageYOffset || document.documentElement.scrollTop || 0;
      var target = "";
      try {
        target = new URL(form.getAttribute("action") || window.location.href, window.location.href).pathname;
      } catch (error) { target = ""; }
      pending = {
        path: window.location.pathname,
        target: target,
        y: Math.round(y),
        ts: Date.now()
      };
      restored = false;
      try {
        window.sessionStorage.setItem(key, JSON.stringify(pending));
      } catch (error) { /* ignore */ }
    }, true);
  }

  initFormScrollRestore();
  initCarousel();
  initMasonryLayouts();
  initBrowseLayout();
  initInfiniteFeed();
  initThemeToggle();
  initBackToTop();
  initImageLoading(document);
  window.addEventListener("load", function () { initImageLoading(document); });
})();