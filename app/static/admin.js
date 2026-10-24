(function () {
  "use strict";

  function initFormScrollRestore() {
    var key = "ostensory.adminFormScroll";
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

  function initAdminTheme() {
    var button = document.querySelector("[data-admin-theme]");
    if (!button) return;
    function current() {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }
    function update() {
      button.setAttribute("aria-pressed", current() === "light" ? "true" : "false");
    }
    function apply(theme) {
      document.documentElement.setAttribute("data-theme", theme);
      try { window.localStorage.setItem("ostensory.theme", theme); } catch (error) { /* ignore */ }
      update();
    }
    update();
    button.addEventListener("click", function () {
      apply(current() === "light" ? "dark" : "light");
    });
  }

  var busyOverlay = null;
  var busyText = null;

  function showBusy(message) {
    if (!busyOverlay) {
      busyOverlay = document.createElement("div");
      busyOverlay.className = "loading-overlay";
      busyOverlay.setAttribute("role", "status");
      busyOverlay.setAttribute("aria-live", "polite");
      var card = document.createElement("div");
      card.className = "loading-card";
      var spinner = document.createElement("span");
      spinner.className = "spinner";
      busyText = document.createElement("p");
      card.appendChild(spinner);
      card.appendChild(busyText);
      busyOverlay.appendChild(card);
      document.body.appendChild(busyOverlay);
    }
    busyText.textContent = message || "正在处理…";
    busyOverlay.hidden = false;
    document.body.classList.add("is-busy");
  }

  function hideBusy() {
    if (busyOverlay) busyOverlay.hidden = true;
    document.body.classList.remove("is-busy");
  }

  window.addEventListener("pageshow", hideBusy);

  var adminImageShellSelector = ".thumb-link, .recent-strip a, .edit-preview, .branding-preview, .mini-grid a";

  function markAdminImage(img) {
    if (!img || img.dataset.loadingBound === "1") return;
    var shell = img.closest ? img.closest(adminImageShellSelector) : null;
    if (!shell) return;
    img.dataset.loadingBound = "1";
    shell.classList.add("media-shell");
    function done() { shell.classList.add("is-loaded"); }
    if (img.complete && img.naturalWidth) done();
    else {
      img.addEventListener("load", done);
      img.addEventListener("error", done);
    }
  }

  function initAdminImages(scope) {
    var root = scope || document;
    if (!root.querySelectorAll) return;
    Array.prototype.forEach.call(root.querySelectorAll("img"), markAdminImage);
  }

  var uploadForm = document.querySelector("[data-upload-form]");
  var fileInput = uploadForm ? uploadForm.querySelector("[data-file-input]") : null;
  var count = document.querySelector("[data-file-count]");
  var uploadSelected = document.querySelector("[data-upload-selected]");
  var dropzone = document.querySelector("[data-dropzone]");
  var panel = document.querySelector("[data-candidate-panel]");
  var candidateGrid = panel ? panel.querySelector("[data-candidate-grid]") : null;
  var candidateSummary = panel ? panel.querySelector("[data-candidate-summary]") : null;
  var candidateTotal = panel ? panel.querySelector("[data-candidate-total]") : null;
  var confirmButton = document.querySelector("[data-candidate-confirm]");
  var candidates = [];

  function humanSize(bytes) {
    if (!bytes) return "0 B";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function fileKey(file) {
    return [file.name, file.size, file.lastModified || 0].join("|");
  }

  function selectedFiles() {
    return candidates
      .filter(function (entry) { return entry.checked; })
      .map(function (entry) { return entry.file; });
  }

  function updateSummary() {
    var checked = selectedFiles().length;
    if (candidateSummary) {
      candidateSummary.textContent = candidates.length
        ? "共 " + candidates.length + " 张，已选择 " + checked + " 张；可以继续添加。"
        : "还没有选择文件。可以分多次选择，候选会一直保留。";
    }
    if (candidateTotal) {
      candidateTotal.textContent = "已选 " + checked + " / " + candidates.length;
    }
    if (count) {
      count.textContent = candidates.length
        ? "已选择 " + checked + " / " + candidates.length + " 个文件"
        : "尚未选择文件";
    }
    if (uploadSelected) {
      uploadSelected.textContent = checked
        ? "已选 " + checked + " 张，共 " + candidates.length + " 张候选"
        : "尚未勾选上传文件";
    }
    if (confirmButton) confirmButton.disabled = checked === 0;
  }

  function revokeEntry(entry) {
    if (entry && entry.url) URL.revokeObjectURL(entry.url);
  }

  function renderCandidates() {
    if (!candidateGrid) return;
    candidateGrid.textContent = "";
    if (!candidates.length) {
      var empty = document.createElement("p");
      empty.className = "candidate-empty";
      empty.textContent = "还没有选择文件。拖入图片或点击下方选择，可以连续添加多批。";
      candidateGrid.appendChild(empty);
    }
    candidates.forEach(function (entry, index) {
      var card = document.createElement("div");
      card.className = "candidate-card" + (entry.checked ? "" : " is-unchecked");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = entry.checked;
      box.setAttribute("data-candidate-index", String(index));
      box.setAttribute("aria-label", "上传 " + entry.file.name);
      box.addEventListener("change", function () {
        entry.checked = box.checked;
        card.classList.toggle("is-unchecked", !entry.checked);
        updateSummary();
      });
      var image = document.createElement("img");
      image.alt = "";
      image.addEventListener("load", function () { image.classList.add("is-loaded"); });
      image.addEventListener("error", function () { image.classList.add("is-loaded"); });
      image.src = entry.url;
      var body = document.createElement("span");
      body.className = "candidate-body";
      var name = document.createElement("span");
      name.className = "candidate-name";
      name.textContent = entry.file.name;
      var size = document.createElement("span");
      size.className = "candidate-size";
      size.textContent = humanSize(entry.file.size);
      body.appendChild(name);
      body.appendChild(size);
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "candidate-remove";
      remove.setAttribute("aria-label", "移除 " + entry.file.name);
      remove.textContent = "×";
      remove.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        revokeEntry(entry);
        candidates.splice(index, 1);
        renderCandidates();
      });
      card.appendChild(box);
      card.appendChild(image);
      card.appendChild(body);
      card.appendChild(remove);
      candidateGrid.appendChild(card);
    });
    updateSummary();
  }

  function addFiles(list) {
    if (!list) return;
    Array.prototype.forEach.call(list, function (file) {
      var key = fileKey(file);
      if (candidates.some(function (entry) { return entry.key === key; })) return;
      candidates.push({
        key: key,
        file: file,
        checked: true,
        url: URL.createObjectURL(file)
      });
    });
    // Allow selecting the same file again later; candidates are the source
    // of truth and are rebuilt into the input only when confirming.
    if (fileInput) fileInput.value = "";
    renderCandidates();
  }

  function syncInputToSelection() {
    var chosen = selectedFiles();
    if (!chosen.length) return false;
    if (typeof DataTransfer !== "function") return true;
    try {
      var transfer = new DataTransfer();
      chosen.forEach(function (file) { transfer.items.add(file); });
      fileInput.files = transfer.files;
      return true;
    } catch (error) {
      return true;
    }
  }

  function submitUpload() {
    if (!syncInputToSelection()) {
      window.alert("请至少勾选一张要上传的图片。");
      return;
    }
    if (uploadForm) {
      if (typeof uploadForm.requestSubmit === "function") uploadForm.requestSubmit();
      else uploadForm.submit();
    }
  }

  var conflictDialog = document.querySelector("[data-upload-conflict-dialog]");
  var conflictList = conflictDialog ? conflictDialog.querySelector("[data-upload-conflict-list]") : null;
  var conflictSummary = conflictDialog ? conflictDialog.querySelector("[data-upload-conflict-summary]") : null;
  var pendingConflicts = [];

  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      try {
        if (!dialog.open) dialog.showModal();
        return;
      } catch (error) { /* fall through for older browsers */ }
    }
    dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  var storageFullDialog = document.querySelector("[data-storage-full-dialog]");
  var storageFullSummary = storageFullDialog
    ? storageFullDialog.querySelector("[data-storage-full-summary]")
    : null;
  var pendingUploadBytes = 0;

  function showStorageFull(data, needed) {
    pendingUploadBytes = Math.max(0, Number(needed) || 0);
    var available = data && data.available_bytes === null
      ? "不限"
      : "约 " + humanSize((data && data.available_bytes) || 0);
    var trash = (data && data.trash) || {};
    if (storageFullSummary) {
      storageFullSummary.textContent =
        "本次需要约 " + humanSize(pendingUploadBytes) +
        "，当前可用 " + available +
        "；回收站有 " + (Number(trash.count) || 0) + " 张，可释放约 " +
        humanSize(Number(trash.reclaimable_bytes) || 0) + "。";
    }
    if (!storageFullDialog) {
      window.alert("存储空间不足。请先到回收站永久删除图片，或扩容存储。");
      return;
    }
    openDialog(storageFullDialog);
  }

  function initStorageFullDialog() {
    if (!storageFullDialog) return;
    var cancelButtons = storageFullDialog.querySelectorAll("[data-storage-full-cancel]");
    Array.prototype.forEach.call(cancelButtons, function (button) {
      button.addEventListener("click", function () { closeDialog(storageFullDialog); });
    });
    storageFullDialog.addEventListener("click", function (event) {
      if (event.target === storageFullDialog) closeDialog(storageFullDialog);
    });
    var auto = storageFullDialog.querySelector("[data-storage-full-auto]");
    var manual = storageFullDialog.querySelector("[data-storage-full-manual]");
    if (manual) {
      manual.addEventListener("click", function () { closeDialog(storageFullDialog); });
    }
    if (!auto || !uploadForm || typeof fetch !== "function") return;
    auto.addEventListener("click", function () {
      var reclaimUrl = uploadForm.getAttribute("data-reclaim-url");
      if (!reclaimUrl) return;
      if (!window.confirm("将按最久未处理的顺序永久删除回收站图片，直到能放下这批上传。删除后无法恢复，继续吗？")) {
        return;
      }
      var original = auto.textContent;
      auto.disabled = true;
      auto.textContent = "正在清理…";
      var body = new URLSearchParams();
      body.set("csrf", uploadForm.getAttribute("data-csrf") || "");
      body.set("need", String(pendingUploadBytes));
      fetch(reclaimUrl, {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
        },
        credentials: "same-origin",
        body: body.toString()
      })
        .then(function (response) {
          if (!response.ok) throw new Error("reclaim failed");
          return response.json();
        })
        .then(function (result) {
          if (result && result.ok) {
            closeDialog(storageFullDialog);
            requestUploadWithNameCheck();
            return;
          }
          if (storageFullSummary) {
            storageFullSummary.textContent = (result && result.message) ||
              "回收站已清理，但仍没有足够的存储空间。";
          }
        })
        .catch(function () {
          if (storageFullSummary) {
            storageFullSummary.textContent = "自动清理失败，请到回收站手动删除图片。";
          }
        })
        .finally(function () {
          auto.disabled = false;
          auto.textContent = original;
        });
    });
  }

  initStorageFullDialog();

  function normalizedName(value) {
    return String(value || "").toLowerCase();
  }

  function uploadNameUrl(albumId) {
    var base = uploadForm ? uploadForm.getAttribute("data-name-check-url") : "";
    if (!base) return "";
    return base + (base.indexOf("?") >= 0 ? "&" : "?") + "album_id=" + encodeURIComponent(albumId || "");
  }

  function showUploadConflicts(conflicts) {
    pendingConflicts = conflicts;
    if (!conflictDialog) {
      if (window.confirm("目标相册内有同名图片：\n" + conflicts.map(function (file) { return file.name; }).join("\n") + "\n\n仍然上传全部吗？")) {
        submitUpload();
      }
      return;
    }
    if (conflictList) {
      conflictList.textContent = "";
      conflicts.forEach(function (file) {
        var item = document.createElement("li");
        var code = document.createElement("code");
        code.textContent = file.name;
        item.appendChild(code);
        conflictList.appendChild(item);
      });
    }
    if (conflictSummary) {
      conflictSummary.textContent = "共 " + conflicts.length + " 个文件与相册中的现有图片同名。";
    }
    openDialog(conflictDialog);
  }

  function checkUploadNames(chosen) {
    var url = uploadNameUrl(albumSelect ? albumSelect.value : "");
    if (!url || typeof fetch !== "function") {
      submitUpload();
      return;
    }
    if (confirmButton) confirmButton.disabled = true;
    fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("name check failed");
        return response.json();
      })
      .then(function (data) {
        var existing = new Set((data.names || []).map(normalizedName));
        var conflicts = chosen.filter(function (file) {
          return existing.has(normalizedName(file.name));
        });
        if (!conflicts.length) {
          submitUpload();
          return;
        }
        showUploadConflicts(conflicts);
      })
      .catch(function () {
        // A failed preflight must not silently block an otherwise valid upload.
        submitUpload();
      })
      .finally(function () {
        if (confirmButton) confirmButton.disabled = selectedFiles().length === 0;
      });
  }

  function requestUploadWithNameCheck() {
    var chosen = selectedFiles();
    if (!chosen.length) {
      window.alert("请至少勾选一张要上传的图片。");
      return;
    }
    var capacityUrl = uploadForm ? uploadForm.getAttribute("data-capacity-url") : "";
    if (!capacityUrl || typeof fetch !== "function") {
      checkUploadNames(chosen);
      return;
    }
    var needed = chosen.reduce(function (sum, file) { return sum + (file.size || 0); }, 0);
    if (confirmButton) confirmButton.disabled = true;
    fetch(capacityUrl + "?bytes=" + encodeURIComponent(String(needed)), {
      headers: { "Accept": "application/json" },
      credentials: "same-origin"
    })
      .then(function (response) {
        if (!response.ok) throw new Error("capacity check failed");
        return response.json();
      })
      .then(function (data) {
        if (data && data.ok === false) {
          showStorageFull(data, needed);
          return;
        }
        checkUploadNames(chosen);
      })
      .catch(function () {
        // Capacity is a preflight convenience; the server still enforces it.
        checkUploadNames(chosen);
      })
      .finally(function () {
        if (confirmButton) {
          confirmButton.disabled = selectedFiles().length === 0;
        }
      });
  }

  if (conflictDialog) {
    conflictDialog.querySelectorAll("[data-upload-conflict-cancel]").forEach(function (button) {
      button.addEventListener("click", function () { closeDialog(conflictDialog); });
    });
    var conflictAll = conflictDialog.querySelector("[data-upload-conflict-all]");
    var conflictOthers = conflictDialog.querySelector("[data-upload-conflict-others]");
    if (conflictAll) {
      conflictAll.addEventListener("click", function () {
        closeDialog(conflictDialog);
        submitUpload();
      });
    }
    if (conflictOthers) {
      conflictOthers.addEventListener("click", function () {
        var conflictNames = new Set(pendingConflicts.map(function (file) { return normalizedName(file.name); }));
        var removed = candidates.filter(function (entry) {
          return conflictNames.has(normalizedName(entry.file.name));
        });
        candidates = candidates.filter(function (entry) {
          return !conflictNames.has(normalizedName(entry.file.name));
        });
        removed.forEach(revokeEntry);
        renderCandidates();
        closeDialog(conflictDialog);
        if (!selectedFiles().length) {
          window.alert("除了同名图片外，没有其他勾选的图片可上传。");
          return;
        }
        submitUpload();
      });
    }
    conflictDialog.addEventListener("click", function (event) {
      if (event.target === conflictDialog) closeDialog(conflictDialog);
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form === uploadForm && !syncInputToSelection()) {
      event.preventDefault();
      window.alert("请至少勾选一张要上传的图片。");
      return;
    }
    var message = (event.submitter && event.submitter.getAttribute("data-confirm")) ||
      form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    if ((form.getAttribute("method") || "get").toLowerCase() === "post") {
      var loadingMessage = form === uploadForm
        ? "正在上传并解析图片…"
        : form.id === "main-settings-form"
          ? "正在保存设置…"
          : "正在处理，请稍候…";
      showBusy(loadingMessage);
    }
    var submit = event.submitter || form.querySelector('button[type="submit"]');
    if (submit && !submit.dataset.noBusy) {
      submit.dataset.original = submit.textContent;
      submit.textContent = "处理中…";
      submit.disabled = true;
      window.setTimeout(function () {
        submit.disabled = false;
        submit.textContent = submit.dataset.original || "提交";
      }, 15000);
    }
  });

  // Settings save redirects to the same page. Remember the reading position so
  // the operator does not get thrown back to the top after every save.
  (function initSettingsScroll() {
    var key = "ostensory.settingsScroll";
    if (!/\/settings\/?$/.test(window.location.pathname)) return;
    if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
    var saved = null;
    try { saved = window.sessionStorage.getItem(key); } catch (error) { saved = null; }
    if (saved !== null) {
      var target = Number(saved);
      try { window.sessionStorage.removeItem(key); } catch (error) { /* ignore */ }
      if (Number.isFinite(target) && target >= 0) {
        window.requestAnimationFrame(function () {
          window.scrollTo(0, target);
          window.setTimeout(function () { window.scrollTo(0, target); }, 120);
        });
      }
    }
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement) || form.id !== "main-settings-form") return;
      try { window.sessionStorage.setItem(key, String(window.scrollY)); } catch (error) { /* ignore */ }
    });
  })();

  if (panel) {
    var selectAll = panel.querySelector("[data-select-all]");
    var selectNone = panel.querySelector("[data-select-none]");
    var clearAll = panel.querySelector("[data-candidate-clear]");
    if (selectAll) {
      selectAll.addEventListener("click", function () {
        candidates.forEach(function (entry) { entry.checked = true; });
        renderCandidates();
      });
    }
    if (selectNone) {
      selectNone.addEventListener("click", function () {
        candidates.forEach(function (entry) { entry.checked = false; });
        renderCandidates();
      });
    }
    if (clearAll) {
      clearAll.addEventListener("click", function () {
        candidates.forEach(revokeEntry);
        candidates = [];
        if (fileInput) fileInput.value = "";
        renderCandidates();
      });
    }
    if (confirmButton) confirmButton.addEventListener("click", requestUploadWithNameCheck);
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      addFiles(fileInput.files);
    });
  }

  if (dropzone && fileInput) {
    ["dragenter", "dragover"].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      addFiles(files);
    });
  }

  var albumSelect = uploadForm ? uploadForm.querySelector("[data-album-select]") : null;
  if (albumSelect) {
    var albumStore = "ostensory.uploadAlbum";
    try {
      var savedAlbum = window.localStorage.getItem(albumStore);
      if (savedAlbum !== null && Array.prototype.some.call(albumSelect.options, function (option) {
        return option.value === savedAlbum;
      })) {
        albumSelect.value = savedAlbum;
      }
    } catch (error) {
      // Private browsing can disable storage; default album still works.
    }
    albumSelect.addEventListener("change", function () {
      try {
        if (albumSelect.value) window.localStorage.setItem(albumStore, albumSelect.value);
        else window.localStorage.removeItem(albumStore);
      } catch (error) {
        // Ignore storage failures.
      }
    });
  }

  (function initBulkSelection() {
    document.querySelectorAll("[data-bulk-form]").forEach(function (form) {
      var group = form.getAttribute("data-bulk-group");
      if (!group) return;
      var items = Array.prototype.slice.call(
        document.querySelectorAll('[data-bulk-item][data-bulk-group="' + group + '"]')
      );
      var allBoxes = Array.prototype.slice.call(
        document.querySelectorAll('[data-bulk-all][data-bulk-group="' + group + '"]')
      );
      var count = document.querySelector('[data-bulk-count][data-bulk-group="' + group + '"]');
      var buttons = Array.prototype.slice.call(
        document.querySelectorAll('[data-bulk-submit][data-bulk-group="' + group + '"]')
      );
      function checkedItems() {
        return items.filter(function (item) { return item.checked; });
      }
      function update() {
        var selected = checkedItems().length;
        if (count) count.textContent = "已选 " + selected + " 项";
        allBoxes.forEach(function (box) {
          box.checked = items.length > 0 && selected === items.length;
          box.indeterminate = selected > 0 && selected < items.length;
        });
        buttons.forEach(function (button) { button.disabled = selected === 0; });
      }
      allBoxes.forEach(function (box) {
        box.addEventListener("change", function () {
          items.forEach(function (item) { item.checked = box.checked; });
          update();
        });
      });
      items.forEach(function (item) { item.addEventListener("change", update); });
      form.addEventListener("submit", function (event) {
        if (!checkedItems().length) {
          event.preventDefault();
          update();
        }
      });
      update();
    });
  })();

  (function initResetDialog() {
    var modal = document.querySelector("[data-reset-modal]");
    if (!modal) return;
    var open = document.querySelector("[data-reset-open]");
    var steps = Array.prototype.slice.call(modal.querySelectorAll("[data-reset-step]"));
    var check1 = modal.querySelector("[data-reset-check1]");
    var check2 = modal.querySelector("[data-reset-check2]");
    var next2 = modal.querySelector('[data-reset-next="2"]');
    var next3 = modal.querySelector('[data-reset-next="3"]');
    var phrase = modal.querySelector("[data-reset-phrase]");
    var final = modal.querySelector("[data-reset-final]");
    var confirm1 = modal.querySelector("[data-reset-confirm1]");
    var confirm2 = modal.querySelector("[data-reset-confirm2]");

    function showStep(number) {
      steps.forEach(function (step) {
        step.hidden = step.getAttribute("data-reset-step") !== String(number);
      });
    }
    function sync() {
      if (check1 && next2) next2.disabled = !check1.checked;
      if (check2 && next3) next3.disabled = !check2.checked;
      var ready = Boolean(
        check1 && check1.checked && check2 && check2.checked &&
        phrase && phrase.value.trim() === "完全重置"
      );
      if (final) final.disabled = !ready;
      if (confirm1) confirm1.value = check1 && check1.checked ? "1" : "";
      if (confirm2) confirm2.value = check2 && check2.checked ? "1" : "";
    }
    function reset() {
      modal.hidden = true;
      showStep(1);
      if (check1) check1.checked = false;
      if (check2) check2.checked = false;
      if (phrase) phrase.value = "";
      sync();
    }
    if (open) open.addEventListener("click", function () {
      modal.hidden = false;
      showStep(1);
      sync();
      if (next2) next2.focus();
    });
    modal.querySelectorAll("[data-reset-close]").forEach(function (button) {
      button.addEventListener("click", reset);
    });
    modal.querySelectorAll("[data-reset-next]").forEach(function (button) {
      button.addEventListener("click", function () {
        showStep(Number(button.getAttribute("data-reset-next")));
        if (button.getAttribute("data-reset-next") === "3" && phrase) phrase.focus();
      });
    });
    [check1, check2].forEach(function (box) {
      if (box) box.addEventListener("change", sync);
    });
    if (phrase) phrase.addEventListener("input", sync);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.hidden) reset();
    });
  })();

  renderCandidates();
  initAdminImages(document);
  initAdminTheme();

  document.addEventListener("focusin", function (event) {
    var field = event.target;
    if (field && field.hasAttribute && field.hasAttribute("data-unlock-autofill") && field.hasAttribute("readonly")) {
      field.removeAttribute("readonly");
    }
  });
  document.addEventListener("click", function (event) {
    var field = event.target;
    if (field && field.hasAttribute && field.hasAttribute("data-unlock-autofill") && field.hasAttribute("readonly")) {
      field.removeAttribute("readonly");
      field.focus();
    }
  });

  (function initCursorPreview() {
    var select = document.querySelector("[data-cursor-style]");
    var urlInput = document.querySelector("[data-cursor-url]");
    var preview = document.querySelector("[data-cursor-preview]");
    if (!select || !preview) return;
    function xValue(name) {
      var input = document.querySelector('input[name="' + name + '"]');
      return input ? Number(input.value) || 0 : 0;
    }
    function sync() {
      var style = select.value;
      var url = urlInput ? urlInput.value.trim() : "";
      preview.className = "cursor-preview cursor-preview-" + style;
      preview.style.cursor = "";
      if (style === "custom" && /^(https?:\/\/|\/)[^\s"'()]+$/.test(url)) {
        preview.style.cursor = 'url("' + url + '") ' + xValue("cursor_hotspot_x") + " " + xValue("cursor_hotspot_y") + ", auto";
      }
    }
    select.addEventListener("change", sync);
    if (urlInput) urlInput.addEventListener("input", sync);
    document.querySelectorAll('input[name="cursor_hotspot_x"], input[name="cursor_hotspot_y"]').forEach(function (input) {
      input.addEventListener("input", sync);
    });
    sync();
  })();

  (function initCheckAll() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-check-all]"), function (master) {
      var group = master.getAttribute("data-check-all");
      master.addEventListener("change", function () {
        Array.prototype.forEach.call(
          document.querySelectorAll('[data-check-group="' + group + '"]'),
          function (input) { input.checked = master.checked; }
        );
      });
    });
  })();

  (function initIntroPreview() {
    var preview = document.querySelector("[data-intro-preview]");
    if (!preview) return;
    var input = document.querySelector("[data-intro-input]");
    var size = document.querySelector("[data-intro-size]");
    var styleSelect = document.querySelector("[data-intro-style]");
    var color = document.querySelector("[data-intro-color]");
    var quote = document.querySelector("[data-intro-quote]");
    var allowHtml = document.querySelector("[data-intro-allow-html]");
    var styleMap = {
      italic: ["italic", "400"],
      normal: ["normal", "400"],
      bold: ["normal", "700"],
      italic_bold: ["italic", "700"]
    };
    function sync() {
      var sizeValue = Number(size && size.value) || 15;
      sizeValue = Math.max(10, Math.min(72, sizeValue));
      var styleValue = (styleMap[styleSelect ? styleSelect.value : "italic"] || styleMap.italic);
      preview.style.fontSize = sizeValue + "px";
      preview.style.fontStyle = styleValue[0];
      preview.style.fontWeight = styleValue[1];
      var colorValue = color ? color.value.trim() : "";
      preview.style.color = colorValue;
      preview.classList.toggle("has-quote", !!(quote && quote.checked));
      if (input) {
        if (!input.value.trim()) preview.textContent = "";
        else if (allowHtml && allowHtml.checked) preview.innerHTML = input.value;
        else preview.textContent = input.value;
      }
    }
    [input, size, styleSelect, color, quote, allowHtml].forEach(function (field) {
      if (!field) return;
      field.addEventListener("input", sync);
      field.addEventListener("change", sync);
    });
    sync();
  })();

  (function initDedupDialog() {
    var dialog = document.querySelector("[data-dedup-dialog]");
    if (!dialog) return;
    function open() {
      if (typeof dialog.showModal === "function") {
        try {
          if (!dialog.open) dialog.showModal();
          return;
        } catch (error) { /* fall through */ }
      }
      dialog.setAttribute("open", "");
    }
    function close() {
      if (typeof dialog.close === "function" && dialog.open) dialog.close();
      else dialog.removeAttribute("open");
    }
    document.querySelectorAll("[data-open-dedup]").forEach(function (button) {
      button.addEventListener("click", open);
    });
    dialog.querySelectorAll("[data-dedup-close]").forEach(function (button) {
      button.addEventListener("click", close);
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) close();
    });
    if (dialog.getAttribute("data-open") === "1") open();
  })();

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button[data-confirm]") : null;
    if (!button) return;
    var message = button.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  initFormScrollRestore();

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (!button) return;
    var target = document.querySelector(button.getAttribute("data-copy-target"));
    if (!target) return;
    var text = "value" in target ? target.value : (target.textContent || "");
    function done() {
      var old = button.textContent;
      button.textContent = "已复制";
      window.setTimeout(function () { button.textContent = old; }, 1400);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done);
    } else {
      var area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); done(); } finally { document.body.removeChild(area); }
    }
  });
})();