const listEl = document.getElementById("modpack-list");
const statusEl = document.getElementById("status");
const versionSelect = document.getElementById("game-version");
const browseBtn = document.getElementById("browse-exe");
const detectBtn = document.getElementById("detect-exe");
const detectResultsEl = document.getElementById("detect-results");
const exePathEl = document.getElementById("exe-path");
const installBtn = document.getElementById("install");
const logEl = document.getElementById("progress-log");
const logDetailsEl = document.getElementById("log-details");
const progressEl = document.getElementById("progress");
const progressPhaseEl = document.getElementById("progress-phase");
const resultBannerEl = document.getElementById("result-banner");

let selectedExe = null;
let lastBuild = null; // {output, size_bytes} once a build finishes, for "export a copy"

// Human-readable labels for install.py's on_progress() phase names; unlisted phases fall back to the raw name.
const PHASE_LABELS = {
  check: "Checking your game copy",
  gdre: "Fetching gdRE Tools",
  decompile: "Decompiling your game",
  version: "Reading game version",
  repair: "Repairing decompiler artifacts",
  combine: "Combining enabled modpacks",
  patch: "Applying patches",
  inject: "Adding mod files",
  autoload: "Registering autoloads",
  godot: "Fetching Godot",
  export: "Exporting the build",
  done: "Done",
};

