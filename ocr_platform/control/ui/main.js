import { createJsonRequester } from "./api.js";
import { clearSessionToken, loadSessionToken, saveSessionToken } from "./auth.js";
import { DATABASE_STATUS_API, DIAGNOSTICS_API } from "./diagnostics.js";
import { JOBS_API_ROOT, jobApiPath } from "./jobs.js";
import { DEFAULT_MODEL_PROFILE, MODEL_PROFILES, MODEL_PROFILES_API, modelProfileApiPath } from "./profiles.js";
import { REMOTE_ADMIN_API } from "./remote-admin.js";
import { state, ui } from "./state.js";
import { SERVERS_API_ROOT, serverApiPath } from "./workers.js";

const REFRESH_MS = 3000;
const requestJson = createJsonRequester(() => ui.controlApiToken.value.trim());

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function setMessage(el, text, isError = false) {
  el.textContent = text;
  if (text) {
    el.classList.add("message");
    el.classList.toggle("error", isError);
  } else {
    el.classList.remove("error");
  }
}

function jobNeedsAttention(job) {
  if (!job) return false;
  const attentionShards = Array.isArray(job.attention_shards) ? job.attention_shards.length : 0;
  return Boolean(
    job.status === "failed" ||
    job.status === "stopping" ||
    job.execution_paused ||
    job.error_message ||
    attentionShards ||
    Number(job.retrying_shards || 0) > 0 ||
    Number(job.stale_shards || 0) > 0
  );
}

function renderOperationsSummary() {
  if (!ui.opsQueueSummary) return;
  const jobs = Array.isArray(state.jobs) ? state.jobs : [];
  const running = jobs.filter((job) => ["running", "scanning", "sharding"].includes(job.status)).length;
  const queued = jobs.filter((job) => job.status === "queued").length;
  const attention = jobs.filter(jobNeedsAttention).length;
  const visible = visibleServers();
  const readiness = visible.map((server) => classifyWorkerReadiness(server));
  const doctor = state.deploymentDoctor;
  const doctorWorkers = doctor && doctor.workers ? doctor.workers : null;
  const workerTotal = visible.length || (doctorWorkers ? Number(doctorWorkers.total || 0) : 0);
  const ready = doctorWorkers
    ? Math.min(Number(doctorWorkers.ready || 0), workerTotal)
    : readiness.filter((item) => item.level === "ready").length;
  const doctorText = doctor ? (doctor.ok ? "Ready" : "Check") : "-";
  const doctorNote = doctor
    ? (doctor.ok ? "Ready to submit UI jobs" : `${(doctor.issues || []).length} issue(s)`)
    : "deployment doctor";

  ui.opsQueueSummary.innerHTML = [
    `<span class="summary-label">Queue</span>`,
    `<strong>${escapeHtml(running)}</strong>`,
    `<span class="summary-note">${escapeHtml(queued)} queued jobs</span>`,
  ].join("");
  ui.opsAttentionSummary.innerHTML = [
    `<span class="summary-label">Needs attention</span>`,
    `<strong>${escapeHtml(attention)}</strong>`,
    `<span class="summary-note">retrying, failed, or stale work</span>`,
  ].join("");
  ui.opsWorkerSummary.innerHTML = [
    `<span class="summary-label">Workers</span>`,
    `<strong>${escapeHtml(ready)}/${escapeHtml(workerTotal)}</strong>`,
    `<span class="summary-note">ready / visible workers</span>`,
  ].join("");
  ui.opsReadinessSummary.innerHTML = [
    `<span class="summary-label">Readiness</span>`,
    `<strong><span class="${statusClass(doctor && doctor.ok ? "succeeded" : "warning")}">${escapeHtml(doctorText)}</span></strong>`,
    `<span class="summary-note">${escapeHtml(doctorNote)}</span>`,
  ].join("");
}

function selectedAllowedServerIds() {
  const selected = selectedInputMode() === "directory"
    ? [ui.singleWorkerSelect.value].filter(Boolean)
    : Array.from(ui.selectedWorkersList.querySelectorAll('input[type="checkbox"]:checked')).map((input) => input.value);
  const manual = ui.manualWorkerIds.value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return Array.from(new Set([...selected, ...manual]));
}

function selectedServerId() {
  return selectedAllowedServerIds()[0] || "";
}

function selectedInputMode() {
  const mode = ui.executionMode.value;
  if (mode === "single_server") return "directory";
  if (mode === "existing_manifest") return "existing_manifest";
  if (mode === "distributed_manifest_scan") return "distributed_remote_folder_snapshot";
  return "remote_folder_snapshot";
}

function remoteWorkerSharedRoots() {
  return ui.remoteWorkerSharedRoots.value
    .split(":")
    .map((item) => item.trim())
    .filter(Boolean);
}

function renderRemoteWorkerTargets(targets) {
  state.remoteWorkerTargets = targets || [];
  const options = state.remoteWorkerTargets.map((target) => {
    const suffix = target.hostname && target.hostname !== target.host ? ` (${target.hostname})` : "";
    return `<option value="${escapeHtml(target.id)}">${escapeHtml(target.host + suffix)}</option>`;
  });
  ui.remoteWorkerTarget.innerHTML = [
    `<option value="">manual target</option>`,
    ...options
  ].join("");
}

function applyRemoteWorkerTarget(targetId) {
  const target = state.remoteWorkerTargets.find((item) => item.id === targetId);
  if (!target) {
    return;
  }
  ui.remoteWorkerHost.value = target.host || "";
  ui.remoteWorkerSshUser.value = target.ssh_user || "";
  ui.remoteWorkerServerId.value = target.server_id || target.hostname || target.host || "";
  ui.remoteWorkerServiceUser.value = target.service_user || target.ssh_user || "ocr-agent";
  ui.remoteWorkerServiceGroup.value = target.service_group || target.service_user || target.ssh_user || "ocr-agent";
  ui.remoteWorkerRepoDir.value = target.repo_dir || "/opt/ocr-platform/ocrparser";
  ui.remoteWorkerControlUrl.value = target.control_url || ui.remoteWorkerControlUrl.value;
  if (Array.isArray(target.shared_roots) && target.shared_roots.length) {
    ui.remoteWorkerSharedRoots.value = target.shared_roots.join(":");
  }
}

async function loadRemoteWorkerTargets() {
  const payload = await requestJson(REMOTE_ADMIN_API.targets);
  renderRemoteWorkerTargets(payload.targets || []);
}

function remoteWorkerBasePayload() {
  const payload = {
    host: ui.remoteWorkerHost.value.trim(),
    ssh_user: ui.remoteWorkerSshUser.value.trim() || null,
  };
  if (!payload.host) {
    throw new Error("remote worker host is required");
  }
  return payload;
}

function remoteWorkerInstallPayload() {
  const payload = {
    ...remoteWorkerBasePayload(),
    server_id: ui.remoteWorkerServerId.value.trim(),
    service_user: ui.remoteWorkerServiceUser.value.trim() || "ocr-agent",
    service_group: ui.remoteWorkerServiceGroup.value.trim() || "ocr-agent",
    repo_dir: ui.remoteWorkerRepoDir.value.trim() || "/opt/ocr-platform/ocrparser",
    control_url: ui.remoteWorkerControlUrl.value.trim(),
    shared_roots: remoteWorkerSharedRoots(),
  };
  if (!payload.server_id) {
    throw new Error("remote worker server_id is required for install dry-run");
  }
  if (!payload.control_url) {
    throw new Error("remote worker control_url is required for install dry-run");
  }
  if (!payload.shared_roots.length) {
    throw new Error("remote worker shared_roots is required");
  }
  return payload;
}

function renderRemoteWorkerResult(payload) {
  ui.remoteWorkerOutput.textContent = [
    `action: ${payload.action}`,
    `ok: ${payload.ok}`,
    `return_code: ${payload.return_code}`,
    `command: ${(payload.command || []).join(" ")}`,
    "",
    "stdout:",
    payload.stdout || "",
    "",
    "stderr:",
    payload.stderr || "",
  ].join("\n");
}

function workerScaleSharedRoots() {
  return ui.workerScaleSharedRoots.value
    .split(":")
    .map((item) => item.trim())
    .filter(Boolean);
}

function workersOnHost(host) {
  return visibleServers().filter((server) => server.host === host);
}

function deriveWorkerScalePrefix(server) {
  const raw = String(server.id || server.host || "ocr-worker");
  return raw.replace(/-\d+$/, "").replace(/[^A-Za-z0-9_.:-]+/g, "-") || "ocr-worker";
}

function selectWorkerScaleTarget(serverId) {
  const server = state.servers.find((item) => item.id === serverId);
  if (!server) return;
  const caps = server.capabilities || {};
  const sharedRoots = Array.isArray(caps.shared_roots) ? caps.shared_roots : [];
  const hostWorkers = workersOnHost(server.host);
  state.workerScaleTarget = server;
  ui.workerScaleHost.value = server.host || server.id;
  ui.workerScaleSeedServerId.value = server.id;
  ui.workerScaleServerIdPrefix.value = deriveWorkerScalePrefix(server);
  ui.workerScaleCurrentCount.value = hostWorkers.length;
  ui.workerScaleTargetCount.value = Math.max(2, hostWorkers.length || 1);
  ui.workerScaleRepoDir.value = caps.repo_dir || ui.workerScaleRepoDir.value || "/opt/ocr-platform/ocrparser";
  if (sharedRoots.length) {
    ui.workerScaleSharedRoots.value = sharedRoots.join(":");
  }
  ui.workerScaleResult.innerHTML = `<span class="${statusClass("ready")}">Scale target selected</span> <span class="small">${escapeHtml(server.id)}</span>`;
}

function workerScalePayload() {
  const targetCount = Number(ui.workerScaleTargetCount.value || 0);
  const payload = {
    host: ui.workerScaleHost.value.trim(),
    ssh_user: ui.workerScaleSshUser.value.trim() || null,
    repo_dir: ui.workerScaleRepoDir.value.trim() || "/opt/ocr-platform/ocrparser",
    service_user: ui.workerScaleServiceUser.value.trim() || "ocr-agent",
    service_group: ui.workerScaleServiceGroup.value.trim() || "ocr-agent",
    target_count: targetCount,
    seed_server_id: ui.workerScaleSeedServerId.value.trim() || null,
    server_id_prefix: ui.workerScaleServerIdPrefix.value.trim(),
    shared_roots: workerScaleSharedRoots(),
  };
  if (!payload.host) throw new Error("worker scale host is required");
  if (!payload.server_id_prefix) throw new Error("worker scale server_id_prefix is required");
  if (!Number.isInteger(targetCount) || targetCount < 1 || targetCount > 16) {
    throw new Error("worker scale target_count must be between 1 and 16");
  }
  return payload;
}

function renderWorkerScaleResult(payload, waitingText = "") {
  const items = Array.isArray(payload.plan_items) ? payload.plan_items : [];
  const rows = items.length ? items.map((item) => {
    const status = item.status || "pending";
    return `<tr>
      <td>${escapeHtml(item.action || "")}</td>
      <td><span class="${statusClass(status)}">${escapeHtml(status)}</span></td>
      <td>${escapeHtml(item.instance || "-")}</td>
      <td>${escapeHtml(item.server_id || "-")}</td>
      <td>${escapeHtml(item.message || "")}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="muted">No structured plan items returned</td></tr>`;
  const debug = `<details class="advanced"><summary>debug output</summary><pre class="log-box">${escapeHtml([
    `action: ${payload.action || ""}`,
    `ok: ${payload.ok}`,
    `return_code: ${payload.return_code}`,
    `command: ${(payload.command || []).join(" ")}`,
    "",
    payload.stdout || "",
    payload.stderr || "",
  ].join("\n"))}</pre></details>`;
  ui.workerScaleResult.innerHTML = `
    <div class="small">${escapeHtml(waitingText || "")}</div>
    <div class="table-scroll"><table class="compact-table">
      <thead><tr><th>Action</th><th>Status</th><th>Instance</th><th>Server ID</th><th>Message</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${debug}`;
}

async function runWorkerScalePlan() {
  const result = await requestJson(REMOTE_ADMIN_API.scalePlan, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(workerScalePayload()),
  });
  renderWorkerScaleResult(result);
}

function countedScaledWorkers(host, prefix) {
  return visibleServers().filter((server) => {
    const readiness = classifyWorkerReadiness(server);
    return server.host === host && String(server.id || "").startsWith(prefix) && readiness.level === "ready";
  }).length;
}

async function waitForWorkerScaleHeartbeat(host, prefix, targetCount) {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await loadServers();
    const count = countedScaledWorkers(host, prefix);
    if (count >= targetCount) {
      return `Scaled: ${count}/${targetCount} ready workers`;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  return `Waiting for heartbeat confirmation: ${countedScaledWorkers(host, prefix)}/${targetCount} ready workers`;
}

async function applyWorkerScale() {
  const payload = workerScalePayload();
  const result = await requestJson(REMOTE_ADMIN_API.scaleApply, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  renderWorkerScaleResult(result, "Waiting for heartbeat confirmation");
  const heartbeat = await waitForWorkerScaleHeartbeat(payload.host, payload.server_id_prefix, payload.target_count);
  renderWorkerScaleResult(result, heartbeat);
}

async function runRemoteWorkerPreflight() {
  const payload = {
    ...remoteWorkerBasePayload(),
    service_user: ui.remoteWorkerServiceUser.value.trim() || "ocr-agent",
    service_group: ui.remoteWorkerServiceGroup.value.trim() || "ocr-agent",
    repo_dir: ui.remoteWorkerRepoDir.value.trim() || "/opt/ocr-platform/ocrparser",
    shared_roots: remoteWorkerSharedRoots(),
  };
  const result = await requestJson(REMOTE_ADMIN_API.preflight, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  renderRemoteWorkerResult(result);
}

async function runRemoteWorkerInstallDryRun() {
  const result = await requestJson(REMOTE_ADMIN_API.installDryRun, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(remoteWorkerInstallPayload()),
  });
  renderRemoteWorkerResult(result);
}

async function runRemoteWorkerInstallApply() {
  const result = await requestJson(REMOTE_ADMIN_API.installApply, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(remoteWorkerInstallPayload()),
  });
  renderRemoteWorkerResult(result);
}

async function runRemoteWorkerServiceAction() {
  const result = await requestJson(REMOTE_ADMIN_API.service, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...remoteWorkerBasePayload(),
      action: ui.remoteWorkerServiceAction.value,
    }),
  });
  renderRemoteWorkerResult(result);
}

function isPoolInputMode() {
  return selectedInputMode() !== "directory";
}

function visibleServers(servers = state.servers) {
  return servers.filter((server) => server.id !== "__server_pool__");
}

function onlineWorkers(servers = state.servers) {
  return visibleServers(servers).filter((server) => !server.is_stale && server.status !== "offline");
}

function sharedPathReady(pathInfo) {
  return Boolean(pathInfo && pathInfo.exists && pathInfo.is_dir && pathInfo.readable);
}

function nonnegativeCount(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
}

function workerEventSpoolBacklog(server) {
  const spool = (server.capabilities && server.capabilities.event_spool) || {};
  const pendingEvents = nonnegativeCount(spool.pending_events);
  const pendingLogs = nonnegativeCount(spool.pending_logs);
  const failedEvents = nonnegativeCount(spool.failed_events);
  const failedLogs = nonnegativeCount(spool.failed_logs);
  const droppedEvents = nonnegativeCount(spool.dropped_events);
  const droppedLogs = nonnegativeCount(spool.dropped_logs);
  return {
    dir: spool.dir || "",
    pending_events: pendingEvents,
    pending_logs: pendingLogs,
    failed_events: failedEvents,
    failed_logs: failedLogs,
    dropped_events: droppedEvents,
    dropped_logs: droppedLogs,
    total: pendingEvents + pendingLogs + failedEvents + failedLogs + droppedEvents + droppedLogs,
  };
}

function workerPendingShardUpdateBacklog(server) {
  const pendingUpdates = (server.capabilities && server.capabilities.pending_shard_updates) || {};
  const pending = nonnegativeCount(pendingUpdates.pending);
  const failed = nonnegativeCount(pendingUpdates.failed);
  return {
    pending,
    failed,
    total: pending + failed,
  };
}

function classifyWorkerReadiness(server, eligibilityItem = null) {
  const caps = server.capabilities || {};
  const sharedPaths = Array.isArray(caps.shared_paths) ? caps.shared_paths : [];
  const pressure = caps.resource_pressure || {};
  const eventSpool = workerEventSpoolBacklog(server);
  const shardUpdateSpool = workerPendingShardUpdateBacklog(server);
  const blockers = [];
  const warnings = [];

  if (server.is_stale) {
    blockers.push("stale heartbeat");
  }
  if (server.status === "offline") {
    blockers.push("offline");
  }
  if (!sharedPaths.some(sharedPathReady)) {
    blockers.push("no readable shared path");
  }
  if (eligibilityItem && !eligibilityItem.can_access) {
    blockers.push(`input path: ${eligibilityItem.reason || "not accessible"}`);
  }
  if (pressure.constrained) {
    const reason = Array.isArray(pressure.reasons) && pressure.reasons.length
      ? pressure.reasons[0]
      : "resource pressure";
    blockers.push(`resource constrained: ${reason}`);
  }

  ["git_ref", "script_version", "python_path", "repo_dir", "work_dir"].forEach((key) => {
    if (!caps[key]) {
      warnings.push(`missing ${key}`);
    }
  });
  if (!Array.isArray(caps.shared_roots) || !caps.shared_roots.length) {
    warnings.push("missing shared_roots");
  }
  if (sharedPaths.some(sharedPathReady) && !sharedPaths.some((item) => sharedPathReady(item) && item.writable)) {
    warnings.push("shared path read-only");
  }
  if (eventSpool.total > 0) {
    warnings.push(`event/log spool backlog: ${eventSpool.total}`);
  }
  if (shardUpdateSpool.total > 0) {
    warnings.push(`shard update backlog: ${shardUpdateSpool.total}`);
  }

  const level = blockers.length ? "blocked" : (warnings.length ? "warning" : "ready");
  return {
    level,
    label: level === "ready" ? "Ready" : (level === "warning" ? "Warning" : "Blocked"),
    blockers,
    warnings,
    matchedPath: eligibilityItem && eligibilityItem.matched_path ? eligibilityItem.matched_path : ""
  };
}

