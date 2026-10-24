(function () {
  "use strict";

  var form = document.querySelector("[data-guest-upload-form]");
  if (!form) return;
  var messages = window.OSTENSORY_I18N || {};
  var fileInput = form.querySelector("[data-guest-file-input]");
  var grid = form.querySelector("[data-guest-grid]");
  var summary = form.querySelector("[data-guest-summary]");
  var selectedLabel = form.querySelector("[data-guest-selected]");
  var submit = form.querySelector("[data-guest-submit]");
  var titlesInput = form.querySelector("[data-guest-titles]");
  var dropzone = form.querySelector("[data-guest-dropzone]");
  var album = form.querySelector("[data-guest-album]");
  var maxBytes = Number(form.getAttribute("data-guest-max-bytes")) || 0;
  var maxFiles = Number(form.getAttribute("data-guest-max-files")) || 100;
  var candidates = [];

  function t(key, values) {
    var text = messages[key] || key;
    if (values) {
      Object.keys(values).forEach(function (name) {
        text = text.replace(new RegExp("\\{" + name + "\\}", "g"), String(values[name]));
      });
    }
    return text;
  }

  function showBusy(message) {
    var overlay = document.createElement("div");
    overlay.className = "loading-overlay";
    overlay.setAttribute("role", "status");
    overlay.setAttribute("aria-live", "polite");
    var card = document.createElement("div");
    card.className = "loading-card";
    var spinner = document.createElement("span");
    spinner.className = "spinner";
    var text = document.createElement("p");
    text.textContent = message;
    card.appendChild(spinner);
    card.appendChild(text);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    document.body.classList.add("is-busy");
  }

  function humanSize(bytes) {
    if (!bytes) return "0 B";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function keyOf(file) {
    return [file.name, file.size, file.lastModified || 0].join("|");
  }

  function chosen() {
    return candidates.filter(function (item) { return item.checked; });
  }

  function updateState() {
    var count = chosen().length;
    if (summary) {
      summary.textContent = candidates.length
        ? t("guest.summary", { total: candidates.length, count: count })
        : t("guest.no_files");
    }
    if (selectedLabel) selectedLabel.textContent = t("guest.selected", { count: count });
    if (submit) submit.disabled = count === 0;
  }

  function render() {
    grid.textContent = "";
    if (!candidates.length) {
      var empty = document.createElement("p");
      empty.className = "candidate-empty";
      empty.textContent = t("guest.candidate_empty");
      grid.appendChild(empty);
      updateState();
      return;
    }
    candidates.forEach(function (item, index) {
      var card = document.createElement("div");
      card.className = "guest-candidate" + (item.checked ? "" : " is-unchecked");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = item.checked;
      box.setAttribute("aria-label", t("guest.select_file", { name: item.file.name }));
      box.addEventListener("change", function () {
        item.checked = box.checked;
        card.classList.toggle("is-unchecked", !item.checked);
        updateState();
      });
      var image = document.createElement("img");
      image.addEventListener("load", function () { image.classList.add("is-loaded"); });
      image.addEventListener("error", function () { image.classList.add("is-loaded"); });
      image.src = item.url;
      image.alt = "";
      var meta = document.createElement("div");
      meta.className = "guest-candidate-meta";
      var name = document.createElement("span");
      name.className = "guest-candidate-name";
      name.textContent = item.file.name;
      var size = document.createElement("span");
      size.className = "guest-candidate-size";
      size.textContent = humanSize(item.file.size);
      var title = document.createElement("input");
      title.type = "text";
      title.maxLength = 300;
      title.value = item.title;
      title.placeholder = t("guest.work_title");
      title.setAttribute("aria-label", t("guest.work_title_aria"));
      title.addEventListener("input", function () { item.title = title.value; });
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "candidate-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", t("guest.remove", { name: item.file.name }));
      remove.addEventListener("click", function () {
        URL.revokeObjectURL(item.url);
        candidates.splice(index, 1);
        render();
      });
      meta.appendChild(name);
      meta.appendChild(size);
      meta.appendChild(title);
      card.appendChild(box);
      card.appendChild(image);
      card.appendChild(meta);
      card.appendChild(remove);
      grid.appendChild(card);
    });
    updateState();
  }

  function addFiles(list) {
    if (!list) return;
    var overflowed = false;
    Array.prototype.forEach.call(list, function (file) {
      if (candidates.length >= maxFiles) {
        overflowed = true;
        return;
      }
      if (maxBytes && file.size > maxBytes) {
        window.alert(t("guest.too_large", { name: file.name, size: humanSize(maxBytes) }));
        return;
      }
      var key = keyOf(file);
      if (candidates.some(function (item) { return item.key === key; })) return;
      candidates.push({
        key: key,
        file: file,
        checked: true,
        title: "",
        url: URL.createObjectURL(file)
      });
    });
    if (overflowed) window.alert(t("guest.too_many", { count: maxFiles }));
    if (fileInput) fileInput.value = "";
    render();
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () { addFiles(fileInput.files); });
  }

  if (dropzone) {
    ["dragenter", "dragover"].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.add("is-dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.remove("is-dragover");
      });
    });
    dropzone.addEventListener("drop", function (event) {
      addFiles(event.dataTransfer && event.dataTransfer.files);
    });
  }

  var selectAll = form.querySelector("[data-guest-select-all]");
  var selectNone = form.querySelector("[data-guest-select-none]");
  var clear = form.querySelector("[data-guest-clear]");
  if (selectAll) selectAll.addEventListener("click", function () {
    candidates.forEach(function (item) { item.checked = true; });
    render();
  });
  if (selectNone) selectNone.addEventListener("click", function () {
    candidates.forEach(function (item) { item.checked = false; });
    render();
  });
  if (clear) clear.addEventListener("click", function () {
    candidates.forEach(function (item) { URL.revokeObjectURL(item.url); });
    candidates = [];
    render();
  });

  form.addEventListener("submit", function (event) {
    var picked = chosen();
    if (!picked.length) {
      event.preventDefault();
      window.alert(t("guest.need_one"));
      return;
    }
    if (picked.length > maxFiles) {
      event.preventDefault();
      window.alert(t("guest.too_many", { count: maxFiles }));
      return;
    }
    if (maxBytes && picked.some(function (item) { return item.file.size > maxBytes; })) {
      event.preventDefault();
      window.alert(t("guest.some_too_large"));
      return;
    }
    if (typeof DataTransfer !== "function") return;
    try {
      var transfer = new DataTransfer();
      picked.forEach(function (item) { transfer.items.add(item.file); });
      fileInput.files = transfer.files;
      if (titlesInput) {
        titlesInput.value = JSON.stringify(picked.map(function (item) {
          return item.title || "";
        }));
      }
    } catch (error) {
      event.preventDefault();
      window.alert(t("guest.browser_error"));
      return;
    }
    if (submit) {
      submit.disabled = true;
      submit.textContent = t("guest.uploading");
    }
    showBusy(t("guest.busy"));
  });

  if (album) {
    var store = "ostensory.guestAlbum";
    try {
      var saved = window.localStorage.getItem(store);
      if (saved !== null && Array.prototype.some.call(album.options, function (option) {
        return option.value === saved;
      })) {
        album.value = saved;
      }
    } catch (error) {
      // Storage can be disabled; the server-side default still works.
    }
    album.addEventListener("change", function () {
      try {
        if (album.value) window.localStorage.setItem(store, album.value);
      } catch (error) {
        // Ignore storage failures.
      }
    });
  }

  render();
})();