function api() {
  if (!window.pywebview) throw new Error("pywebview bridge not ready yet");
  return window.pywebview.api;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

// Stable, distinct color per pack id so the fallback icon isn't a wall of identical grey squares with more than a couple mods.
function colorForId(id) {
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  const hue = hash % 360;
  return `hsl(${hue} 55% 62%)`;
}

function render(packs) {
  listEl.innerHTML = "";
  if (packs.length === 0) {
    listEl.innerHTML = '<div class="empty-state">No modpacks found under modpacks/.</div>';
    return;
  }

  const reorderable = packs.filter((p) => p.enabled && !p.forced)
    .sort((a, b) => a.priority - b.priority);

  for (const pack of packs) {
    const row = document.createElement("div");
    row.className = "pack-row" + (pack.enabled ? "" : " disabled");

    const icon = document.createElement("div");
    icon.className = "pack-icon";
    icon.style.background = colorForId(pack.id);
    icon.textContent = (pack.name || "?").trim().charAt(0).toUpperCase();
    row.append(icon);
    if (pack.image) loadPackIcon(pack.id, icon);

    const toggle = document.createElement("label");
    toggle.className = "toggle";
    toggle.title = pack.forced
      ? "Always installed when the game version supports it."
      : (pack.enabled ? "Disable this modpack" : "Enable this modpack");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = pack.enabled;
    check.disabled = pack.forced;
    check.addEventListener("change", async () => {
      render(await api().set_enabled(pack.id, check.checked));
    });
    toggle.append(check,
      Object.assign(document.createElement("span"), { className: "toggle-track" }),
      Object.assign(document.createElement("span"), { className: "toggle-thumb" }));
    row.append(toggle);

    const info = document.createElement("div");
    info.className = "pack-info";
    const legacyTag = pack.modloader === "old"
      ? '<span class="tag legacy-tag" title="Imported from a pre-modloader project; version and links may be out of date.">legacy</span>'
      : "";
    const forcedTag = pack.forced
      ? '<span class="tag forced-tag" title="Always installed when the game version supports it, regardless of this toggle.">core</span>'
      : "";
    info.innerHTML = `
      <div class="title-line">
        <strong>${escapeHtml(pack.name)}</strong>
        <span class="version">v${escapeHtml(pack.version)}</span>
        ${legacyTag}${forcedTag}
      </div>
      ${pack.description ? `<div class="desc">${escapeHtml(pack.description)}</div>` : ""}
      <div class="meta">by ${escapeHtml(pack.author || "unknown")} &middot; supports ${escapeHtml(pack.game_versions.join(", ") || "?")}</div>
    `;
    row.append(info);

    if (pack.enabled && !pack.forced) {
      const idx = reorderable.findIndex((p) => p.id === pack.id);
      const reorder = document.createElement("div");
      reorder.className = "reorder";
      const up = document.createElement("button");
      up.className = "btn btn-ghost";
      up.textContent = "▲";
      up.title = "Load earlier";
      up.disabled = idx <= 0;
      up.addEventListener("click", async () => render(await api().move_pack(pack.id, -1)));
      const down = document.createElement("button");
      down.className = "btn btn-ghost";
      down.textContent = "▼";
      down.title = "Load later";
      down.disabled = idx < 0 || idx >= reorderable.length - 1;
      down.addEventListener("click", async () => render(await api().move_pack(pack.id, 1)));
      reorder.append(up, down);
      row.append(reorder);
    } else {
      row.append(Object.assign(document.createElement("div"), { className: "reorder-spacer" }));
    }

    listEl.append(row);
  }
}

async function loadPackIcon(packId, iconEl) {
  try {
    const src = await api().get_pack_image(packId);
    if (!src) return;
    const img = document.createElement("img");
    img.src = src;
    img.alt = "";
    iconEl.textContent = "";
    iconEl.style.background = "none";
    iconEl.append(img);
  } catch {
    // keep the letter placeholder
  }
}

function updateInstallEnabled() {
  installBtn.disabled = !selectedExe || installBtn.dataset.busy === "1";
}

function logLine(text, cls) {
  const line = document.createElement("div");
  line.className = "log-line" + (cls ? " " + cls : "");
  line.textContent = text;
  logEl.append(line);
  logEl.scrollTop = logEl.scrollHeight;
}

async function refresh() {
  render(await api().list_modpacks());
}

function selectExe(path) {
  selectedExe = path;
  exePathEl.textContent = path;
  exePathEl.title = path;
  detectResultsEl.hidden = true;
  detectResultsEl.innerHTML = "";
  updateInstallEnabled();
}

browseBtn.addEventListener("click", async () => {
  const path = await api().pick_game_exe();
  if (path) selectExe(path);
});

detectBtn.addEventListener("click", async () => {
  detectBtn.disabled = true;
  try {
    const candidates = await api().find_game_candidates();
    detectResultsEl.innerHTML = "";
    if (candidates.length === 0) {
      detectResultsEl.innerHTML = '<div class="empty-state">Nothing found automatically, use "Choose game exe" instead.</div>';
    } else {
      for (const path of candidates) {
        const btn = document.createElement("button");
        btn.className = "btn btn-ghost";
        btn.textContent = path;
        btn.addEventListener("click", () => selectExe(path));
        detectResultsEl.append(btn);
      }
    }
    detectResultsEl.hidden = false;
  } finally {
    detectBtn.disabled = false;
  }
});

document.getElementById("check-conflicts").addEventListener("click", async () => {
  statusEl.textContent = "checking…";
  statusEl.className = "";
  const result = await api().check_conflicts(versionSelect.value);
  statusEl.textContent = result.ok
    ? `no conflicts for ${versionSelect.options[versionSelect.selectedIndex].text}`
    : `conflict: ${result.error}`;
  statusEl.className = result.ok ? "ok" : "error";
});

installBtn.addEventListener("click", async () => {
  if (!selectedExe) return;
  logEl.innerHTML = "";
  lastBuild = null;
  resultBannerEl.hidden = true;
  logDetailsEl.open = false;
  progressEl.hidden = false;
  progressPhaseEl.textContent = "Starting…";
  installBtn.textContent = "Installing…";
  installBtn.dataset.busy = "1";
  updateInstallEnabled();
  logLine(`starting install from ${selectedExe}`);
  const ack = await api().start_install(selectedExe);
  if (!ack.ok) {
    progressEl.hidden = true;
    showResult(false, ack.error);
    logLine(ack.error, "error");
    installBtn.textContent = "Install & Launch";
    installBtn.dataset.busy = "0";
    updateInstallEnabled();
  }
  // otherwise wait for window.onInstallEvent progress/done/error callbacks
});

function showResult(ok, headline, sub) {
  resultBannerEl.hidden = false;
  resultBannerEl.className = "result-banner " + (ok ? "ok" : "error");
  resultBannerEl.innerHTML = `
    <div class="headline">${escapeHtml(headline)}</div>
    ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}
    <div class="actions"></div>
  `;
}

window.onInstallEvent = (event) => {
  if (event.type === "progress") {
    progressPhaseEl.textContent = PHASE_LABELS[event.phase] || event.message;
    logLine(`[${event.phase}] ${event.message}`);
    return;
  }

  progressEl.hidden = true;
  installBtn.textContent = "Install & Launch";
  installBtn.dataset.busy = "0";
  updateInstallEnabled();

  if (event.type === "error") {
    showResult(false, "Install failed", event.message);
    logLine(event.message, "error");
    logDetailsEl.open = true;
    return;
  }

  // done
  const mb = Math.round(event.size_bytes / 1048576);
  const launchNote = event.launched
    ? "Launched."
    : `Built, but could not launch it automatically: ${event.launch_error}`;
  showResult(true, `Built (${mb} MB) for game ${event.game_version}`, launchNote);
  logLine(`built ${event.output} (${mb} MB) for game ${event.game_version}`, "ok");
  logLine(event.launched ? "launched." : `could not launch: ${event.launch_error}`,
    event.launched ? "ok" : "error");
  lastBuild = { output: event.output };
  showExportOption();
};

function showExportOption() {
  const actions = resultBannerEl.querySelector(".actions");
  if (!lastBuild || !actions) return;
  const btn = document.createElement("button");
  btn.className = "btn btn-ghost";
  btn.textContent = "Export this build a copy elsewhere…";
  btn.addEventListener("click", async () => {
    const suggested = lastBuild.output.split(/[\\/]/).pop();
    const dest = await api().pick_export_destination(suggested);
    if (!dest) return;
    const result = await api().export_build(lastBuild.output, dest);
    logLine(result.ok ? `copied to ${result.path}` : result.error, result.ok ? "ok" : "error");
    logDetailsEl.open = true;
  });
  actions.append(btn);
}

window.addEventListener("pywebviewready", refresh);