function workerReadinessDetail(readiness) {
  const notes = readiness.blockers.length ? readiness.blockers : readiness.warnings;
  const matched = readiness.matchedPath ? `<div class="small">matched ${escapeHtml(readiness.matchedPath)}</div>` : "";
  const noteText = notes.length ? `<div class="small">${escapeHtml(notes.slice(0, 3).join("; "))}</div>` : "";
  return `<span class="${statusClass(readiness.level)}">${escapeHtml(readiness.label)}</span>${matched}${noteText}`;
}

function workerReadinessNotes(readiness) {
  const notes = readiness.blockers.length ? readiness.blockers : readiness.warnings;
  const matched = readiness.matchedPath ? `<div class="small">matched ${escapeHtml(readiness.matchedPath)}</div>` : "";
  const noteText = notes.length ? `<div class="small">${escapeHtml(notes.slice(0, 3).join("; "))}</div>` : "";
  return `${matched}${noteText}`;
}

function workerActions(server) {
  if (server.is_stale || server.status === "offline") {
    return `<button data-id="${escapeHtml(server.id)}" class="deleteBtn removeServerBtn">Remove</button>`;
  }
  return `<div class="cell-stack">
    <button type="button" data-id="${escapeHtml(server.id)}" class="scaleWorkerBtn">Scale</button>
    <span class="muted">active</span>
  </div>`;
}

function setRawInputMode() {
  document.getElementById("inputMode").value = selectedInputMode();
}

function syncExecutionModeFields() {
  setRawInputMode();
  const existingManifest = selectedInputMode() === "existing_manifest";
  const directoryMode = selectedInputMode() === "directory";
  const wasDirectoryMode = ui.workerScope.disabled;
  document.getElementById("manifestPathField").classList.toggle("hidden", !existingManifest);
  document.getElementById("manifestRootField").classList.toggle("hidden", existingManifest);
  if (!directoryMode && wasDirectoryMode) {
    ui.workerScope.value = "all_eligible";
  }
  ui.workerScope.disabled = directoryMode;
  if (directoryMode) {
    ui.workerScope.value = "selected";
  }
  const showWorkerSelection = directoryMode || ui.workerScope.value === "selected";
  ui.selectedWorkersPanel.classList.toggle("hidden", !showWorkerSelection);
  ui.singleWorkerField.classList.toggle("hidden", !directoryMode);
  ui.selectedWorkersField.classList.toggle("hidden", directoryMode);
  ui.manualWorkersPanel.classList.toggle("hidden", !showWorkerSelection);
}

function normalizeModelProfile(profile) {
  return {
    ...profile,
    extra_args: profile && profile.extra_args && typeof profile.extra_args === "object" ? profile.extra_args : {},
    requires_api_key: Boolean(profile && profile.requires_api_key),
    has_api_key: Boolean(profile && profile.has_api_key),
    api_key_env_var: profile && profile.api_key_env_var ? String(profile.api_key_env_var) : "",
    is_default: Boolean(profile && profile.is_default)
  };
}

function renderModelProfileOptions(previousValue = "") {
  const entries = Object.entries(state.modelProfiles);
  ui.modelProfile.innerHTML = entries
    .map(([id, profile]) => `<option value="${escapeHtml(id)}">${escapeHtml(profile.label || id)}</option>`)
    .join("");
  const defaultEntry = entries.find(([, profile]) => profile.is_default);
  const nextValue = previousValue && state.modelProfiles[previousValue]
    ? previousValue
    : (defaultEntry ? defaultEntry[0] : DEFAULT_MODEL_PROFILE);
  if (state.modelProfiles[nextValue]) {
    ui.modelProfile.value = nextValue;
  }
}

async function loadModelProfiles() {
  const previousValue = ui.modelProfile.value;
  try {
    const profiles = await requestJson(MODEL_PROFILES_API);
    state.modelProfiles = Object.fromEntries(
      profiles.map((profile) => [profile.id, normalizeModelProfile(profile)])
    );
    setMessage(ui.modelProfileMessage, "");
  } catch (error) {
    state.modelProfiles = Object.fromEntries(
      Object.entries(MODEL_PROFILES).map(([id, profile]) => [id, normalizeModelProfile({ id, ...profile })])
    );
    setMessage(ui.modelProfileMessage, `Using built-in profiles: ${error.message}`, true);
  }
  renderModelProfileOptions(previousValue);
  applyModelProfile();
}

function selectedModelProfile() {
  return state.modelProfiles[ui.modelProfile.value] || null;
}

function renderResolvedModelConfig(profile) {
  if (!profile) {
    ui.resolvedModelConfig.innerHTML = "";
    return;
  }
  const items = [
    ["engine", profile.engine],
    ["ip", profile.ip],
    ["port", profile.port],
    ["model_name", profile.model_name],
    ["file_concurrency", profile.extra_args && profile.extra_args.file_concurrency],
  ];
  ui.resolvedModelConfig.innerHTML = items
    .map(([key, value]) => `<span class="config-chip"><strong>${escapeHtml(key)}</strong> ${escapeHtml(value)}</span>`)
    .join("");
}

function formatProfileKeyStatus(profile) {
  if (!profile) return "No selected profile";
  if (profile.api_key_env_var && profile.has_api_key) {
    return `API key resolved from env var ${profile.api_key_env_var}`;
  }
  if (profile.api_key_env_var) {
    return `Env var ${profile.api_key_env_var} is configured but not resolved`;
  }
  return profile.has_api_key ? "API key saved for this profile" : "No saved API key";
}

function renderModelProfileEditor(profile) {
  if (!profile) {
    ui.profileLabel.value = "";
    ui.profileEngine.value = "";
    ui.profileIp.value = "";
    ui.profilePort.value = "";
    ui.profileModelName.value = "";
    ui.profilePageConcurrency.value = "";
    ui.profileExtraArgs.value = "{}";
    ui.profileApiKey.value = "";
    ui.profileApiKeyEnvVar.value = "";
    ui.profileHasApiKey.textContent = formatProfileKeyStatus(null);
    ui.profileRequiresApiKey.checked = false;
    ui.profileClearApiKey.checked = false;
    ui.profileIsDefault.checked = false;
    return;
  }
  ui.profileLabel.value = profile.label || "";
  ui.profileEngine.value = profile.engine || "";
  ui.profileIp.value = profile.ip || "";
  ui.profilePort.value = profile.port || "";
  ui.profileModelName.value = profile.model_name || "";
  ui.profilePageConcurrency.value = profile.page_concurrency || "";
  ui.profileExtraArgs.value = JSON.stringify(profile.extra_args || {}, null, 2);
  ui.profileApiKey.value = "";
  ui.profileApiKeyEnvVar.value = profile.api_key_env_var || "";
  ui.profileHasApiKey.textContent = formatProfileKeyStatus(profile);
  ui.profileRequiresApiKey.checked = Boolean(profile.requires_api_key);
  ui.profileClearApiKey.checked = false;
  ui.profileIsDefault.checked = Boolean(profile.is_default);
}

function applyModelProfile() {
  const profile = selectedModelProfile();
  if (!profile) return;

  document.getElementById("pageConcurrency").value = profile.page_concurrency || "";
  document.getElementById("fileConcurrency").value = (profile.extra_args && profile.extra_args.file_concurrency) || "";
  document.getElementById("apiConcurrencyStart").value = (profile.extra_args && profile.extra_args.api_concurrency_start) || "";
  document.getElementById("apiConcurrencyMax").value = (profile.extra_args && profile.extra_args.api_concurrency_max) || "";
  document.getElementById("apiAutotuneInterval").value = (profile.extra_args && profile.extra_args.api_autotune_interval) || "";
  document.getElementById("enableApiAutotune").checked = Boolean(profile.extra_args && profile.extra_args.enable_api_autotune);
  document.getElementById("numCpuWorkers").value = (profile.extra_args && profile.extra_args.num_cpu_workers) || "";
  ui.apiKey.placeholder = profile.requires_api_key && !profile.has_api_key
    ? "Required unless saved in profile"
    : "Optional per-job override";
  renderResolvedModelConfig(profile);
  renderModelProfileEditor(profile);
}

async function saveModelProfile() {
  const profileId = ui.modelProfile.value;
  if (!profileId) {
    setMessage(ui.modelProfileMessage, "Please select a model profile first.", true);
    return;
  }
  let extraArgs = {};
  try {
    extraArgs = JSON.parse(ui.profileExtraArgs.value || "{}");
    if (!extraArgs || Array.isArray(extraArgs) || typeof extraArgs !== "object") {
      throw new Error("extra_args must be a JSON object");
    }
    const secretLikeKeys = Object.keys(extraArgs).filter((key) => {
      const normalized = key.toLowerCase().replace(/-/g, "_");
      return normalized === "api_key"
        || normalized === "api_key_env_var"
        || normalized === "authorization"
        || normalized === "password"
        || normalized.endsWith("_token")
        || normalized.endsWith("_secret")
        || normalized.endsWith("_password");
    });
    if (secretLikeKeys.length) {
      throw new Error(`extra_args must not contain secret-like keys (${secretLikeKeys.join(", ")}); use saved_api_key or api_key_env_var instead`);
    }
  } catch (error) {
    setMessage(ui.modelProfileMessage, `Failed to parse extra_args JSON: ${error.message}`, true);
    return;
  }
  const payload = {
    label: ui.profileLabel.value.trim() || profileId,
    engine: ui.profileEngine.value.trim(),
    ip: ui.profileIp.value.trim() || null,
    port: Number(ui.profilePort.value || 0) || null,
    model_name: ui.profileModelName.value.trim() || null,
    page_concurrency: Number(ui.profilePageConcurrency.value || 0) || null,
    extra_args: extraArgs,
    requires_api_key: ui.profileRequiresApiKey.checked,
    is_default: ui.profileIsDefault.checked,
    api_key_env_var: ui.profileApiKeyEnvVar.value.trim() || null,
    clear_api_key: ui.profileClearApiKey.checked
  };
  const apiKey = ui.profileApiKey.value.trim();
  if (apiKey) {
    payload.api_key = apiKey;
  }
  if (!payload.engine) {
    setMessage(ui.modelProfileMessage, "engine is required.", true);
    return;
  }
  const saved = await requestJson(modelProfileApiPath(profileId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.modelProfiles[profileId] = normalizeModelProfile(saved);
  renderModelProfileOptions(profileId);
  applyModelProfile();
  const keyStatus = state.modelProfiles[profileId].has_api_key
    ? "API key saved and available for jobs"
    : (payload.clear_api_key ? "API key cleared" : "No saved API key");
  setMessage(ui.modelProfileMessage, `Saved profile: ${profileId}. ${keyStatus}.`);
}

function statusClass(status) {
  return `status ${status || "queued"}`;
}

function formatNumber(value, digits = 2) {
  if (value == null) return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(digits);
}

function formatBytes(value) {
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num <= 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let scaled = num;
  let unitIndex = 0;
  while (scaled >= 1024 && unitIndex < units.length - 1) {
    scaled /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex <= 1 ? 0 : 1;
  return `${scaled.toFixed(digits)} ${units[unitIndex]}`;
}

function resourceStatus(percent) {
  const value = Number(percent);
  if (!Number.isFinite(value)) return "stopped";
  if (value >= 90) return "blocked";
  if (value >= 75) return "warning";
  return "ready";
}

function renderDatabaseStatus(payload, error = "") {
  if (error) {
    ui.databaseStatus.innerHTML = `<div class="metric"><strong>error</strong><span class="small">${escapeHtml(error)}</span></div>`;
    return;
  }
  if (!payload) {
    ui.databaseStatus.innerHTML = `<div class="metric"><strong>-</strong><span class="small">dialect</span></div>`;
    return;
  }
  const knownMigrations = Array.isArray(payload.known_migrations) ? payload.known_migrations : [];
  const appliedMigrations = Array.isArray(payload.applied_migrations) ? payload.applied_migrations : [];
  const missingMigrations = Array.isArray(payload.missing_migrations) ? payload.missing_migrations : [];
  const latest = payload.latest_applied_migration || "none";
  const tableStatus = payload.schema_migrations_table_exists ? "present" : "missing";
  const currentStatus = payload.is_current ? "current" : "stale";
  const latestClass = latest === "none" ? "warning" : "succeeded";
  const tableClass = payload.schema_migrations_table_exists ? "succeeded" : "failed";
  const currentClass = payload.is_current ? "succeeded" : "failed";
  ui.databaseStatus.innerHTML = [
    `<div class="metric"><strong>${escapeHtml(payload.dialect || "unknown")}</strong><span class="small">dialect</span></div>`,
    `<div class="metric"><strong><span class="${statusClass(latestClass)}">${escapeHtml(latest)}</span></strong><span class="small">latest_applied_migration</span></div>`,
    `<div class="metric"><strong><span class="${statusClass(currentClass)}">${escapeHtml(currentStatus)}</span></strong><span class="small">migration currency</span></div>`,
    `<div class="metric"><strong><span class="${statusClass(tableClass)}">${escapeHtml(tableStatus)}</span></strong><span class="small">schema_migrations table</span></div>`,
    `<div class="metric"><strong>${escapeHtml(appliedMigrations.length)}</strong><span class="small">applied migrations</span></div>`,
    `<div class="metric"><strong>${escapeHtml(knownMigrations.length)}</strong><span class="small">known migrations</span></div>`,
    `<div class="metric"><strong>${escapeHtml(missingMigrations.length)}</strong><span class="small">missing migrations</span></div>`,
  ].join("");
}

async function loadDatabaseStatus() {
  try {
    const payload = await requestJson(DATABASE_STATUS_API);
    state.databaseStatus = payload;
    renderDatabaseStatus(payload);
  } catch (error) {
    renderDatabaseStatus(null, String(error.message || error));
  }
}

const DEPLOYMENT_DOCTOR_ISSUES = {
  database_not_postgres: "database is not PostgreSQL",
  database_migrations_missing: "database migrations table is missing",
  database_migration_not_current: "database migrations are not current",
  api_auth_disabled: "control API auth is disabled",
  no_workers: "no workers registered",
  no_ready_workers: "no ready workers",
  resource_constrained_workers: "workers report resource pressure"
};

function renderDeploymentDoctor(payload, error = "") {
  if (error) {
    ui.deploymentDoctorStatus.innerHTML = `<div class="metric"><strong>error</strong><span class="small">${escapeHtml(error)}</span></div>`;
    renderOperationsSummary();
    return;
  }
  if (!payload) {
    ui.deploymentDoctorStatus.innerHTML = `<div class="metric"><strong>-</strong><span class="small">readiness</span></div>`;
    renderOperationsSummary();
    return;
  }
  const issues = Array.isArray(payload.issues) ? payload.issues : [];
  const workers = payload.workers || {};
  const database = payload.database || {};
  const apiAuth = payload.api_auth || {};
  const readinessText = payload.ok ? "Ready to submit UI jobs" : "Needs attention";
  const readinessClass = payload.ok ? "succeeded" : "warning";
  const issueText = issues.length
    ? issues.map((item) => DEPLOYMENT_DOCTOR_ISSUES[item.code] || item.code || item.message || "issue").slice(0, 4).join(", ")
    : "none";
  ui.deploymentDoctorStatus.innerHTML = [
    `<div class="metric"><strong><span class="${statusClass(readinessClass)}">${escapeHtml(readinessText)}</span></strong><span class="small">readiness</span></div>`,
    `<div class="metric"><strong>${escapeHtml(database.dialect || "unknown")}</strong><span class="small">database</span></div>`,
    `<div class="metric"><strong>${escapeHtml(workers.ready || 0)}/${escapeHtml(workers.total || 0)}</strong><span class="small">ready workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(workers.with_shared_roots || 0)}</strong><span class="small">shared-root workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(apiAuth.enabled ? "enabled" : "disabled")}</strong><span class="small">API auth</span></div>`,
    `<div class="metric"><strong>${escapeHtml(issues.length)}</strong><span class="small">${escapeHtml(issueText)}</span></div>`,
  ].join("");
  renderOperationsSummary();
}

async function loadDeploymentDoctor() {
  try {
    const payload = await requestJson(DIAGNOSTICS_API);
    state.deploymentDoctor = payload;
    renderDeploymentDoctor(payload);
  } catch (error) {
    state.deploymentDoctor = null;
    renderDeploymentDoctor(null, String(error.message || error));
  }
}

function resourcePercent(value) {
  const num = Number(value);
  return Number.isFinite(num) ? `${formatNumber(num, 1)}%` : "-";
}

function rowProgress(job) {
  const fileText = `${job.completed_files || 0}/${job.total_files || 0} files`;
  const completedPages = Number(job.completed_pages || 0);
  const pageText = job.total_pages == null
    ? `${completedPages} ${completedPages === 1 ? "page" : "pages"} done`
    : `${completedPages}/${job.total_pages} pages`;
  const percent = Number(job.progress_percent || 0);
  const width = Math.max(0, Math.min(percent, 100));
  const pct = job.progress_percent == null ? "" : `${formatNumber(job.progress_percent)}%`;
  return `<div class="cell-stack">
    <div class="cell-row"><span class="primary-text">${escapeHtml(fileText)}</span><span class="small">${escapeHtml(pct)}</span></div>
    <div class="small">${escapeHtml(pageText)}</div>
    <div class="progress-track"><div class="progress-fill" style="width: ${escapeHtml(width)}%"></div></div>
  </div>`;
}

function rowShards(job) {
  const scanUnits = Number(job.total_scan_units || 0);
  const scanLine = scanUnits
    ? `<div class="small">scan ${escapeHtml(`${job.succeeded_scan_units || 0}/${scanUnits} done; pending ${job.pending_scan_units || 0}; running ${job.running_scan_units || 0}; stale ${job.stale_scan_units || 0}`)}</div>`
    : "";
  if (!job.total_shards) return scanLine || "-";
  const done = `${job.succeeded_shards || 0}/${job.total_shards} shards done`;
  const counts = [
    `pending ${job.pending_shards || 0}`,
    `running ${job.running_shards || 0}`,
    `retrying ${job.retrying_shards || 0}`,
    `stale ${job.stale_shards || 0}`,
    `failed ${job.failed_shards || 0}`,
    `stopped ${job.stopped_shards || 0}`,
  ].join("; ");
  const recovery = job.recovery_status
    ? `<div><span class="${statusClass(job.recovery_status)}">${escapeHtml(job.recovery_status)}</span></div>`
    : "";
  return `${escapeHtml(done)}${recovery}<div class="small">${escapeHtml(counts)}</div>${scanLine}`;
}

function rowWorkerSummary(job) {
  const workers = (job.worker_shards || []).filter((item) => item.server_id);
  if (!workers.length) {
    const selectedCount = Array.isArray(job.allowed_server_ids) ? job.allowed_server_ids.length : 0;
    const label = job.assigned_server_id
      ? job.assigned_server_id
      : (selectedCount ? `${selectedCount} selected` : "server pool");
    return `<div class="cell-stack">
      <div class="primary-text">${escapeHtml(label)}</div>
      <div class="small">no shards generated</div>
    </div>`;
  }
  const active = workers.filter((item) => Number(item.running_shards || 0) > 0).length;
  const stale = workers.reduce((sum, item) => sum + Number(item.stale_shards || 0), 0);
  const failed = workers.reduce((sum, item) => sum + Number(item.failed_shards || 0), 0);
  const workerLabel = workers.length === 1 ? "worker" : "workers";
  return `<div class="cell-stack">
    <div class="primary-text">${escapeHtml(workers.length)} ${workerLabel}</div>
    <div class="small">${escapeHtml(active)} active; ${escapeHtml(stale)} stale; ${escapeHtml(failed)} failed</div>
  </div>`;
}

function rowThroughput(job) {
  const pages = job.pages_per_second == null ? "-" : `${formatNumber(job.pages_per_second, 3)} page/s`;
  const files = job.files_per_minute == null ? "-" : `${formatNumber(job.files_per_minute, 2)} file/min`;
  const eta = job.eta_seconds == null ? "" : `<div class="small">ETA ${formatDuration(job.eta_seconds)}</div>`;
  return `${escapeHtml(pages)}<div class="small">${escapeHtml(files)}</div>${eta}`;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "-";
  if (value < 60) return `${Math.round(value)}s`;
  const minutes = Math.round(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  return `${hours}h`;
}

function timestampAgeSeconds(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return Math.max(0, (Date.now() - date.getTime()) / 1000);
}

function shardRunningSeconds(shard) {
  const explicit = Number(shard.running_seconds);
  if (Number.isFinite(explicit) && explicit >= 0) return explicit;
  return timestampAgeSeconds(shard.started_at);
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function formatFailureCategoryCounts(label, counts) {
  if (!counts || typeof counts !== "object") return "";
  const entries = Object.entries(counts)
    .filter(([_category, count]) => Number(count) > 0)
    .sort((left, right) => Number(right[1]) - Number(left[1]) || String(left[0]).localeCompare(String(right[0])));
  if (!entries.length) return "";
  const rendered = entries
    .slice(0, 4)
    .map(([category, count]) => `<span class="status failed">${escapeHtml(category)} ${escapeHtml(count)}</span>`)
    .join("");
  const suffix = entries.length > 4 ? `<span class="small">+${escapeHtml(entries.length - 4)} more</span>` : "";
  return `<div class="small">${escapeHtml(label)}</div><div class="cell-row">${rendered}${suffix}</div>`;
}

function formatFreshness(job) {
  const eventAt = job.last_heartbeat_at || job.last_event_at;
  const stale = job.is_stale ? `<span class="status stale">stale</span>` : "";
  if (!eventAt) {
    return `${stale}<div class="small">No events yet</div>`;
  }
  const date = new Date(eventAt);
  return `${stale}<div class="small">${escapeHtml(date.toLocaleString())}</div>`;
}

function deploymentDoctorJobIssues() {
  const payload = state.deploymentDoctor || {};
  const issues = Array.isArray(payload.issues) ? payload.issues : [];
  const productionCodes = new Set([
    "database_not_postgres",
    "database_migrations_missing",
    "database_migration_not_current",
    "api_auth_disabled",
    "no_workers",
    "no_ready_workers"
  ]);
  return issues.filter((issue) => productionCodes.has(issue.code));
}

function jobRuntimeSignals(job) {
  const shards = [];
  if (Array.isArray(job.worker_shards)) {
    job.worker_shards.forEach((worker) => {
      if (Array.isArray(worker.current_shards)) shards.push(...worker.current_shards);
    });
  }
  if (Array.isArray(job.attention_shards)) shards.push(...job.attention_shards);
  const unique = new Map();
  shards.forEach((shard) => {
    if (shard && shard.id != null) unique.set(shard.id, shard);
  });
  const current = Array.from(unique.values());
  return {
    current,
    api_inflight: current.reduce((sum, shard) => sum + Number(shard.api_inflight || 0), 0),
    api_waiting: current.reduce((sum, shard) => sum + Number(shard.api_waiting || 0), 0),
    oldest_api_inflight: current.reduce((max, shard) => Math.max(max, Number(shard.oldest_api_inflight || 0)), 0),
  };
}

function firstAttentionShard(job) {
  const shards = Array.isArray(job.attention_shards) ? job.attention_shards : [];
  return shards.find((shard) => ["running", "retrying", "stale", "failed"].includes(shard.status)) || shards[0] || null;
}

function firstStartupStalledShard(runtime) {
  return runtime.current.find((shard) => {
    if (!["running", "retrying"].includes(shard.status)) return false;
    const runningSeconds = shardRunningSeconds(shard);
    return runningSeconds != null
      && runningSeconds >= 120
      && Number(shard.processed_files || 0) === 0
      && Number(shard.completed_pages || 0) === 0
      && Number(shard.api_inflight || 0) === 0
      && Number(shard.api_waiting || 0) === 0;
  }) || null;
}

function formatWorkPiece(shard) {
  if (!shard) return "";
  const parts = [];
  if (shard.shard_index != null) parts.push(`work piece ${shard.shard_index}`);
  if (shard.assigned_server_id) parts.push(`on ${shard.assigned_server_id}`);
  return parts.length ? ` (${parts.join(" ")})` : "";
}

function formatJobTechnicalState(job) {
  const parts = [`job ${job.status || "queued"}`];
  if (job.lifecycle_stage) parts.push(`stage ${job.lifecycle_stage}`);
  if (job.scan_status && job.scan_status !== "not_started") parts.push(`scan ${job.scan_status}`);
  if (job.recovery_status && job.recovery_status !== "healthy") parts.push(`recovery ${job.recovery_status}`);
  if (job.is_stale) parts.push("heartbeat late");
  return parts.join(" · ");
}

function explainJobStatus(job) {
  const terminal = ["succeeded", "failed", "stopped"].includes(job.status);
  const runtime = jobRuntimeSignals(job);
  const productionIssues = deploymentDoctorJobIssues();
  const active = ["queued", "running", "stopping"].includes(job.status);
  if (active && productionIssues.length) {
    const labels = productionIssues
      .slice(0, 3)
      .map((issue) => DEPLOYMENT_DOCTOR_ISSUES[issue.code] || issue.code || "deployment issue");
    return {
      level: "warning",
      label: "Setup needs attention",
      message: labels.join("; "),
      next: "Check Deployment Doctor.",
    };
  }
  if (job.status === "failed" || Number(job.failed_shards || 0) > 0 || Number(job.failed_scan_units || 0) > 0) {
    const shard = firstAttentionShard(job);
    const target = formatWorkPiece(shard);
    return {
      level: "failed",
      label: "Needs attention",
      message: `${job.failure_category || "Error found"}${target}.`,
      next: "Open Recent errors.",
    };
  }
  if (job.status === "stopping") {
    return {
      level: "stopping",
      label: "Stopping",
      message: "Stop requested.",
      next: "Watch active work.",
    };
  }
  if (terminal) {
    return {
      level: job.status,
      label: job.status === "succeeded" ? "Completed" : "Stopped",
      message: `${job.completed_files || 0}/${job.total_files || 0} files.`,
      next: job.status === "succeeded" ? "" : "Review logs if unexpected.",
    };
  }
  if (job.is_stale) {
    return {
      level: "stale",
      label: "No recent update",
      message: "Worker heartbeat is late.",
      next: "Check worker health.",
    };
  }
  if (Number(job.stale_shards || 0) > 0 || Number(job.retrying_shards || 0) > 0) {
    const shard = firstAttentionShard(job);
    const target = formatWorkPiece(shard);
    return {
      level: "recovering",
      label: "Recovering work",
      message: `${job.retrying_shards || 0} retrying · ${job.stale_shards || 0} stale${target}.`,
      next: "Open Attention work.",
    };
  }
  if (runtime.oldest_api_inflight >= 300 || (runtime.api_waiting > 0 && runtime.oldest_api_inflight >= 120)) {
    const shard = firstAttentionShard(job);
    const target = formatWorkPiece(shard);
    return {
      level: "warning",
      label: "Model API may be stuck",
      message: `${formatNumber(runtime.oldest_api_inflight, 1)}s wait · ${runtime.api_waiting} queued${target}.`,
      next: "Check model API capacity.",
    };
  }
  const startupStalledShard = firstStartupStalledShard(runtime);
  if (startupStalledShard) {
    const runningFor = formatDuration(shardRunningSeconds(startupStalledShard));
    const target = formatWorkPiece(startupStalledShard);
    return {
      level: "warning",
      label: "Worker has not started real work",
      message: `No progress for ${runningFor}${target}.`,
      next: "Open logs.",
    };
  }
  if (
    Number(job.total_shards || 0) > 0
    && Number(job.running_shards || 0) > 0
    && Number(job.pending_shards || 0) === 0
    && Number(job.succeeded_shards || 0) > 0
    && Number(job.running_shards || 0) <= Math.max(1, Math.ceil(Number(job.total_shards || 0) * 0.1))
  ) {
    const shard = firstAttentionShard(job);
    const target = formatWorkPiece(shard);
    return {
      level: "warning",
      label: "Almost done",
      message: `${job.running_shards || 0} left${target}.`,
      next: "",
    };
  }
  if (active && Number(job.running_shards || 0) === 0 && Number(job.pending_shards || 0) > 0) {
    return {
      level: "queued",
      label: "Waiting for worker",
      message: `${job.pending_shards || 0} pieces waiting.`,
      next: "Check workers.",
    };
  }
  if (active && Number(job.total_shards || 0) === 0 && Number(job.total_scan_units || 0) === 0 && !job.last_heartbeat_at) {
    return {
      level: "queued",
      label: "Waiting for first update",
      message: "Submitted; no worker update yet.",
      next: "Check workers.",
    };
  }
  if (
    active
    && (job.lifecycle_stage === "scanning" || job.scan_status === "running")
    && Number(job.pending_scan_units || 0) > 0
    && Number(job.running_scan_units || 0) === 0
  ) {
    return {
      level: "queued",
      label: "Waiting for scan worker",
      message: `${job.pending_scan_units || 0} scan tasks waiting.`,
      next: "Check workers.",
    };
  }
  if (job.lifecycle_stage === "scanning" || job.scan_status === "running") {
    return {
      level: "running",
      label: "Finding PDFs",
      message: `${job.scan_progress_files || 0} PDFs found.`,
      next: "",
    };
  }
  if (Number(job.running_shards || 0) > 0) {
    return {
      level: "running",
      label: "Processing PDFs",
      message: `${job.running_shards || 0} running · ${runtime.api_inflight} API.`,
      next: "",
    };
  }
  return {
    level: "queued",
    label: "Waiting to start",
    message: "Queued.",
    next: "",
  };
}

function formatJobStatusExplanation(job) {
  const explanation = explainJobStatus(job);
  const next = explanation.next
    ? `<div class="next-action">${escapeHtml(explanation.next)}</div>`
    : "";
  return `<div class="status-explanation diagnosis small">
    <div>${escapeHtml(explanation.message)}</div>
    ${next}
  </div>`;
}

function formatJobStatus(job) {
  const explanation = explainJobStatus(job);
  const hasHealthIssue = Boolean(
    job.failure_category ||
    job.error_message ||
    (job.degraded_pages || 0) > 0 ||
    (job.quality_flags && job.quality_flags.length) ||
    Object.keys(job.failure_category_counts || {}).length ||
    Object.keys(job.shard_failure_category_counts || {}).length ||
    Object.keys(job.scan_unit_failure_category_counts || {}).length
  );
  return `<div class="cell-stack" title="Technical state: ${escapeHtml(formatJobTechnicalState(job))}">
    <div class="cell-row">
      <span class="${statusClass(explanation.level)}">${escapeHtml(explanation.label)}</span>
    </div>
    ${formatJobStatusExplanation(job)}
    ${hasHealthIssue ? formatHealth(job) : ""}
  </div>`;
}

function formatJobIdentity(job) {
  const expanded = state.openJobDetails.has(job.id);
  return `<div class="cell-stack">
    <div class="primary-text mono" title="${escapeHtml(job.id)}">${escapeHtml(job.id.slice(0, 8))}...</div>
    <div class="small">${escapeHtml(job.engine)}</div>
    <button type="button" class="link-button jobDetailsToggle" data-id="${escapeHtml(job.id)}" aria-expanded="${expanded ? "true" : "false"}">
      ${expanded ? "▼" : "▶"} details
    </button>
  </div>`;
}

function formatWorkerAllocation(job) {
  const workers = (job.worker_shards || []).filter((item) => item.server_id);
  if (!workers.length) {
    const selected = Array.isArray(job.allowed_server_ids) ? job.allowed_server_ids : [];
    const selectedText = selected.length
      ? `${selected.length} selected: ${selected.join(", ")}`
      : (job.assigned_server_id || "server pool");
    return `<div class="small">${escapeHtml(selectedText)}</div><div class="small">No shard ownership reported yet.</div>`;
  }
  return `<div class="worker-detail-list scroll-detail-list">${
    workers
      .map((item) => {
        const done = `${item.succeeded_shards || 0}/${item.total_shards || 0} shards done`;
        const detail = [
          `pending ${item.pending_shards || 0}`,
          `running ${item.running_shards || 0}`,
          `retrying ${item.retrying_shards || 0}`,
          `stale ${item.stale_shards || 0}`,
          `failed ${item.failed_shards || 0}`,
        ].join("; ");
        const current = Array.isArray(item.current_shards) && item.current_shards.length
          ? item.current_shards.map(formatCurrentShard).join("")
          : `<div class="small">No active shard on this worker.</div>`;
        const api = `api ${item.api_inflight || 0}/${item.api_inflight_peak || 0} peak; wait ${item.api_waiting || 0}; oldest ${formatNumber(item.oldest_api_inflight || 0, 1)}s`;
        return `<div class="worker-detail-item">
          <div class="primary-text path-full">${escapeHtml(item.server_id)}</div>
          <div>${escapeHtml(done)}</div>
          <div class="small">${escapeHtml(detail)}</div>
          <div class="small">${escapeHtml(api)}</div>
          <div class="detail-section">${current}</div>
        </div>`;
      })
      .join("")
  }</div>`;
}

function formatJobOverview(job) {
  const processedFiles = Number(job.completed_files || 0) + Number(job.failed_files || 0) + Number(job.skipped_files || 0);
  const shardDone = `${job.succeeded_shards || 0}/${job.total_shards || 0}`;
  const scanTotal = Number(job.total_scan_units || 0);
  const scanDone = scanTotal ? `${job.succeeded_scan_units || 0}/${scanTotal}` : "-";
  const shardCounts = [
    `running ${job.running_shards || 0}`,
    `succeeded ${job.succeeded_shards || 0}`,
    `failed ${job.failed_shards || 0}`,
    `stale ${job.stale_shards || 0}`,
    `retrying ${job.retrying_shards || 0}`,
  ].join("; ");
  return `<div class="metric-pairs">
      <div><div class="detail-label">Total files</div><div class="primary-text">${escapeHtml(job.total_files || 0)}</div></div>
      <div><div class="detail-label">Scanned files</div><div class="primary-text">${escapeHtml(job.scanned_files || 0)}</div></div>
      <div><div class="detail-label">Processed files</div><div class="primary-text">${escapeHtml(processedFiles)}</div></div>
      <div><div class="detail-label">Shards created</div><div class="primary-text">${escapeHtml(job.shards_created || 0)}</div></div>
      <div><div class="detail-label">Total shards</div><div class="primary-text">${escapeHtml(job.total_shards || 0)}</div></div>
      <div><div class="detail-label">Shard done</div><div class="primary-text">${escapeHtml(shardDone)}</div></div>
    </div>
    <div class="small">${escapeHtml(shardCounts)}</div>
    <div class="small">scan units ${escapeHtml(scanDone)}</div>
    ${formatJobVersionWarning(job)}`;
}

function formatScanLifecycle(job) {
  const status = job.scan_status || "not_started";
  const files = job.scan_discovered_pdf_count ?? job.scan_progress_files ?? job.scanned_files ?? 0;
  const estimatedFiles = job.scan_estimated_total_pdf_count ?? job.scan_estimated_total_files ?? "-";
  const remainingFiles = job.scan_remaining_pdf_count ?? job.scan_remaining_files ?? "-";
  const scanPercent = job.scan_progress_percent == null ? "-" : `${formatNumber(job.scan_progress_percent)}%`;
  const dirs = job.scan_progress_dirs || 0;
  const bytes = formatBytes(job.scan_progress_bytes || 0);
  const eta = job.scan_eta_seconds == null ? "-" : formatDuration(job.scan_eta_seconds);
  const startedAt = formatDateTime(job.scan_started_at);
  const finishedAt = formatDateTime(job.scan_finished_at);
  const samples = Array.isArray(job.scan_error_samples) ? job.scan_error_samples : [];
  const sampleText = samples.length
    ? samples.slice(0, 3).map((item) => `${item.path || "-"}: ${item.failure_category || "scan_error"}: ${item.reason || "-"}`).join(" | ")
    : "no sampled scan errors";
  return `<div class="detail-section">
    <div class="cell-row">
      <span class="${statusClass(status === "done" ? "succeeded" : status)}">${escapeHtml(status)}</span>
      <span class="small">Scanning lifecycle</span>
    </div>
    <div class="metric-pairs">
      <div><div class="detail-label">Found PDFs</div><div class="primary-text">${escapeHtml(files)}</div></div>
      <div><div class="detail-label">Estimated PDFs</div><div class="primary-text">${escapeHtml(estimatedFiles)}</div></div>
      <div><div class="detail-label">Remaining PDFs</div><div class="primary-text">${escapeHtml(remainingFiles)}</div></div>
      <div><div class="detail-label">Scan progress</div><div>${escapeHtml(scanPercent)}</div></div>
      <div><div class="detail-label">Scanned dirs</div><div class="primary-text">${escapeHtml(dirs)}</div></div>
      <div><div class="detail-label">Scanned bytes</div><div>${escapeHtml(bytes)}</div></div>
      <div><div class="detail-label">ETA</div><div>${escapeHtml(eta)}</div></div>
      <div><div class="detail-label">Started</div><div>${escapeHtml(startedAt)}</div></div>
      <div><div class="detail-label">Finished</div><div>${escapeHtml(finishedAt)}</div></div>
    </div>
    <div class="small path-full">current ${escapeHtml(job.scan_current_path || "-")}</div>
    <div class="small path-full">${escapeHtml(job.scan_error_count || 0)} scan errors sampled: ${escapeHtml(sampleText)}</div>
  </div>`;
}

function formatJobLifecycle(job) {
  const stage = job.lifecycle_stage || job.status || "queued";
  return `<div class="detail-section">
    <div class="cell-row">
      <span class="${statusClass(stage)}">${escapeHtml(stage)}</span>
      <span class="small">Lifecycle</span>
    </div>
    <div class="small">scan ${escapeHtml(job.scan_status || "not_started")}; recovery ${escapeHtml(job.recovery_status || "healthy")}</div>
  </div>`;
}

function formatWorkPlan(job) {
  const shardDone = `${job.succeeded_shards || 0}/${job.total_shards || 0}`;
  const scanTotal = Number(job.total_scan_units || 0);
  const scanDone = scanTotal ? `${job.succeeded_scan_units || 0}/${scanTotal}` : "-";
  const shardCounts = [
    `pending ${job.pending_shards || 0}`,
    `running ${job.running_shards || 0}`,
    `retrying ${job.retrying_shards || 0}`,
    `stale ${job.stale_shards || 0}`,
    `failed ${job.failed_shards || 0}`,
    `stopped ${job.stopped_shards || 0}`,
  ].join("; ");
  const scanCounts = scanTotal
    ? [`pending ${job.pending_scan_units || 0}`, `running ${job.running_scan_units || 0}`, `stale ${job.stale_scan_units || 0}`].join("; ")
    : "no distributed scan units";
  const snapshotStatus = job.manifest_snapshot_status || "missing";
  const manifestStatus = job.manifest_status || "missing";
  const frozenAt = job.manifest_frozen_at
    ? new Date(job.manifest_frozen_at).toLocaleString()
    : "-";
  const integrityStatus = job.manifest_integrity_status || "-";
  const integrityIssueCount = Number(job.manifest_integrity_issue_count || 0);
  const integrityClass = job.manifest_integrity_ok === true
    ? "succeeded"
    : (job.manifest_integrity_ok === false ? "failed" : "pending");
  return `<div class="metric-pairs">
      <div><div class="detail-label">Shards</div><div class="primary-text">${escapeHtml(shardDone)}</div></div>
      <div><div class="detail-label">Generated shards</div><div class="primary-text">${escapeHtml(job.shards_created || 0)}</div></div>
      <div><div class="detail-label">Executable shards</div><div class="primary-text">${escapeHtml(job.executable_shards || 0)}</div></div>
      <div><div class="detail-label">Scan units</div><div class="primary-text">${escapeHtml(scanDone)}</div></div>
      <div><div class="detail-label">Snapshot</div><div><span class="${statusClass(snapshotStatus === "frozen" ? "succeeded" : snapshotStatus)}">${escapeHtml(snapshotStatus)}</span></div></div>
      <div><div class="detail-label">Frozen at</div><div>${escapeHtml(frozenAt)}</div></div>
    </div>
    <div class="small">manifest ${escapeHtml(manifestStatus)}</div>
    <div class="small">Freeze integrity <span class="${statusClass(integrityClass)}">${escapeHtml(integrityStatus)}</span>; issues ${escapeHtml(integrityIssueCount)}</div>
    <div class="small">${escapeHtml(shardCounts)}</div>
    <div class="small">${escapeHtml(scanCounts)}</div>`;
}

function leaseStatusClass(status) {
  if (status === "healthy") return "healthy";
  if (status === "expiring") return "expiring";
  if (status === "stale") return "stale";
  if (status === "expired" || status === "missing") return "failed";
  return "stopped";
}

function formatCurrentShard(shard) {
  const processed = `${shard.processed_files || 0}/${shard.file_count || 0} files`;
  const pages = `${shard.completed_pages || 0} pages`;
  const speed = shard.pages_per_second == null
    ? "-"
    : `${formatNumber(shard.pages_per_second, 3)} page/s`;
  const fileSpeed = shard.files_per_minute == null
    ? "-"
    : `${formatNumber(shard.files_per_minute, 2)} file/min`;
  const lease = shard.lease_seconds_remaining == null
    ? shard.lease_status || "none"
    : `${shard.lease_status || "none"} ${shard.lease_seconds_remaining}s`;
  const attempts = shard.max_attempts
    ? `${shard.attempt_count || 0}/${shard.max_attempts}`
    : `${shard.attempt_count || 0}`;
  const apiInflight = shard.api_inflight == null
    ? "-"
    : `${formatNumber(shard.api_inflight, 0)} / ${formatNumber(shard.api_inflight_peak || 0, 0)} peak`;
  const apiWaiting = shard.api_waiting == null ? "-" : formatNumber(shard.api_waiting, 0);
  const oldestInflight = shard.oldest_api_inflight
    ? `${formatNumber(shard.oldest_api_inflight, 1)}s`
    : "-";
  const execution = shard.execution_paused
    ? `paused; api limit ${shard.api_concurrency_limit || "-"}`
    : `running; api limit ${shard.api_concurrency_limit || "-"}`;
  const problem = [shard.failure_category, shard.error_message].filter(Boolean).join(": ");
  return `<div class="worker-detail-item">
    <div class="cell-row">
      <span class="${statusClass(shard.status)}">${escapeHtml(shard.status)}</span>
      <span class="primary-text mono">#${escapeHtml(shard.shard_index)}</span>
    </div>
    <div class="small">id ${escapeHtml(shard.id)}</div>
    <div class="metric-pairs">
      <div><div class="detail-label">Files</div><div>${escapeHtml(processed)}</div></div>
      <div><div class="detail-label">Pages</div><div>${escapeHtml(pages)}</div></div>
      <div><div class="detail-label">Speed</div><div>${escapeHtml(speed)}</div><div class="small">${escapeHtml(fileSpeed)}</div></div>
      <div><div class="detail-label">Attempt</div><div>${escapeHtml(attempts)}</div></div>
      <div><div class="detail-label">API inflight</div><div>${escapeHtml(apiInflight)}</div></div>
      <div><div class="detail-label">API wait</div><div>${escapeHtml(apiWaiting)}</div></div>
      <div><div class="detail-label">Oldest inflight</div><div>${escapeHtml(oldestInflight)}</div></div>
      <div><div class="detail-label">Execution</div><div>${escapeHtml(execution)}</div><div class="small">${escapeHtml(shard.execution_control_reason || "-")}</div></div>
    </div>
    <div class="cell-row">
      <span class="${statusClass(leaseStatusClass(shard.lease_status))}">${escapeHtml(lease)}</span>
    </div>
    ${problem ? `<div class="small path-full">${escapeHtml(problem)}</div>` : ""}
  </div>`;
}

function formatAttentionShards(job) {
  const shards = Array.isArray(job.attention_shards) ? job.attention_shards : [];
  const totalAttention = Number(job.running_shards || 0)
    + Number(job.retrying_shards || 0)
    + Number(job.stale_shards || 0)
    + Number(job.failed_shards || 0);
  const sampleText = totalAttention > shards.length
    ? `Showing first ${shards.length} of ${totalAttention} attention shards. Open the shard inspector for the full filtered list.`
    : `Showing ${shards.length} of ${totalAttention} attention shards.`;
  if (!shards.length) {
    return `<div class="small">Only active, stale, retrying, and failed shards are shown. ${escapeHtml(sampleText)} None need attention right now.</div>`;
  }
  return `<div class="small">Only active, stale, retrying, and failed shards are shown. ${escapeHtml(sampleText)}</div>
    <div class="worker-detail-list scroll-detail-list">${
      shards.map(formatCurrentShard).join("")
    }</div>`;
}

function shardInspectorState(jobId) {
  if (!state.shardInspectors.has(jobId)) {
    state.shardInspectors.set(jobId, {
      open: false,
      status: "attention",
      workerId: "",
      failureCategory: "",
      minAttemptCount: "",
      runningLongerThanSeconds: "",
      limit: 100,
      offset: 0,
      loading: false,
      error: "",
      payload: null
    });
  }
  return state.shardInspectors.get(jobId);
}

function formatShardInspector(job) {
  const inspector = shardInspectorState(job.id);
  const statusOptions = ["attention", "running", "retrying", "stale", "failed", "pending", "succeeded", "stopped", "all"]
    .map((value) => `<option value="${escapeHtml(value)}"${inspector.status === value ? " selected" : ""}>${escapeHtml(value)}</option>`)
    .join("");
  if (!inspector.open) {
    return `<button type="button" class="link-button inspectShardsBtn" data-id="${escapeHtml(job.id)}">Inspect shards</button>
      <div class="small">Loads shard rows only when opened. Attention mode excludes succeeded shards.</div>`;
  }
  const payload = inspector.payload || {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const total = Number(payload.total || 0);
  const offset = Number(payload.offset || inspector.offset || 0);
  const limit = Number(payload.limit || inspector.limit || 100);
  const rangeStart = total ? offset + 1 : 0;
  const rangeEnd = Math.min(offset + items.length, total);
  const rows = items.length
    ? items.map(formatShardTableRow).join("")
    : `<tr><td colspan="9" class="muted">No shards match this filter</td></tr>`;
  const previousDisabled = offset <= 0 ? " disabled" : "";
  const nextDisabled = !payload.has_more ? " disabled" : "";
  const message = inspector.loading
    ? `<div class="small">Loading shards...</div>`
    : (inspector.error ? `<div class="small error">${escapeHtml(inspector.error)}</div>` : "");
  return `<div class="shard-inspector">
    <div class="shard-inspector-tools">
      <label class="small">status
        <select class="shardStatusSelect" data-id="${escapeHtml(job.id)}">${statusOptions}</select>
      </label>
      <label class="small">worker
        <input class="shardWorkerFilter" data-id="${escapeHtml(job.id)}" placeholder="worker id" value="${escapeHtml(inspector.workerId || "")}" />
      </label>
      <label class="small">failure
        <input class="shardFailureCategoryFilter" data-id="${escapeHtml(job.id)}" placeholder="failure_category" value="${escapeHtml(inspector.failureCategory || "")}" />
      </label>
      <label class="small">min attempts
        <input class="shardMinAttemptsFilter" data-id="${escapeHtml(job.id)}" type="number" min="0" placeholder="0" value="${escapeHtml(inspector.minAttemptCount || "")}" />
      </label>
      <label class="small">running over sec
        <input class="shardRunningLongerFilter" data-id="${escapeHtml(job.id)}" type="number" min="1" placeholder="3600" value="${escapeHtml(inspector.runningLongerThanSeconds || "")}" />
      </label>
      <button type="button" class="link-button refreshShardsBtn" data-id="${escapeHtml(job.id)}">Refresh shards</button>
      <button type="button" class="link-button closeShardsBtn" data-id="${escapeHtml(job.id)}">Close</button>
      <span class="small">${escapeHtml(rangeStart)}-${escapeHtml(rangeEnd)} of ${escapeHtml(total)}</span>
    </div>
    ${message}
    <div class="shard-table-wrap">
      <table class="shard-table">
        <thead><tr>
          <th>#</th><th>Status</th><th>Worker</th><th>Files</th><th>Pages</th><th>API</th><th>Lease</th><th>Attempt</th><th>Problem</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="shard-inspector-tools">
      <button type="button" class="link-button shardPageBtn" data-id="${escapeHtml(job.id)}" data-direction="prev"${previousDisabled}>Previous</button>
      <button type="button" class="link-button shardPageBtn" data-id="${escapeHtml(job.id)}" data-direction="next"${nextDisabled}>Next</button>
    </div>
  </div>`;
}

function formatJobLogs(job) {
  const stateItem = state.jobLogs.get(job.id);
  if (!stateItem || !stateItem.open) {
    return `<button type="button" class="link-button showJobLogsBtn" data-id="${escapeHtml(job.id)}">Job logs</button>
      <div class="small">Loads bounded recent stdout/stderr rows only when opened.</div>`;
  }
  if (stateItem.loading) {
    return `<div class="small">Loading job logs...</div>`;
  }
  if (stateItem.error) {
    return `<div class="small error">${escapeHtml(stateItem.error)}</div>`;
  }
  const payload = stateItem.payload || {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const total = Number(payload.total || 0);
  const offset = Number(payload.offset || stateItem.offset || 0);
  const limit = Number(payload.limit || stateItem.limit || 100);
  const rangeStart = total ? offset + 1 : 0;
  const rangeEnd = Math.min(offset + items.length, total);
  const previousDisabled = offset <= 0 ? " disabled" : "";
  const nextDisabled = !payload.has_more ? " disabled" : "";
  const rows = items.length
    ? items.map((item) => `<tr>
        <td class="small">${escapeHtml(formatDateTime(item.created_at))}</td>
        <td>${escapeHtml(item.server_id || "-")}</td>
        <td><span class="${statusClass(item.stream === "stderr" ? "failed" : "running")}">${escapeHtml(item.stream || "-")}</span></td>
        <td class="path-full">${escapeHtml(item.line || "")}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" class="muted">No log rows retained for this job</td></tr>`;
  return `<div class="small">Log page ${escapeHtml(rangeStart)}-${escapeHtml(rangeEnd)} of ${escapeHtml(total)}</div>
    <div class="shard-table-wrap">
      <table class="shard-table">
        <thead><tr><th>Time</th><th>Worker</th><th>Stream</th><th>Line</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="shard-inspector-tools">
      <button type="button" class="link-button logPageBtn" data-id="${escapeHtml(job.id)}" data-direction="prev" data-limit="${escapeHtml(limit)}" data-offset="${escapeHtml(offset)}"${previousDisabled}>Previous logs</button>
      <button type="button" class="link-button logPageBtn" data-id="${escapeHtml(job.id)}" data-direction="next" data-limit="${escapeHtml(limit)}" data-offset="${escapeHtml(offset)}"${nextDisabled}>Next logs</button>
    </div>`;
}

function formatRecentErrors(job) {
  const stateItem = state.recentErrors.get(job.id);
  if (!stateItem || !stateItem.open) {
    return `<button type="button" class="link-button showRecentErrorsBtn" data-id="${escapeHtml(job.id)}">Recent errors</button>
      <div class="small">Loads bounded recent failure events and retained failed-file samples.</div>`;
  }
  if (stateItem.loading) {
    return `<div class="small">Loading recent errors...</div>`;
  }
  if (stateItem.error) {
    return `<div class="small error">${escapeHtml(stateItem.error)}</div>`;
  }
  const payload = stateItem.payload || {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const total = Number(payload.total || 0);
  const offset = Number(payload.offset || stateItem.offset || 0);
  const limit = Number(payload.limit || stateItem.limit || 100);
  const rangeStart = total ? offset + 1 : 0;
  const rangeEnd = Math.min(offset + items.length, total);
  const previousDisabled = offset <= 0 ? " disabled" : "";
  const nextDisabled = !payload.has_more ? " disabled" : "";
  const rows = items.length
    ? items.map((item) => `<tr>
        <td><span class="${statusClass("failed")}">${escapeHtml(item.failure_category || "unknown")}</span><div class="small">${escapeHtml(item.source || "-")}</div></td>
        <td>${escapeHtml(item.event_type || "-")}</td>
        <td class="path-full">${escapeHtml(item.file_path || item.filename || "-")}</td>
        <td class="path-full">${escapeHtml(item.error || "-")}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" class="muted">No retained recent errors for this job</td></tr>`;
  return `<div class="small">Recent error page ${escapeHtml(rangeStart)}-${escapeHtml(rangeEnd)} of ${escapeHtml(total)}</div>
    <div class="shard-table-wrap">
      <table class="shard-table">
        <thead><tr><th>Category</th><th>Event</th><th>File</th><th>Error</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="shard-inspector-tools">
      <button type="button" class="link-button recentErrorPageBtn" data-id="${escapeHtml(job.id)}" data-direction="prev" data-limit="${escapeHtml(limit)}" data-offset="${escapeHtml(offset)}"${previousDisabled}>Previous errors</button>
      <button type="button" class="link-button recentErrorPageBtn" data-id="${escapeHtml(job.id)}" data-direction="next" data-limit="${escapeHtml(limit)}" data-offset="${escapeHtml(offset)}"${nextDisabled}>Next errors</button>
    </div>`;
}

function shardAttemptKey(jobId, shardId) {
  return `${jobId}:${shardId}`;
}

function formatShardAttempts(jobId, shard) {
  const key = shardAttemptKey(jobId, shard.id);
  const stateItem = state.shardAttempts.get(key);
  if (!stateItem || !stateItem.open) {
    return `<button type="button" class="link-button showAttemptsBtn" data-job-id="${escapeHtml(jobId)}" data-shard-id="${escapeHtml(shard.id)}">Attempt history</button>`;
  }
  if (stateItem.loading) {
    return `<div class="small">Loading attempt history...</div>`;
  }
  if (stateItem.error) {
    return `<div class="small error">${escapeHtml(stateItem.error)}</div>`;
  }
  const attemptsPayload = stateItem.payload || {};
  const attempts = Array.isArray(attemptsPayload.items)
    ? attemptsPayload.items
    : (Array.isArray(stateItem.items) ? stateItem.items : []);
  if (!attempts.length) {
    return `<div class="small">No attempt history yet.</div>`;
  }
  const total = Number(attemptsPayload.total || attempts.length || 0);
  const offset = Number(attemptsPayload.offset || stateItem.offset || 0);
  const limit = Number(attemptsPayload.limit || stateItem.limit || 100);
  const rangeStart = total ? offset + 1 : 0;
  const rangeEnd = Math.min(offset + attempts.length, total);
  const previousDisabled = offset <= 0 ? " disabled" : "";
  const nextDisabled = !attemptsPayload.has_more ? " disabled" : "";
  return `<div class="small">Attempt page ${escapeHtml(rangeStart)}-${escapeHtml(rangeEnd)} of ${escapeHtml(total)}</div>
  <div class="attempt-list">${
    attempts.map((attempt) => {
      const problem = [attempt.failure_category, attempt.error_message].filter(Boolean).join(": ");
      const execution = attempt.execution_paused
        ? `paused; api limit ${attempt.api_concurrency_limit || "-"}`
        : `running; api limit ${attempt.api_concurrency_limit || "-"}`;
      return `<div class="small path-full">
        <span class="${statusClass(attempt.status)}">${escapeHtml(attempt.status)}</span>
        #${escapeHtml(attempt.attempt_number)} ${escapeHtml(attempt.server_id)}
        files ${escapeHtml(attempt.processed_files || 0)}
        pages ${escapeHtml(attempt.completed_pages || 0)}
        execution ${escapeHtml(execution)}
        ${attempt.execution_control_reason ? ` ${escapeHtml(attempt.execution_control_reason)}` : ""}
        ${problem ? ` ${escapeHtml(problem)}` : ""}
      </div>`;
    }).join("")
  }</div>
  <div class="shard-inspector-tools">
    <button type="button" class="link-button attemptPageBtn" data-job-id="${escapeHtml(jobId)}" data-shard-id="${escapeHtml(shard.id)}" data-direction="prev" data-limit="${escapeHtml(limit)}" data-offset="${escapeHtml(offset)}"${previousDisabled}>Previous attempts</button>
    <button type="button" class="link-button attemptPageBtn" data-job-id="${escapeHtml(jobId)}" data-shard-id="${escapeHtml(shard.id)}" data-direction="next" data-limit="${escapeHtml(limit)}" data-offset="${escapeHtml(offset)}"${nextDisabled}>Next attempts</button>
  </div>`;
}

function formatShardTableRow(shard) {
  const files = `${shard.processed_files || 0}/${shard.file_count || 0}`;
  const api = `${shard.api_inflight || 0}/${shard.api_inflight_peak || 0} peak`;
  const execution = shard.execution_paused
    ? `paused; api limit ${shard.api_concurrency_limit || "-"}`
    : `running; api limit ${shard.api_concurrency_limit || "-"}`;
  const lease = shard.lease_expires_at
    ? `${shard.lease_expires_at}`
    : (shard.lease_status || "-");
  const attempts = shard.attempt_count == null ? "-" : shard.attempt_count;
  const problem = [shard.failure_category, shard.error_message].filter(Boolean).join(": ");
  return `<tr>
    <td class="mono">${escapeHtml(shard.shard_index)}</td>
    <td><span class="${statusClass(shard.status)}">${escapeHtml(shard.status)}</span></td>
    <td class="path-full">${escapeHtml(shard.assigned_server_id || "-")}</td>
    <td>${escapeHtml(files)}</td>
    <td>${escapeHtml(shard.completed_pages || 0)}</td>
    <td>${escapeHtml(api)}<div class="small">oldest ${escapeHtml(formatNumber(shard.oldest_api_inflight || 0, 1))}s</div><div class="small">Execution ${escapeHtml(execution)}</div><div class="small">${escapeHtml(shard.execution_control_reason || "-")}</div></td>
    <td class="small">${escapeHtml(lease)}</td>
    <td>${escapeHtml(attempts)}<div>${formatShardAttempts(shard.job_id, shard)}</div></td>
    <td class="small path-full">${escapeHtml(problem || "-")}</td>
  </tr>`;
}

function formatManifestIntegrity(job) {
  const integrity = state.manifestIntegrity.get(job.id);
  if (!integrity) {
    return `<button type="button" class="link-button checkManifestIntegrityBtn" data-id="${escapeHtml(job.id)}">Check manifest integrity</button>
      <div class="small">Checks manifest file, shard files, and file counts only when requested.</div>`;
  }
  if (integrity.loading) {
    return `<div class="small">Checking manifest integrity...</div>`;
  }
  if (integrity.error) {
    return `<div class="small error">${escapeHtml(integrity.error)}</div>`;
  }
  const payload = integrity.payload || {};
  const bad = Array.isArray(payload.bad_shards) ? payload.bad_shards : [];
  const badScanUnits = Array.isArray(payload.bad_scan_units) ? payload.bad_scan_units : [];
  const badShardCount = Number(payload.bad_shard_count || bad.length || 0);
  const badScanUnitCount = Number(payload.bad_scan_unit_count || badScanUnits.length || 0);
  const badShardSampleText = badShardCount > bad.length
    ? `sampled ${bad.length} of ${badShardCount} shard issues`
    : `${badShardCount} shard issues`;
  const inaccessibleFromControl = payload.status === "not_accessible_from_control";
  const statusLabel = payload.ok ? "ok" : (inaccessibleFromControl ? "not accessible" : "failed");
  const statusClassName = payload.ok ? "succeeded" : (inaccessibleFromControl ? "warning" : "failed");
  const workerStatus = payload.worker_integrity_status || "-";
  const sourceLine = payload.source === "worker"
    ? `<div class="small">checked by worker ${escapeHtml(payload.checked_by_server_id || "-")} at ${escapeHtml(payload.checked_at ? formatDateTime(payload.checked_at) : "-")}</div>`
    : "";
  const badScanUnitSampleText = badScanUnitCount > badScanUnits.length
    ? `sampled ${badScanUnits.length} of ${badScanUnitCount} scan-unit manifest issues`
    : `${badScanUnitCount} scan-unit manifest issues`;
  const badText = bad.length
    ? bad.slice(0, 3).map((item) => `#${item.shard_index} ${item.reason}`).join("; ")
    : "no shard file issues";
  const manifestError = payload.manifest_error || "-";
  const metaError = payload.meta_error || "-";
  const badScanUnitText = badScanUnits.length
    ? badScanUnits.slice(0, 3).map((item) => `#${item.scan_unit_id} ${item.reason}`).join("; ")
    : "no scan-unit manifest issues";
  const scanUnitLine = payload.scan_unit_count
    ? `<div class="small">scan units ${escapeHtml(payload.scan_unit_count)}; scan_unit_manifest_count_matches ${escapeHtml(Boolean(payload.scan_unit_manifest_count_matches))}</div>
       <div class="small">${escapeHtml(badScanUnitSampleText)}</div>
       <div class="small">${escapeHtml(badScanUnitText)}</div>`
    : "";
  return `<div class="detail-section">
    <div class="cell-row">
      <span class="${statusClass(statusClassName)}">${escapeHtml(statusLabel)}</span>
      <button type="button" class="link-button checkManifestIntegrityBtn" data-id="${escapeHtml(job.id)}">Refresh integrity</button>
      ${inaccessibleFromControl ? `<button type="button" class="link-button requestWorkerManifestIntegrityBtn" data-id="${escapeHtml(job.id)}">Request worker check</button>` : ""}
    </div>
    ${sourceLine}
    ${inaccessibleFromControl ? `<div class="small">Manifest paths are on a worker shared root that this control process cannot read directly; worker_integrity_status ${escapeHtml(workerStatus)}.</div>` : ""}
    <div class="small">manifest file ${escapeHtml(payload.manifest_file_exists ? "exists" : "missing")}; manifest_file_count_matches ${escapeHtml(Boolean(payload.manifest_file_count_matches))}; manifest_error ${escapeHtml(manifestError)}; meta_error ${escapeHtml(metaError)}</div>
    <div class="small">meta file_count ${escapeHtml(payload.meta_actual_file_count ?? "-")}/${escapeHtml(payload.meta_expected_file_count ?? 0)}; meta_file_count_matches ${escapeHtml(Boolean(payload.meta_file_count_matches))}</div>
    ${scanUnitLine}
    <div class="small">${escapeHtml(badShardSampleText)}</div>
    <div class="small">shards ${escapeHtml(payload.shard_count || 0)}; shard total ${escapeHtml(payload.shard_expected_file_count || 0)}/${escapeHtml(payload.shard_reference_file_count || 0)}; shard_file_count_matches_manifest ${escapeHtml(Boolean(payload.shard_file_count_matches_manifest))}; ${escapeHtml(badText)}</div>
  </div>`;
}

function formatManifestFreezeReport(job) {
  const freeze = state.manifestFreezeReports.get(job.id);
  if (!freeze) {
    return `<button type="button" class="link-button checkManifestFreezeBtn" data-id="${escapeHtml(job.id)}">Check manifest freeze</button>
      <div class="small">Shows the frozen scan snapshot after distributed scanning finishes.</div>`;
  }
  if (freeze.loading) {
    return `<div class="small">Checking manifest freeze...</div>`;
  }
  if (freeze.error) {
    return `<div class="small error">${escapeHtml(freeze.error)}</div>`;
  }
  const payload = freeze.payload || {};
  const report = payload.report || {};
  const scanUnits = report.scan_units || {};
  const shards = report.shards || {};
  const frozen = Boolean(report.frozen);
  const frozenAt = payload.frozen_at ? formatDateTime(payload.frozen_at) : "-";
  const scanText = [
    `succeeded ${scanUnits.succeeded || 0}`,
    `failed ${scanUnits.failed || 0}`,
    `pending ${scanUnits.pending || 0}`,
    `running ${scanUnits.running || 0}`,
    `stale ${scanUnits.stale || 0}`,
  ].join("; ");
  const shardText = [
    `pending ${shards.pending || 0}`,
    `running ${shards.running || 0}`,
    `succeeded ${shards.succeeded || 0}`,
    `failed ${shards.failed || 0}`,
    `stale ${shards.stale || 0}`,
  ].join("; ");
  return `<div class="detail-section">
    <div class="cell-row">
      <span class="${statusClass(frozen ? "succeeded" : payload.status || "running")}">${escapeHtml(frozen ? "frozen" : (payload.status || "not frozen"))}</span>
      <button type="button" class="link-button checkManifestFreezeBtn" data-id="${escapeHtml(job.id)}">Refresh freeze</button>
    </div>
    <div class="metric-pairs">
      <div><div class="detail-label">Frozen at</div><div>${escapeHtml(frozenAt)}</div></div>
      <div><div class="detail-label">Files</div><div class="primary-text">${escapeHtml(report.file_count || 0)}</div></div>
      <div><div class="detail-label">Bytes</div><div>${escapeHtml(formatBytes(report.total_bytes || 0))}</div></div>
      <div><div class="detail-label">Shards</div><div class="primary-text">${escapeHtml(report.shard_count || 0)}</div></div>
    </div>
    <div class="small">scan units: ${escapeHtml(scanText)}</div>
    <div class="small">shards: ${escapeHtml(shardText)}</div>
    <div class="small">scan errors: ${escapeHtml(report.scan_error_count || 0)}</div>
  </div>`;
}

function formatJobDetailsPanel(job) {
  return `<div class="job-detail-panel">
    <div class="job-detail-layout">
      <div class="detail-card detail-card-paths">
        <h4>Paths</h4>
        <div class="detail-section">
          <div>
            <div class="detail-label">Input</div>
            <div class="small path-full">${escapeHtml(job.input_dir)}</div>
          </div>
          <div>
            <div class="detail-label">Output</div>
            <div class="small path-full">${escapeHtml(job.output_dir)}</div>
          </div>
        </div>
      </div>
      <div class="detail-card">
        <h4>Job overview</h4>
        <div class="detail-section">${formatJobOverview(job)}</div>
      </div>
      <div class="detail-card">
        <h4>Scan lifecycle</h4>
        ${formatScanLifecycle(job)}
      </div>
      <div class="detail-card">
        <h4>Lifecycle</h4>
        ${formatJobLifecycle(job)}
      </div>
      <div class="detail-card">
        <h4>Worker current shards</h4>
        ${formatWorkerAllocation(job)}
      </div>
      <div class="detail-card detail-card-wide">
        <h4>Manifest integrity</h4>
        ${formatManifestIntegrity(job)}
      </div>
      <div class="detail-card detail-card-wide">
        <h4>Manifest freeze</h4>
        ${formatManifestFreezeReport(job)}
      </div>
      <div class="detail-card detail-card-wide">
        <h4>Attention shards</h4>
        ${formatAttentionShards(job)}
      </div>
      <div class="detail-card detail-card-wide">
        <h4>Shard inspector</h4>
        ${formatShardInspector(job)}
      </div>
      <div class="detail-card">
        <h4>Freshness</h4>
        <div>${formatFreshness(job)}</div>
      </div>
      <div class="detail-card">
        <h4>Health</h4>
        ${formatHealth(job) || `<span class="${statusClass("healthy")}">healthy</span>`}
      </div>
      <div class="detail-card detail-card-wide">
        <h4>Recent errors</h4>
        ${formatRecentErrors(job)}
      </div>
      <div class="detail-card detail-card-wide">
        <h4>Job logs</h4>
        ${formatJobLogs(job)}
      </div>
    </div>
  </div>`;
}

function formatServerHeartbeat(server) {
  const stale = server.is_stale ? `<span class="status stale">stale</span>` : "";
  if (!server.last_heartbeat_at) {
    return `${stale}<div class="small">No heartbeat yet</div>`;
  }
  const date = new Date(server.last_heartbeat_at);
  return `${stale}<div class="small">${escapeHtml(date.toLocaleString())}</div>`;
}

function formatSharedPaths(server) {
  const paths = (server.capabilities && server.capabilities.shared_paths) || [];
  if (!Array.isArray(paths) || !paths.length) {
    return `<span class="muted">not reported</span>`;
  }
  return paths
    .map((item) => {
      const ok = item.exists && item.is_dir && item.readable;
      const label = ok ? "ok" : "unavailable";
      const cls = ok ? "succeeded" : "failed";
      const details = [
        item.readable ? "read" : "no read",
        item.writable ? "write" : "no write",
      ].join("; ");
      return `<div class="worker-detail-item">
        <div><span class="${statusClass(cls)}">${escapeHtml(label)}</span></div>
        <div class="small path-full">${escapeHtml(item.path)}</div>
        <div class="small">${escapeHtml(details)}</div>
      </div>`;
    })
    .join("");
}

function formatSharedPathSummary(server) {
  const paths = (server.capabilities && server.capabilities.shared_paths) || [];
  if (!Array.isArray(paths) || !paths.length) {
    return `<span class="muted">not reported</span>`;
  }
  const ready = paths.filter(sharedPathReady);
  const writable = ready.filter((item) => item.writable);
  const primary = ready[0] || paths[0];
  const label = ready.length ? "ok" : "blocked";
  const cls = ready.length ? "succeeded" : "failed";
  const detail = ready.length
    ? `${ready.length}/${paths.length} readable; ${writable.length} writable`
    : `${paths.length} reported`;
  return `<div class="cell-stack">
    <div class="cell-row"><span class="${statusClass(cls)}">${escapeHtml(label)}</span><span class="small">${escapeHtml(detail)}</span></div>
    <div class="small path-text" title="${escapeHtml(primary.path || "")}">${escapeHtml(primary.path || "")}</div>
  </div>`;
}

function formatWorkerVersion(server) {
  const caps = server.capabilities || {};
  const version = caps.git_ref || "unknown git";
  const script = caps.script_version || "unknown script";
  return `<div class="cell-stack">
    <div class="primary-text mono">${escapeHtml(version)}</div>
    <div class="small">${escapeHtml(script)}</div>
  </div>`;
}

function formatVersionWarning(servers) {
  const visible = visibleServers(servers);
  const versions = new Map();
  for (const server of visible) {
    const caps = server.capabilities || {};
    const key = [caps.git_ref || "unknown git", caps.script_version || "unknown script"].join(" / ");
    if (!versions.has(key)) versions.set(key, []);
    versions.get(key).push(server.id);
  }
  if (versions.size <= 1) return "";
  const details = Array.from(versions.entries())
    .slice(0, 4)
    .map(([version, ids]) => `${version}: ${ids.join(", ")}`)
    .join(" | ");
  return `<div class="diagnosis small"><span class="status warning">mixed worker versions</span> ${escapeHtml(details)}</div>`;
}

function formatJobVersionWarning(job) {
  if (job.worker_version_status !== "mixed") return "";
  const refs = job.worker_version_refs || {};
  const details = Object.entries(refs)
    .slice(0, 4)
    .map(([version, ids]) => `${version}: ${Array.isArray(ids) ? ids.join(", ") : ids}`)
    .join(" | ");
  const message = job.worker_version_warning || "assigned workers report different git_ref or script_version values";
  return `<div class="diagnosis small"><span class="status warning">mixed worker versions</span> ${escapeHtml(message)} ${escapeHtml(details)}</div>`;
}

function formatWorkerIdentity(server) {
  const expanded = state.openWorkerDetails.has(server.id);
  return `<div class="cell-stack">
    <div class="primary-text">${escapeHtml(server.id)}</div>
    <div class="small">${escapeHtml(server.host)}</div>
    ${formatServerHeartbeat(server)}
    <button type="button" class="link-button workerDetailsToggle" data-id="${escapeHtml(server.id)}" aria-expanded="${expanded ? "true" : "false"}">
      ${expanded ? "▼" : "▶"} details
    </button>
  </div>`;
}

function formatWorkerHealth(server, readiness) {
  return `<div class="cell-stack">
    <div class="cell-row">
      <span class="${statusClass(server.status)}">${escapeHtml(server.status)}</span>
      <span class="${statusClass(readiness.level)}">${escapeHtml(readiness.label)}</span>
    </div>
    ${workerReadinessNotes(readiness)}
  </div>`;
}

function formatWorkerLoad(server) {
  const capacity = Number(server.capacity_slots || 1);
  const runningShards = Number(server.running_shards || 0);
  const activeJobs = Number(server.active_jobs || 0);
  const resources = (server.capabilities && server.capabilities.system_resources) || {};
  const cpuPercent = resources.cpu ? resources.cpu.load_percent_1m : null;
  const memoryPercent = resources.memory ? resources.memory.percent : null;
  const disks = Array.isArray(resources.disks) ? resources.disks : [];
  const diskPercents = disks.map((item) => Number(item.percent || 0)).filter((item) => Number.isFinite(item));
  const diskPercent = diskPercents.length
    ? Math.max(...diskPercents)
    : null;
  const resourceLine = resources.checked_at
    ? `<div class="small">CPU ${escapeHtml(resourcePercent(cpuPercent))}; MEM ${escapeHtml(resourcePercent(memoryPercent))}; DISK ${escapeHtml(resourcePercent(diskPercent))}</div>`
    : "";
  return `<div class="cell-stack">
    <div class="primary-text">${escapeHtml(runningShards)} / ${escapeHtml(capacity)}</div>
    <div class="small">running shards</div>
    <div class="small">${escapeHtml(activeJobs)} active jobs</div>
    ${resourceLine}
  </div>`;
}

function formatWorkerRuntime(server) {
  const caps = server.capabilities || {};
  const details = [
    ["git", caps.git_ref],
    ["script", caps.script_version],
    ["python", caps.python_path],
    ["repo", caps.repo_dir],
    ["work", caps.work_dir],
  ].filter(([, value]) => value);
  if (!details.length) {
    return `<span class="muted">not reported</span>`;
  }
  return `<div class="detail-section">${
    details
      .map(([label, value]) => `<div>
        <div class="detail-label">${escapeHtml(label)}</div>
        <div class="small path-full">${escapeHtml(value)}</div>
      </div>`)
      .join("")
  }</div>`;
}

function formatWorkerLoadDetails(server) {
  const capacity = Number(server.capacity_slots || 1);
  const runningShards = Number(server.running_shards || 0);
  const activeJobs = Number(server.active_jobs || 0);
  return `<div class="metric-pairs">
    <div><div class="detail-label">Running shards</div><div class="primary-text">${escapeHtml(runningShards)}</div></div>
    <div><div class="detail-label">Capacity</div><div class="primary-text">${escapeHtml(capacity)}</div></div>
    <div><div class="detail-label">Active jobs</div><div class="primary-text">${escapeHtml(activeJobs)}</div></div>
    <div><div class="detail-label">Status</div><div><span class="${statusClass(server.status)}">${escapeHtml(server.status)}</span></div></div>
  </div>`;
}

function formatWorkerResourceDetails(server) {
  const resources = (server.capabilities && server.capabilities.system_resources) || null;
  const pressure = (server.capabilities && server.capabilities.resource_pressure) || {};
  if (!resources || !resources.checked_at) {
    return `<span class="muted">not reported</span>`;
  }
  const cpu = resources.cpu || {};
  const memory = resources.memory || {};
  const disks = Array.isArray(resources.disks) ? resources.disks : [];
  const diskItems = disks.length
    ? disks.map((disk) => `<div class="worker-detail-item">
        <div class="cell-row"><span class="${statusClass(resourceStatus(disk.percent))}">${escapeHtml(resourcePercent(disk.percent))}</span><span class="small">${escapeHtml(disk.exists ? "exists" : "missing")}</span></div>
        <div class="small path-full">${escapeHtml(disk.path || "")}</div>
        <div class="small">${escapeHtml(formatBytes(disk.free_bytes))} free / ${escapeHtml(formatBytes(disk.total_bytes))}</div>
      </div>`).join("")
    : `<span class="muted">no disk paths reported</span>`;
  return `<div class="detail-section">
    <div>
      <div class="detail-label">Guard</div>
      <div class="cell-row">
        <span class="${statusClass(pressure.constrained ? "blocked" : (pressure.level || "ready"))}">${escapeHtml(pressure.constrained ? "resource constrained" : (pressure.level || "ready"))}</span>
      </div>
      ${Array.isArray(pressure.reasons) && pressure.reasons.length ? `<div class="small">${escapeHtml(pressure.reasons.join("; "))}</div>` : ""}
    </div>
    <div class="metric-pairs">
      <div>
        <div class="detail-label">CPU load</div>
        <div><span class="${statusClass(resourceStatus(cpu.load_percent_1m))}">${escapeHtml(resourcePercent(cpu.load_percent_1m))}</span></div>
        <div class="small">1m ${escapeHtml(formatNumber(cpu.load_avg_1m, 2))}; ${escapeHtml(cpu.logical_count || "-")} cores</div>
      </div>
      <div>
        <div class="detail-label">Memory</div>
        <div><span class="${statusClass(resourceStatus(memory.percent))}">${escapeHtml(resourcePercent(memory.percent))}</span></div>
        <div class="small">${escapeHtml(formatBytes(memory.available_bytes))} available / ${escapeHtml(formatBytes(memory.total_bytes))}</div>
      </div>
    </div>
    <div>
      <div class="detail-label">Disks</div>
      <div class="worker-detail-list scroll-detail-list">${diskItems}</div>
    </div>
    <div class="small">checked ${escapeHtml(new Date(resources.checked_at).toLocaleString())}</div>
  </div>`;
}

function formatWorkerEventSpool(server) {
  const backlog = workerEventSpoolBacklog(server);
  const status = backlog.total > 0 ? "warning" : "succeeded";
  const label = backlog.total > 0 ? `${backlog.total} queued, quarantined, or dropped` : "clear";
  const dir = backlog.dir
    ? `<div class="small path-full">${escapeHtml(backlog.dir)}</div>`
    : `<div class="small muted">spool dir not reported</div>`;
  return `<div class="detail-section">
    <div class="cell-row">
      <span class="${statusClass(status)}">${escapeHtml(label)}</span>
    </div>
    <div class="metric-pairs">
      <div><div class="detail-label">Pending events</div><div class="primary-text">${escapeHtml(backlog.pending_events)}</div></div>
      <div><div class="detail-label">Pending logs</div><div class="primary-text">${escapeHtml(backlog.pending_logs)}</div></div>
      <div><div class="detail-label">Failed events</div><div class="primary-text">${escapeHtml(backlog.failed_events)}</div></div>
      <div><div class="detail-label">Failed logs</div><div class="primary-text">${escapeHtml(backlog.failed_logs)}</div></div>
      <div><div class="detail-label">Dropped events</div><div class="primary-text">${escapeHtml(backlog.dropped_events)}</div></div>
      <div><div class="detail-label">Dropped logs</div><div class="primary-text">${escapeHtml(backlog.dropped_logs)}</div></div>
    </div>
    ${dir}
  </div>`;
}

function formatWorkerPendingShardUpdates(server) {
  const backlog = workerPendingShardUpdateBacklog(server);
  const status = backlog.total > 0 ? "warning" : "succeeded";
  const label = backlog.total > 0 ? `${backlog.total} queued or quarantined` : "clear";
  return `<div class="detail-section">
    <div class="cell-row">
      <span class="${statusClass(status)}">${escapeHtml(label)}</span>
    </div>
    <div class="metric-pairs">
      <div><div class="detail-label">Pending updates</div><div class="primary-text">${escapeHtml(backlog.pending)}</div></div>
      <div><div class="detail-label">Failed updates</div><div class="primary-text">${escapeHtml(backlog.failed)}</div></div>
    </div>
  </div>`;
}

function formatWorkerDetailsPanel(server, readiness) {
  return `<div class="job-detail-panel">
    <div class="job-detail-layout">
      <div class="detail-card">
        <h4>Runtime</h4>
        ${formatWorkerRuntime(server)}
      </div>
      <div class="detail-card">
        <h4>Shared paths</h4>
        <div class="worker-detail-list scroll-detail-list">${formatSharedPaths(server)}</div>
      </div>
      <div class="detail-card">
        <h4>Health</h4>
        ${formatWorkerHealth(server, readiness)}
      </div>
      <div class="detail-card">
        <h4>Load</h4>
        ${formatWorkerLoadDetails(server)}
      </div>
      <div class="detail-card">
        <h4>Resources</h4>
        ${formatWorkerResourceDetails(server)}
      </div>
      <div class="detail-card">
        <h4>Event/log spool</h4>
        ${formatWorkerEventSpool(server)}
      </div>
      <div class="detail-card">
        <h4>Shard update spool</h4>
        ${formatWorkerPendingShardUpdates(server)}
      </div>
      <div class="detail-card">
        <h4>Heartbeat</h4>
        ${formatServerHeartbeat(server)}
      </div>
    </div>
  </div>`;
}

function renderServers(servers) {
  state.servers = servers;
  const visible = visibleServers(servers);
  const online = onlineWorkers(servers);
  const readiness = visible.map((server) => classifyWorkerReadiness(server));
  const ready = readiness.filter((item) => item.level === "ready");
  const warning = readiness.filter((item) => item.level === "warning");
  const blocked = readiness.filter((item) => item.level === "blocked");
  ui.serverMetrics.innerHTML = [
    `<div class="metric"><strong>${escapeHtml(ready.length)}</strong><span class="small">Ready workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(warning.length)}</strong><span class="small">Warning workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(blocked.length)}</strong><span class="small">Blocked workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(online.reduce((sum, server) => sum + (server.running_shards || 0), 0))}</strong><span class="small">running shards</span></div>`,
  ].join("") + formatVersionWarning(servers);
  renderOperationsSummary();
  if (!visible.length) {
    ui.serversBody.innerHTML = `<tr><td colspan="6" class="muted">No servers yet</td></tr>`;
    return;
  }
  ui.serversBody.innerHTML = visible
    .flatMap((server) => {
      const itemReadiness = classifyWorkerReadiness(server);
      const rows = [`<tr>
        <td>${formatWorkerIdentity(server)}</td>
        <td>${formatWorkerHealth(server, itemReadiness)}</td>
        <td>${formatWorkerLoad(server)}</td>
        <td>${formatWorkerVersion(server)}</td>
        <td>${formatSharedPathSummary(server)}</td>
        <td>${workerActions(server)}</td>
      </tr>`];
      if (state.openWorkerDetails.has(server.id)) {
        rows.push(`<tr class="detail-row"><td colspan="6">${formatWorkerDetailsPanel(server, itemReadiness)}</td></tr>`);
      }
      return rows;
    })
    .join("");
}

function formatHealth(job) {
  const parts = [];
  if (job.failure_category) {
    parts.push(`<span class="status failed">${escapeHtml(job.failure_category)}</span>`);
  }
  parts.push(formatFailureCategoryCounts("File failures", job.failure_category_counts));
  parts.push(formatFailureCategoryCounts("Shard failures", job.shard_failure_category_counts));
  parts.push(formatFailureCategoryCounts("Scan-unit failures", job.scan_unit_failure_category_counts));
  if ((job.degraded_pages || 0) > 0) {
    parts.push(`<span class="status stopping">image fallback</span>`);
    parts.push(`<div class="small">${escapeHtml(job.degraded_pages)} degraded pages</div>`);
  }
  if (job.error_message) {
    parts.push(`<div class="small" title="${escapeHtml(job.error_message)}">${escapeHtml(job.error_message)}</div>`);
  }
  if (job.quality_flags && job.quality_flags.length && !(job.degraded_pages > 0)) {
    parts.push(`<div class="small">${escapeHtml(job.quality_flags.join(", "))}</div>`);
  }
  return parts.join("") || `<span class="muted">ok</span>`;
}

function renderJobs(jobs) {
  if (!jobs.length) {
    ui.jobsBody.innerHTML = `<tr><td colspan="6" class="muted">No jobs yet</td></tr>`;
    renderOperationsSummary();
    return;
  }

  ui.jobsBody.innerHTML = jobs
    .flatMap((job) => {
      const terminal = ["succeeded", "failed", "stopped"].includes(job.status);
      const actions = terminal
        ? `<div class="cell-row"><button data-id="${escapeHtml(job.id)}" class="archiveJobBtn">Archive</button><button data-id="${escapeHtml(job.id)}" class="deleteBtn">Delete</button></div>`
        : `<button data-id="${escapeHtml(job.id)}" class="stopBtn">Stop</button>`;
      const rows = [`<tr>
        <td>${formatJobIdentity(job)}</td>
        <td>${formatJobStatus(job)}</td>
        <td>${rowProgress(job)}</td>
        <td>${rowThroughput(job)}</td>
        <td>${rowWorkerSummary(job)}</td>
        <td>${actions}</td>
      </tr>`];
      if (state.openJobDetails.has(job.id)) {
        rows.push(`<tr class="detail-row"><td colspan="6">${formatJobDetailsPanel(job)}</td></tr>`);
      }
      return rows;
    })
    .join("");
  renderOperationsSummary();
}

function renderWorkerSelectors(servers, selectedIds) {
  const selectable = visibleServers(servers);
  const options = selectable.map((server) =>
    `<option value="${escapeHtml(server.id)}">${escapeHtml(server.id)} (${escapeHtml(server.status)}, ${escapeHtml(server.host)})</option>`
  ).join("");
  ui.singleWorkerSelect.innerHTML =
    options || "<option value=\"\" disabled>No servers yet</option>";
  if (selectedIds.length && selectable.some((server) => server.id === selectedIds[0])) {
    ui.singleWorkerSelect.value = selectedIds[0];
  } else if (options) {
    ui.singleWorkerSelect.selectedIndex = 0;
  }

  if (!selectable.length) {
    ui.selectedWorkersList.innerHTML = `<span class="muted">No workers yet</span>`;
    return;
  }
  ui.selectedWorkersList.innerHTML = selectable
    .map((server) => {
      const checked = selectedIds.includes(server.id) ? " checked" : "";
      const label = `${server.id} (${server.status}, ${server.host})`;
      return `<label class="check-item"><input type="checkbox" value="${escapeHtml(server.id)}"${checked} /> ${escapeHtml(label)}</label>`;
    })
    .join("");
}

async function loadServers() {
  const servers = await requestJson(SERVERS_API_ROOT);
  renderServers(servers);
  const selected = selectedAllowedServerIds();
  renderWorkerSelectors(servers, selected);
  syncExecutionModeFields();
  schedulePreflight();
}

function renderPreflightDiagnosis(eligibility) {
  if (!eligibility) return "";
  const byId = new Map(eligibility.servers.map((item) => [item.server_id, item]));
  const visible = visibleServers();
  if (!visible.length) {
    return `<div class="diagnosis small">no visible workers registered</div>`;
  }
  const readiness = visible.map((server) => classifyWorkerReadiness(server, byId.get(server.id)));
  const blocked = readiness.filter((item) => item.level === "blocked");
  const warning = readiness.filter((item) => item.level === "warning");
  const versions = new Set(
    visible.map((server) => {
      const caps = server.capabilities || {};
      return [caps.git_ref || "unknown git", caps.script_version || "unknown script"].join(" / ");
    })
  );
  const lines = [];
  if (blocked.length) {
    lines.push(`${blocked.length} blocked: ${blocked.slice(0, 3).map((item) => item.blockers[0] || "not ready").join("; ")}`);
  }
  if (warning.length) {
    lines.push(`${warning.length} warning: ${warning.slice(0, 3).map((item) => item.warnings[0] || "check runtime").join("; ")}`);
  }
  if (versions.size > 1) {
    lines.push(`mixed versions: ${Array.from(versions).slice(0, 3).join("; ")}`);
  }
  return lines.length ? `<div class="diagnosis small">${escapeHtml(lines.join(" | "))}</div>` : `<div class="diagnosis small">all visible workers are ready for this path</div>`;
}

function inferDefaultManifestRoot(eligibility) {
  if (!eligibility || selectedInputMode() === "existing_manifest") {
    return "";
  }
  const mode = selectedInputMode();
  const allowedServerIds = selectedAllowedServerIds();
  const limitToSelected = mode === "directory" || ui.workerScope.value === "selected";
  const visible = eligibility.servers.filter((server) => server.server_id !== "__server_pool__");
  const eligible = visible.filter((server) => server.can_access && server.matched_path);
  const scopedEligible = limitToSelected
    ? eligible.filter((server) => allowedServerIds.includes(server.server_id))
    : eligible;
  const roots = Array.from(new Set(scopedEligible.map((server) => String(server.matched_path).replace(/\/+$/, ""))));
  return roots.length === 1 ? `${roots[0]}/.ocr_platform/manifests` : "";
}

function applyDefaultManifestRoot(eligibility) {
  const manifestRootInput = document.getElementById("manifestRoot");
  const nextRoot = inferDefaultManifestRoot(eligibility);
  const currentRoot = manifestRootInput.value.trim();
  if (!nextRoot) {
    if (currentRoot && currentRoot === state.lastAutoManifestRoot) {
      manifestRootInput.value = "";
    }
    state.lastAutoManifestRoot = "";
    return;
  }
  if (!currentRoot || currentRoot === state.lastAutoManifestRoot) {
    manifestRootInput.value = nextRoot;
    state.lastAutoManifestRoot = nextRoot;
  }
}

function perWorkerApiConcurrencyMax() {
  const explicitMax = Number(document.getElementById("apiConcurrencyMax").value || 0);
  if (explicitMax) return explicitMax;
  const profile = selectedModelProfile();
  const extra = (profile && profile.extra_args) || {};
  return Number(extra.api_concurrency_max || extra.api_concurrency_start || extra.api_concurrency || profile?.page_concurrency || 0) || 0;
}

function activeShardRuntimeMetrics() {
  const shards = [];
  for (const job of state.jobs || []) {
    if (Array.isArray(job.current_shards)) shards.push(...job.current_shards);
    if (Array.isArray(job.attention_shards)) shards.push(...job.attention_shards);
  }
  const unique = new Map();
  shards.forEach((shard) => {
    if (shard && shard.shard_id != null) unique.set(shard.shard_id, shard);
  });
  const items = Array.from(unique.values());
  return {
    running_shards: items.filter((shard) => shard.status === "running").length,
    api_inflight: items.reduce((sum, shard) => sum + Number(shard.api_inflight || 0), 0),
    api_waiting: items.reduce((sum, shard) => sum + Number(shard.api_waiting || 0), 0),
    oldest_api_inflight: items.reduce((max, shard) => Math.max(max, Number(shard.oldest_api_inflight || 0)), 0),
  };
}

function renderPreflight(eligibility) {
  if (!eligibility) {
    ui.preflightMetrics.innerHTML = `<div class="metric"><strong>-</strong><span class="small">path check</span></div>`;
    return;
  }
  const visible = eligibility.servers.filter((server) => server.server_id !== "__server_pool__");
  const eligible = visible.filter((server) => server.can_access);
  const eligibilityById = new Map(visible.map((server) => [server.server_id, server]));
  const workerReadiness = visibleServers().map((server) => classifyWorkerReadiness(server, eligibilityById.get(server.id)));
  const ready = workerReadiness.filter((item) => item.level === "ready");
  const warning = workerReadiness.filter((item) => item.level === "warning");
  const blocked = workerReadiness.filter((item) => item.level === "blocked");
  const mode = selectedInputMode();
  const allowedServerIds = selectedAllowedServerIds();
  const limitToSelected = mode === "directory" || ui.workerScope.value === "selected";
  const scopedEligible = limitToSelected
    ? eligible.filter((server) => allowedServerIds.includes(server.server_id))
    : eligible;
  const selectedEligibility = visible.find((server) => server.server_id === allowedServerIds[0]);
  const pathStatus = mode === "directory"
    ? (selectedEligibility && selectedEligibility.can_access ? "ok" : "check")
    : (scopedEligible.length > 0 ? "ok" : "blocked");
  const shardText = mode === "directory" ? "single worker" : `${document.getElementById("targetFilesPerShard").value || 1000}/shard`;
  const perWorkerApi = perWorkerApiConcurrencyMax();
  const plannedApi = perWorkerApi ? scopedEligible.length * perWorkerApi : "-";
  const runtime = activeShardRuntimeMetrics();
  ui.preflightMetrics.innerHTML = [
    `<div class="metric"><strong>${escapeHtml(pathStatus)}</strong><span class="small">shared path</span></div>`,
    `<div class="metric"><strong>${escapeHtml(scopedEligible.length)}</strong><span class="small">eligible workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(plannedApi)}</strong><span class="small">planned API concurrency</span></div>`,
    `<div class="metric"><strong>${escapeHtml(runtime.running_shards)}</strong><span class="small">running shards</span></div>`,
    `<div class="metric"><strong>${escapeHtml(runtime.api_inflight)}</strong><span class="small">API inflight</span></div>`,
    `<div class="metric"><strong>${escapeHtml(runtime.api_waiting)}</strong><span class="small">API waiting</span></div>`,
    `<div class="metric"><strong>${escapeHtml(formatNumber(runtime.oldest_api_inflight, 1))}s</strong><span class="small">oldest API inflight</span></div>`,
    `<div class="metric"><strong>${escapeHtml(ready.length)}</strong><span class="small">Ready workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(warning.length)}</strong><span class="small">Warning workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(blocked.length)}</strong><span class="small">Blocked workers</span></div>`,
    `<div class="metric"><strong>${escapeHtml(shardText)}</strong><span class="small">shard plan</span></div>`,
  ].join("") + renderPreflightDiagnosis(eligibility);
}

async function updatePreflight() {
  const inputDir = document.getElementById("inputDir").value.trim();
  if (!inputDir) {
    state.latestEligibility = null;
    const manifestRootInput = document.getElementById("manifestRoot");
    if (manifestRootInput.value.trim() === state.lastAutoManifestRoot) {
      manifestRootInput.value = "";
      state.lastAutoManifestRoot = "";
    }
    renderPreflight(null);
    return;
  }
  try {
    const eligibility = await requestJson(`/api/servers/eligibility?input_dir=${encodeURIComponent(inputDir)}`);
    state.latestEligibility = eligibility;
    applyDefaultManifestRoot(eligibility);
    renderPreflight(eligibility);
    setMessage(ui.jobMessage, "");
  } catch (error) {
    setMessage(ui.jobMessage, String(error.message || error), true);
  }
}

function schedulePreflight() {
  if (state.preflightTimer) clearTimeout(state.preflightTimer);
  state.preflightTimer = setTimeout(updatePreflight, 250);
}

async function loadJobs() {
  const url = buildJobsSummaryUrl();
  const payload = await requestJson(url);
  const jobs = Array.isArray(payload.items) ? payload.items : [];
  state.jobs = jobs;
  state.jobsPage.lastCount = jobs.length;
  state.jobsPage.total = Number(payload.total || 0);
  state.jobsPage.hasMore = Boolean(payload.has_more);
  renderJobs(jobs);
  renderJobsPagination();
  ui.refreshHint.textContent = `Last refreshed: ${new Date().toLocaleTimeString()}`;
}

async function refreshOperationsData({ quiet = false } = {}) {
  const results = await Promise.allSettled([
    loadDatabaseStatus(),
    loadDeploymentDoctor(),
    loadServers(),
    loadJobs(),
  ]);
  const errors = results
    .filter((result) => result.status === "rejected")
    .map((result) => result.reason && result.reason.message ? result.reason.message : String(result.reason));
  if (errors.length) {
    if (!quiet) setMessage(ui.jobsMessage, errors.join("; "), true);
    return false;
  }
  setMessage(ui.jobsMessage, "");
  return true;
}

function buildJobsSummaryUrl() {
  const params = new URLSearchParams({
    limit: String(state.jobsPage.limit),
    offset: String(state.jobsPage.offset)
  });
  if (state.jobsPage.status && state.jobsPage.status !== "all") {
    params.set("status", state.jobsPage.status);
  }
  if (state.jobsPage.includeArchived) {
    params.set("include_archived", "true");
  }
  return `/api/jobs/summary/page?${params.toString()}`;
}

function renderJobsPagination() {
  const start = state.jobsPage.lastCount ? state.jobsPage.offset + 1 : 0;
  const end = state.jobsPage.offset + state.jobsPage.lastCount;
  ui.jobsPageInfo.textContent = `${start}-${end} / ${state.jobsPage.total}`;
  ui.previousJobsBtn.disabled = state.jobsPage.offset <= 0;
  ui.nextJobsBtn.disabled = !state.jobsPage.hasMore;
}

async function loadShardInspector(jobId) {
  const inspector = shardInspectorState(jobId);
  inspector.loading = true;
  inspector.error = "";
  await loadJobs();
  try {
    const params = new URLSearchParams({
      status: inspector.status,
      limit: String(inspector.limit),
      offset: String(inspector.offset)
    });
    if (inspector.workerId) {
      params.set("worker_id", inspector.workerId);
    }
    if (inspector.failureCategory) {
      params.set("failure_category", inspector.failureCategory);
    }
    if (inspector.minAttemptCount !== "") {
      params.set("min_attempt_count", String(inspector.minAttemptCount));
    }
    if (inspector.runningLongerThanSeconds !== "") {
      params.set("running_longer_than_seconds", String(inspector.runningLongerThanSeconds));
    }
    inspector.payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/shards?${params.toString()}`);
  } catch (error) {
    inspector.error = String(error.message || error);
  } finally {
    inspector.loading = false;
    await loadJobs();
  }
}

async function checkManifestIntegrity(jobId) {
  state.manifestIntegrity.set(jobId, { loading: true, error: "", payload: null });
  await loadJobs();
  try {
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/manifest/integrity`);
    state.manifestIntegrity.set(jobId, { loading: false, error: "", payload });
  } catch (error) {
    state.manifestIntegrity.set(jobId, { loading: false, error: String(error.message || error), payload: null });
  }
  await loadJobs();
}

async function requestWorkerManifestIntegrity(jobId) {
  const existing = state.manifestIntegrity.get(jobId);
  state.manifestIntegrity.set(jobId, {
    loading: true,
    error: "",
    payload: existing ? existing.payload : null
  });
  await loadJobs();
  try {
    await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/manifest/integrity/worker-request`, {
      method: "POST"
    });
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/manifest/integrity`);
    state.manifestIntegrity.set(jobId, { loading: false, error: "", payload });
  } catch (error) {
    state.manifestIntegrity.set(jobId, { loading: false, error: String(error.message || error), payload: null });
  }
  await loadJobs();
}

async function checkManifestFreezeReport(jobId) {
  state.manifestFreezeReports.set(jobId, { loading: true, error: "", payload: null });
  await loadJobs();
  try {
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/manifest/freeze-report`);
    state.manifestFreezeReports.set(jobId, { loading: false, error: "", payload });
  } catch (error) {
    state.manifestFreezeReports.set(jobId, { loading: false, error: String(error.message || error), payload: null });
  }
  await loadJobs();
}

async function loadShardAttempts(jobId, shardId, offset = 0) {
  const key = shardAttemptKey(jobId, shardId);
  const previous = state.shardAttempts.get(key) || {};
  const limit = Number(previous.limit || 100);
  state.shardAttempts.set(key, {
    open: true,
    loading: true,
    error: "",
    items: [],
    payload: previous.payload || null,
    limit,
    offset
  });
  await loadJobs();
  try {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/shards/${encodeURIComponent(shardId)}/attempts/page?${params.toString()}`);
    state.shardAttempts.set(key, {
      open: true,
      loading: false,
      error: "",
      items: payload.items || [],
      payload,
      limit,
      offset
    });
  } catch (error) {
    state.shardAttempts.set(key, {
      open: true,
      loading: false,
      error: String(error.message || error),
      items: [],
      payload: null,
      limit,
      offset
    });
  }
  await loadJobs();
}

async function loadJobLogs(jobId, offset = 0) {
  const previous = state.jobLogs.get(jobId) || {};
  const limit = Number(previous.limit || 100);
  state.jobLogs.set(jobId, {
    open: true,
    loading: true,
    error: "",
    payload: previous.payload || null,
    limit,
    offset
  });
  await loadJobs();
  try {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset)
    });
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/logs/page?${params.toString()}`);
    state.jobLogs.set(jobId, {
      open: true,
      loading: false,
      error: "",
      payload,
      limit,
      offset
    });
  } catch (error) {
    state.jobLogs.set(jobId, {
      open: true,
      loading: false,
      error: String(error.message || error),
      payload: null,
      limit,
      offset
    });
  }
  await loadJobs();
}

async function loadRecentErrors(jobId, offset = 0) {
  const previous = state.recentErrors.get(jobId) || {};
  const limit = Number(previous.limit || 100);
  state.recentErrors.set(jobId, {
    open: true,
    loading: true,
    error: "",
    payload: previous.payload || null,
    limit,
    offset
  });
  await loadJobs();
  try {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset)
    });
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/recent-errors/page?${params.toString()}`);
    state.recentErrors.set(jobId, {
      open: true,
      loading: false,
      error: "",
      payload,
      limit,
      offset
    });
  } catch (error) {
    state.recentErrors.set(jobId, {
      open: true,
      loading: false,
      error: String(error.message || error),
      payload: null,
      limit,
      offset
    });
  }
  await loadJobs();
}

async function registerServer() {
  const serverId = document.getElementById("serverId").value.trim();
  const name = document.getElementById("serverName").value.trim();
  const host = document.getElementById("serverHost").value.trim();
  const capacity = Number(document.getElementById("serverCapacity").value || 1);
  const capabilitiesText = document.getElementById("serverCapabilities").value.trim();
  let capabilities = {};
  if (capabilitiesText) {
    try {
      capabilities = JSON.parse(capabilitiesText);
    } catch (error) {
      setMessage(ui.serverMessage, `Failed to parse capabilities JSON: ${error.message}`, true);
      return;
    }
  }
  const payload = { id: serverId, name, host, capacity_slots: capacity, capabilities };
  await requestJson(`${SERVERS_API_ROOT}/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setMessage(ui.serverMessage, `Registered/updated server: ${serverId}`);
  await loadServers();
}

function buildModelPayload() {
  const profile = selectedModelProfile();
  const extraArgs = { ...((profile && profile.extra_args) || {}) };
  const numCpuWorkers = Number(document.getElementById("numCpuWorkers").value || 0);
  if (numCpuWorkers) {
    extraArgs.num_cpu_workers = numCpuWorkers;
  }
  const fileConcurrency = Number(document.getElementById("fileConcurrency").value || 0);
  if (fileConcurrency) {
    extraArgs.file_concurrency = fileConcurrency;
  }
  const apiConcurrencyStart = Number(document.getElementById("apiConcurrencyStart").value || 0);
  if (apiConcurrencyStart) {
    extraArgs.api_concurrency_start = apiConcurrencyStart;
  }
  const apiConcurrencyMax = Number(document.getElementById("apiConcurrencyMax").value || 0);
  if (apiConcurrencyMax) {
    extraArgs.api_concurrency_max = apiConcurrencyMax;
  }
  const apiAutotuneInterval = Number(document.getElementById("apiAutotuneInterval").value || 0);
  if (apiAutotuneInterval) {
    extraArgs.api_autotune_interval = apiAutotuneInterval;
  }
  if (document.getElementById("enableApiAutotune").checked) {
    extraArgs.enable_api_autotune = true;
  } else {
    delete extraArgs.enable_api_autotune;
  }

  return {
    engine: profile ? profile.engine : "dotsocr",
    ip: profile ? profile.ip : null,
    port: profile ? profile.port : null,
    model_name: profile ? profile.model_name : null,
    extra_args: extraArgs,
  };
}

const PREFLIGHT_ISSUE_LABELS = {
  model_profile_missing_api_key: "model profile API key is missing",
  model_profile_saved_api_key: "legacy model profile API key is stored in DB; clear it and migrate to api_key_env_var",
  mixed_worker_versions: "mixed worker versions",
  database_not_postgres: "database is not PostgreSQL",
  database_migrations_missing: "database migrations table is missing",
  database_migration_not_current: "database migrations are not current",
  control_api_auth_disabled: "control API auth is disabled",
  no_eligible_workers: "no eligible workers",
  output_path_not_writable: "output path is not writable",
  manifest_root_not_writable: "manifest root is not writable",
  resource_constrained_workers: "eligible workers report resource pressure",
  worker_event_spool_backlog: "eligible workers have local event/log spool backlog",
  worker_pending_shard_update_backlog: "eligible workers have local shard update backlog",
  high_detail_row_limits: "high detail row limits",
};

function buildJobRequestPayload(poolMode, allowedServerIds, serverId) {
  const modelPayload = buildModelPayload();
  const payload = {
    input_dir: document.getElementById("inputDir").value.trim(),
    output_dir: document.getElementById("outputDir").value.trim(),
    engine: modelPayload.engine,
    model_profile_id: ui.modelProfile.value,
    assigned_server_id: poolMode ? null : serverId,
    allowed_server_ids: poolMode && ui.workerScope.value === "selected" ? allowedServerIds : [],
    input_mode: selectedInputMode(),
    manifest_path: document.getElementById("manifestPath").value.trim() || null,
    manifest_root: document.getElementById("manifestRoot").value.trim() || null,
    target_files_per_shard: Number(document.getElementById("targetFilesPerShard").value || 1000),
    max_shard_attempts: Number(document.getElementById("maxShardAttempts").value || 3),
    ip: modelPayload.ip,
    port: modelPayload.port,
    model_name: modelPayload.model_name,
    page_concurrency: Number(document.getElementById("pageConcurrency").value || 0) || null,
    force_reprocess: false,
    engine_config: null,
    extra_args: modelPayload.extra_args,
  };
  const apiKey = ui.apiKey.value.trim();
  if (apiKey) {
    payload.extra_args.api_key = apiKey;
  }
  return payload;
}

function validateWorkerSelection(poolMode, allowedServerIds) {
  if (!poolMode && allowedServerIds.length !== 1) {
    throw new Error("Please select exactly one worker for single server mode.");
  }
  if (poolMode && ui.workerScope.value === "selected" && !allowedServerIds.length) {
    throw new Error("Please select at least one worker or choose all eligible workers.");
  }
}

function formatPreflightResult(preflight) {
  const issues = Array.isArray(preflight.issues) ? preflight.issues : [];
  if (!issues.length) {
    return `Preflight ok: ${preflight.eligible_workers || 0} eligible worker(s).`;
  }
  const rendered = issues.slice(0, 6).map((issue) => {
    const label = PREFLIGHT_ISSUE_LABELS[issue.code] || issue.code;
    return `${issue.severity}: ${label}`;
  }).join("; ");
  return `Preflight ${preflight.ok ? "warning" : "blocked"}: ${rendered}`;
}

async function runJobPreflight({ blockOnErrors = false } = {}) {
  const poolMode = isPoolInputMode();
  syncExecutionModeFields();
  const allowedServerIds = selectedAllowedServerIds();
  validateWorkerSelection(poolMode, allowedServerIds);
  const serverId = allowedServerIds[0] || "";
  const payload = buildJobRequestPayload(poolMode, allowedServerIds, serverId);
  const preflight = await requestJson(`${JOBS_API_ROOT}/preflight`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setMessage(ui.jobMessage, formatPreflightResult(preflight), !preflight.ok);
  if (blockOnErrors && !preflight.ok) {
    return null;
  }
  return payload;
}

async function createJob() {
  const payload = await runJobPreflight({ blockOnErrors: true });
  if (!payload) return;
  const poolMode = payload.assigned_server_id == null;
  const allowedServerIds = Array.isArray(payload.allowed_server_ids)
    ? payload.allowed_server_ids
    : [];
  const serverId = payload.assigned_server_id || "";

  let eligibilityMessage = "";
  if (payload.input_dir) {
    const eligibility = state.latestEligibility || await requestJson(`/api/servers/eligibility?input_dir=${encodeURIComponent(payload.input_dir)}`);
    const visible = eligibility.servers.filter((server) => server.server_id !== "__server_pool__");
    if (poolMode) {
      const scopedVisible = ui.workerScope.value === "selected"
        ? visible.filter((server) => allowedServerIds.includes(server.server_id))
        : visible;
      const eligibleCount = scopedVisible.filter((server) => server.can_access).length;
      if (eligibleCount === 0) {
        eligibilityMessage = ` Warning: no eligible server can confirm shared path access.`;
      } else {
        eligibilityMessage = ` Shared path check: ${eligibleCount}/${scopedVisible.length} worker(s) eligible for pool execution.`;
      }
    } else {
      const selected = visible.find((server) => server.server_id === serverId);
      if (!selected || !selected.can_access) {
        const reason = selected ? selected.reason : "server_not_found";
        eligibilityMessage = ` Warning: selected server cannot confirm shared path access (${reason}).`;
      } else {
        eligibilityMessage = ` Shared path check: selected server eligible.`;
      }
    }
  }

  const created = await requestJson(JOBS_API_ROOT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setMessage(ui.jobMessage, `Job created: ${created.id}.${eligibilityMessage}`);
  await loadJobs();
}

async function stopJob(jobId) {
  await requestJson(`/api/jobs/${jobId}/request-stop`, { method: "POST" });
  await loadJobs();
}

async function deleteJob(jobId) {
  await requestJson(`/api/jobs/${jobId}`, { method: "DELETE" });
  await loadJobs();
}

async function archiveJob(jobId) {
  await requestJson(`/api/jobs/${jobId}/archive`, { method: "POST" });
  await loadJobs();
}

async function archiveServer(serverId) {
  await requestJson(serverApiPath(serverId), { method: "DELETE" });
  await loadServers();
}

function bindEvents() {
  ui.controlApiToken.addEventListener("input", () => {
    const token = ui.controlApiToken.value.trim();
    if (token) {
      saveSessionToken(token);
      if (state.tokenRefreshTimer) clearTimeout(state.tokenRefreshTimer);
      state.tokenRefreshTimer = setTimeout(() => {
        refreshOperationsData().catch((error) => {
          setMessage(ui.jobsMessage, String(error.message || error), true);
        });
      }, 250);
    } else {
      clearSessionToken();
    }
  });
  ui.modelProfile.addEventListener("change", applyModelProfile);
  document.getElementById("saveModelProfileBtn").addEventListener("click", async () => {
    try {
      await saveModelProfile();
    } catch (error) {
      setMessage(ui.modelProfileMessage, String(error.message || error), true);
    }
  });
  document.getElementById("refreshModelProfilesBtn").addEventListener("click", async () => {
    await loadModelProfiles();
  });
  ui.executionMode.addEventListener("change", () => {
    syncExecutionModeFields();
    schedulePreflight();
    const mode = selectedInputMode();
    if (mode === "directory") {
      setMessage(ui.jobMessage, "Directory mode runs on the selected server.");
    } else if (mode === "remote_folder_snapshot") {
      setMessage(ui.jobMessage, "Remote folder snapshot lets an eligible server scan the shared path.");
    } else if (mode === "distributed_remote_folder_snapshot") {
      setMessage(ui.jobMessage, "Distributed manifest scan lets eligible servers split directory scanning.");
    } else {
      setMessage(ui.jobMessage, "Static shard mode uses the eligible server pool.");
    }
  });
  document.getElementById("inputDir").addEventListener("input", schedulePreflight);
  document.getElementById("targetFilesPerShard").addEventListener("input", schedulePreflight);
  ui.workerScope.addEventListener("change", () => {
    syncExecutionModeFields();
    schedulePreflight();
  });
  ui.singleWorkerSelect.addEventListener("change", schedulePreflight);
  ui.selectedWorkersList.addEventListener("change", schedulePreflight);
  ui.manualWorkerIds.addEventListener("input", schedulePreflight);
  document.getElementById("registerServerBtn").addEventListener("click", async () => {
    try {
      await registerServer();
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  document.getElementById("refreshServersBtn").addEventListener("click", async () => {
    try {
      await loadServers();
      setMessage(ui.serverMessage, "Server list refreshed");
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  document.getElementById("refreshDeploymentDoctorBtn").addEventListener("click", async () => {
    await loadDeploymentDoctor();
  });
  document.getElementById("refreshRemoteWorkerTargetsBtn").addEventListener("click", async () => {
    try {
      await loadRemoteWorkerTargets();
      setMessage(ui.serverMessage, "Remote worker targets refreshed");
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  ui.remoteWorkerTarget.addEventListener("change", () => {
    applyRemoteWorkerTarget(ui.remoteWorkerTarget.value);
  });
  document.getElementById("remoteWorkerPreflightBtn").addEventListener("click", async () => {
    try {
      await runRemoteWorkerPreflight();
      setMessage(ui.serverMessage, "Remote worker preflight completed");
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  document.getElementById("remoteWorkerInstallDryRunBtn").addEventListener("click", async () => {
    try {
      await runRemoteWorkerInstallDryRun();
      setMessage(ui.serverMessage, "Remote worker install dry-run completed");
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  document.getElementById("remoteWorkerInstallApplyBtn").addEventListener("click", async () => {
    try {
      await runRemoteWorkerInstallApply();
      setMessage(ui.serverMessage, "Remote worker install apply completed");
      await loadServers();
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  document.getElementById("remoteWorkerServiceBtn").addEventListener("click", async () => {
    try {
      await runRemoteWorkerServiceAction();
      setMessage(ui.serverMessage, "Remote worker service action completed");
      await loadServers();
    } catch (error) {
      setMessage(ui.serverMessage, String(error.message || error), true);
    }
  });
  document.getElementById("previewWorkerScaleBtn").addEventListener("click", async () => {
    try {
      await runWorkerScalePlan();
    } catch (error) {
      ui.workerScaleResult.innerHTML = `<span class="${statusClass("failed")}">${escapeHtml(String(error.message || error))}</span>`;
    }
  });
  document.getElementById("applyWorkerScaleBtn").addEventListener("click", async () => {
    try {
      await applyWorkerScale();
    } catch (error) {
      ui.workerScaleResult.innerHTML = `<span class="${statusClass("failed")}">${escapeHtml(String(error.message || error))}</span>`;
    }
  });
  document.getElementById("preflightJobBtn").addEventListener("click", async () => {
    try {
      await runJobPreflight();
    } catch (error) {
      setMessage(ui.jobMessage, String(error.message || error), true);
    }
  });
  document.getElementById("createJobBtn").addEventListener("click", async () => {
    try {
      await createJob();
    } catch (error) {
      setMessage(ui.jobMessage, String(error.message || error), true);
    }
  });
  ui.serversBody.addEventListener("click", async (event) => {
    const target = event.target;
    if (target.classList.contains("workerDetailsToggle") && target.dataset.id) {
      if (state.openWorkerDetails.has(target.dataset.id)) {
        state.openWorkerDetails.delete(target.dataset.id);
      } else {
        state.openWorkerDetails.add(target.dataset.id);
      }
      await loadServers();
      return;
    }
    if (target.classList.contains("scaleWorkerBtn") && target.dataset.id) {
      selectWorkerScaleTarget(target.dataset.id);
      return;
    }
    if (target.classList.contains("removeServerBtn") && target.dataset.id) {
      try {
        await archiveServer(target.dataset.id);
        setMessage(ui.serverMessage, `Removed server: ${target.dataset.id}`);
      } catch (error) {
        setMessage(ui.serverMessage, String(error.message || error), true);
      }
    }
  });
  document.getElementById("refreshJobsBtn").addEventListener("click", async () => {
    try {
      await loadJobs();
      setMessage(ui.jobsMessage, "Jobs list refreshed");
    } catch (error) {
      setMessage(ui.jobsMessage, String(error.message || error), true);
    }
  });
  ui.jobStatusFilter.addEventListener("change", async () => {
    state.jobsPage.status = ui.jobStatusFilter.value;
    state.jobsPage.offset = 0;
    await loadJobs();
  });
  ui.jobPageSize.addEventListener("change", async () => {
    state.jobsPage.limit = Number(ui.jobPageSize.value || 50);
    state.jobsPage.offset = 0;
    await loadJobs();
  });
  ui.includeArchivedJobs.addEventListener("change", async () => {
    state.jobsPage.includeArchived = ui.includeArchivedJobs.checked;
    state.jobsPage.offset = 0;
    await loadJobs();
  });
  ui.previousJobsBtn.addEventListener("click", async () => {
    state.jobsPage.offset = Math.max(0, state.jobsPage.offset - state.jobsPage.limit);
    await loadJobs();
  });
  ui.nextJobsBtn.addEventListener("click", async () => {
    state.jobsPage.offset += state.jobsPage.limit;
    await loadJobs();
  });
  ui.jobsBody.addEventListener("click", async (event) => {
    const target = event.target;
    if (target.classList.contains("jobDetailsToggle") && target.dataset.id) {
      if (state.openJobDetails.has(target.dataset.id)) {
        state.openJobDetails.delete(target.dataset.id);
      } else {
        state.openJobDetails.add(target.dataset.id);
      }
      await loadJobs();
      return;
    }
    if (target.classList.contains("inspectShardsBtn") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.open = true;
      inspector.offset = 0;
      await loadShardInspector(target.dataset.id);
      return;
    }
    if (target.classList.contains("refreshShardsBtn") && target.dataset.id) {
      await loadShardInspector(target.dataset.id);
      return;
    }
    if (target.classList.contains("closeShardsBtn") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.open = false;
      await loadJobs();
      return;
    }
    if (target.classList.contains("shardPageBtn") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      const direction = target.dataset.direction;
      const nextOffset = direction === "next"
        ? inspector.offset + inspector.limit
        : Math.max(0, inspector.offset - inspector.limit);
      inspector.offset = nextOffset;
      await loadShardInspector(target.dataset.id);
      return;
    }
    if (target.classList.contains("checkManifestIntegrityBtn") && target.dataset.id) {
      await checkManifestIntegrity(target.dataset.id);
      return;
    }
    if (target.classList.contains("requestWorkerManifestIntegrityBtn") && target.dataset.id) {
      await requestWorkerManifestIntegrity(target.dataset.id);
      return;
    }
    if (target.classList.contains("checkManifestFreezeBtn") && target.dataset.id) {
      await checkManifestFreezeReport(target.dataset.id);
      return;
    }
    if (target.classList.contains("showAttemptsBtn") && target.dataset.jobId && target.dataset.shardId) {
      await loadShardAttempts(target.dataset.jobId, target.dataset.shardId, 0);
      return;
    }
    if (target.classList.contains("attemptPageBtn") && target.dataset.jobId && target.dataset.shardId) {
      const limit = Number(target.dataset.limit || 100);
      const offset = Number(target.dataset.offset || 0);
      const direction = target.dataset.direction;
      const nextOffset = direction === "next"
        ? offset + limit
        : Math.max(0, offset - limit);
      await loadShardAttempts(target.dataset.jobId, target.dataset.shardId, nextOffset);
      return;
    }
    if (target.classList.contains("showJobLogsBtn") && target.dataset.id) {
      await loadJobLogs(target.dataset.id, 0);
      return;
    }
    if (target.classList.contains("logPageBtn") && target.dataset.id) {
      const limit = Number(target.dataset.limit || 100);
      const offset = Number(target.dataset.offset || 0);
      const direction = target.dataset.direction;
      const nextOffset = direction === "next"
        ? offset + limit
        : Math.max(0, offset - limit);
      await loadJobLogs(target.dataset.id, nextOffset);
      return;
    }
    if (target.classList.contains("showRecentErrorsBtn") && target.dataset.id) {
      await loadRecentErrors(target.dataset.id, 0);
      return;
    }
    if (target.classList.contains("recentErrorPageBtn") && target.dataset.id) {
      const limit = Number(target.dataset.limit || 100);
      const offset = Number(target.dataset.offset || 0);
      const direction = target.dataset.direction;
      const nextOffset = direction === "next"
        ? offset + limit
        : Math.max(0, offset - limit);
      await loadRecentErrors(target.dataset.id, nextOffset);
      return;
    }
    if (target.classList.contains("stopBtn") && target.dataset.id) {
      try {
        await stopJob(target.dataset.id);
      } catch (error) {
        setMessage(ui.jobsMessage, String(error.message || error), true);
      }
    }
    if (target.classList.contains("archiveJobBtn") && target.dataset.id) {
      try {
        await archiveJob(target.dataset.id);
        setMessage(ui.jobsMessage, `Archived job: ${target.dataset.id}`);
      } catch (error) {
        setMessage(ui.jobsMessage, String(error.message || error), true);
      }
    }
    if (target.classList.contains("deleteBtn") && target.dataset.id) {
      try {
        await deleteJob(target.dataset.id);
        setMessage(ui.jobsMessage, `Deleted job: ${target.dataset.id}`);
      } catch (error) {
        setMessage(ui.jobsMessage, String(error.message || error), true);
      }
    }
  });
  ui.jobsBody.addEventListener("change", async (event) => {
    const target = event.target;
    if (target.classList.contains("shardStatusSelect") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.status = target.value;
      inspector.offset = 0;
      await loadShardInspector(target.dataset.id);
    }
    if (target.classList.contains("shardWorkerFilter") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.workerId = target.value.trim();
      inspector.offset = 0;
      await loadShardInspector(target.dataset.id);
    }
    if (target.classList.contains("shardFailureCategoryFilter") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.failureCategory = target.value.trim();
      inspector.offset = 0;
      await loadShardInspector(target.dataset.id);
    }
    if (target.classList.contains("shardMinAttemptsFilter") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.minAttemptCount = target.value.trim();
      inspector.offset = 0;
      await loadShardInspector(target.dataset.id);
    }
    if (target.classList.contains("shardRunningLongerFilter") && target.dataset.id) {
      const inspector = shardInspectorState(target.dataset.id);
      inspector.runningLongerThanSeconds = target.value.trim();
      inspector.offset = 0;
      await loadShardInspector(target.dataset.id);
    }
  });
}

async function init() {
  ui.controlApiToken.value = loadSessionToken();
  await loadModelProfiles();
  syncExecutionModeFields();
  renderPreflight(null);
  bindEvents();
  await refreshOperationsData({ quiet: !ui.controlApiToken.value.trim() });
  loadRemoteWorkerTargets().catch(() => {});
  state.refreshTimer = setInterval(async () => {
    await refreshOperationsData().catch((error) => {
      setMessage(ui.jobsMessage, String(error.message || error), true);
    });
  }, REFRESH_MS);
}

window.addEventListener("beforeunload", () => {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
  }
  if (state.tokenRefreshTimer) {
    clearTimeout(state.tokenRefreshTimer);
  }
});

init();
