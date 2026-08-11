const STEP_COUNT = 4;

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function closestRow(id) {
  const element = document.getElementById(id);
  return element ? element.closest(".row") : null;
}

function appendUnique(panel, elements) {
  const seen = new Set();
  elements.filter(Boolean).forEach((element) => {
    if (!seen.has(element)) {
      seen.add(element);
      panel.append(element);
    }
  });
}

function selectedWorkerText() {
  const scope = document.getElementById("workerScope")?.value || "all_eligible";
  if (scope === "all_eligible") return "All eligible workers";
  const selected = Array.from(
    document.querySelectorAll('#selectedWorkersList input[type="checkbox"]:checked')
  ).map((item) => item.value);
  const manual = (document.getElementById("manualWorkerIds")?.value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return [...new Set([...selected, ...manual])].join(", ") || "No workers selected";
}

export function createJobWizard({ onCancel = () => {} } = {}) {
  const form = document.getElementById("jobForm");
  const review = document.getElementById("wizardReview");
  const backButton = document.getElementById("wizardBackBtn");
  const nextButton = document.getElementById("wizardNextBtn");
  const createButton = document.getElementById("createJobBtn");
  let step = 1;
  let preflight = null;
  let panels = [];

  function makePanel(number, title, description) {
    const panel = document.createElement("section");
    panel.className = "wizard-panel";
    panel.dataset.wizardStep = String(number);
    panel.innerHTML = `<div class="wizard-panel-heading"><div><h3>${escapeHtml(title)}</h3><p class="small">${escapeHtml(description)}</p></div></div>`;
    return panel;
  }

  function organizeForm() {
    if (!form || form.dataset.wizardReady === "1") return;
    form.dataset.wizardReady = "1";
    const inputPanel = makePanel(1, "Input", "Choose the execution mode and shared input/output paths.");
    const enginePanel = makePanel(2, "Engine", "Select a reusable profile and keep overrides explicit.");
    const workerPanel = makePanel(3, "Workers & Capacity", "Confirm worker scope, shared-path access, and available capacity.");
    const reviewPanel = makePanel(4, "Review & Create", "Run the backend preflight before creating the job.");
    const advanced = Array.from(form.querySelectorAll("details.advanced"))
      .find((item) => item.id !== "modelProfileEditor");
    const inputMode = document.getElementById("inputMode")?.closest("div.hidden");
    const preflightMetrics = document.getElementById("preflightMetrics");
    const actionToolbar = document.getElementById("preflightJobBtn")?.closest(".toolbar");

    appendUnique(inputPanel, [
      closestRow("executionMode"),
      closestRow("inputDir"),
      document.getElementById("manifestPathField"),
      inputMode,
    ]);
    appendUnique(enginePanel, [
      closestRow("modelProfile"),
      document.getElementById("resolvedModelConfig"),
      advanced,
    ]);
    const manage = document.createElement("a");
    manage.className = "inline-link";
    manage.href = "#system";
    manage.textContent = "Manage model profiles in System";
    enginePanel.append(manage);
    appendUnique(workerPanel, [closestRow("workerScope"), preflightMetrics]);
    appendUnique(reviewPanel, [review, actionToolbar]);
    panels = [inputPanel, enginePanel, workerPanel, reviewPanel];
    panels.forEach((panel) => form.insertBefore(panel, backButton.parentElement));
  }

  function validateStep() {
    if (step === 1) {
      const required = ["inputDir", "outputDir"];
      if (document.getElementById("executionMode")?.value === "existing_manifest") {
        required.push("manifestPath");
      }
      for (const id of required) {
        const input = document.getElementById(id);
        if (!input || !input.value.trim()) {
          input?.setCustomValidity("This field is required for the selected mode.");
          input?.reportValidity();
          input?.setCustomValidity("");
          return false;
        }
      }
    }
    if (step === 2 && !document.getElementById("modelProfile")?.value) {
      document.getElementById("modelProfile")?.reportValidity();
      return false;
    }
    if (step === 3 && document.getElementById("workerScope")?.value === "selected") {
      const workers = selectedWorkerText();
      if (workers === "No workers selected") {
        document.getElementById("manualWorkerIds")?.setCustomValidity("Select at least one worker or use all eligible workers.");
        document.getElementById("manualWorkerIds")?.reportValidity();
        document.getElementById("manualWorkerIds")?.setCustomValidity("");
        return false;
      }
    }
    return true;
  }

  function reviewRows() {
    const profile = document.getElementById("modelProfile");
    const profileLabel = profile?.selectedOptions?.[0]?.textContent || profile?.value || "-";
    const apiKey = Boolean(document.getElementById("apiKey")?.value.trim());
    return [
      ["Execution mode", document.getElementById("executionMode")?.selectedOptions?.[0]?.textContent || "-"],
      ["Input", document.getElementById("inputDir")?.value || "-"],
      ["Output", document.getElementById("outputDir")?.value || "-"],
      ["Manifest", document.getElementById("manifestPath")?.value || document.getElementById("manifestRoot")?.value || "auto"],
      ["Files per shard", document.getElementById("targetFilesPerShard")?.value || "1000"],
      ["Model profile", profileLabel],
      ["Workers", selectedWorkerText()],
      ["API key override", apiKey ? "Provided for this request only" : "Not provided"],
    ];
  }

  function renderReview() {
    if (!review) return;
    const rows = reviewRows().map(([label, value]) => (
      `<div class="review-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )).join("");
    const issues = Array.isArray(preflight?.issues) ? preflight.issues : [];
    const status = preflight
      ? `<div class="preflight-banner ${preflight.ok ? "ready" : "blocked"}"><strong>${preflight.ok ? "Preflight passed" : "Preflight blocked"}</strong><span>${issues.length ? `${issues.length} issue(s) reported` : "No issues reported"}</span></div>`
      : `<div class="preflight-banner pending"><strong>Preflight required</strong><span>Review the draft, then run the backend preflight.</span></div>`;
    review.innerHTML = `<div class="review-grid">${rows}</div>${status}`;
  }

  function render() {
    panels.forEach((panel, index) => { panel.hidden = index + 1 !== step; });
    document.querySelectorAll("[data-wizard-indicator]").forEach((item) => {
      const number = Number(item.dataset.wizardIndicator);
      item.classList.toggle("complete", number < step);
      if (number === step) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    backButton.disabled = step === 1;
    nextButton.hidden = step === STEP_COUNT;
    if (step === STEP_COUNT) renderReview();
    const active = panels[step - 1]?.querySelector("h3");
    if (active) {
      active.setAttribute("tabindex", "-1");
      active.focus({ preventScroll: true });
    }
  }

  function setStep(nextStep) {
    step = Math.max(1, Math.min(STEP_COUNT, Number(nextStep) || 1));
    render();
  }

  function invalidatePreflight() {
    preflight = null;
    createButton.disabled = true;
    if (step === STEP_COUNT) renderReview();
  }

  function setPreflight(payload) {
    preflight = payload || null;
    createButton.disabled = !preflight?.ok;
    setStep(STEP_COUNT);
  }

  function clearSecret() {
    const apiKey = document.getElementById("apiKey");
    if (apiKey) apiKey.value = "";
    invalidatePreflight();
  }

  function resetAfterCreate() {
    clearSecret();
    setStep(1);
  }

  function init() {
    organizeForm();
    backButton.addEventListener("click", () => setStep(step - 1));
    nextButton.addEventListener("click", () => {
      if (validateStep()) setStep(step + 1);
    });
    document.getElementById("cancelJobWizardBtn")?.addEventListener("click", () => {
      clearSecret();
      onCancel();
    });
    form.addEventListener("input", (event) => {
      if (event.target?.id !== "controlApiToken") invalidatePreflight();
    });
    form.addEventListener("change", invalidatePreflight);
    render();
  }

  return {
    clearSecret,
    init,
    invalidatePreflight,
    renderReview,
    resetAfterCreate,
    setPreflight,
    setStep,
  };
}


import { JOBS_API_ROOT } from "./jobs.js";
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
export function createJobOperationsModule(app) {
  const { state, ui, requestJson } = app;
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

  function renderPreflightDiagnosis(eligibility) {
    if (!eligibility) return "";
    const byId = new Map(eligibility.servers.map((item) => [item.server_id, item]));
    const visible = app.visibleServers();
    if (!visible.length) {
      return `<div class="diagnosis small">no visible workers registered</div>`;
    }
    const readiness = visible.map((server) => app.classifyWorkerReadiness(server, byId.get(server.id)));
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
    return lines.length ? `<div class="diagnosis small">${app.escapeHtml(lines.join(" | "))}</div>` : `<div class="diagnosis small">all visible workers are ready for this path</div>`;
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
    const profile = app.selectedModelProfile();
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
    const workerReadiness = app.visibleServers().map((server) => app.classifyWorkerReadiness(server, eligibilityById.get(server.id)));
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
      `<div class="metric"><strong>${app.escapeHtml(pathStatus)}</strong><span class="small">shared path</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(scopedEligible.length)}</strong><span class="small">eligible workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(plannedApi)}</strong><span class="small">planned API concurrency</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(runtime.running_shards)}</strong><span class="small">running shards</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(runtime.api_inflight)}</strong><span class="small">API inflight</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(runtime.api_waiting)}</strong><span class="small">API waiting</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(app.formatNumber(runtime.oldest_api_inflight, 1))}s</strong><span class="small">oldest API inflight</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(ready.length)}</strong><span class="small">Ready workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(warning.length)}</strong><span class="small">Warning workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(blocked.length)}</strong><span class="small">Blocked workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(shardText)}</strong><span class="small">shard plan</span></div>`,
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
      app.setMessage(ui.jobMessage, "");
    } catch (error) {
      app.setMessage(ui.jobMessage, String(error.message || error), true);
    }
  }

  function schedulePreflight() {
    if (state.preflightTimer) clearTimeout(state.preflightTimer);
    state.preflightTimer = setTimeout(updatePreflight, 250);
  }

  function buildModelPayload() {
    const profile = app.selectedModelProfile();
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
    const poolMode = app.isPoolInputMode();
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
    app.setMessage(ui.jobMessage, formatPreflightResult(preflight), !preflight.ok);
    app.jobWizard?.setPreflight(preflight);
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
    app.setMessage(ui.jobMessage, `Job created: ${created.id}.${eligibilityMessage}`);
    state.openJobDetails.add(created.id);
    app.jobWizard?.resetAfterCreate();
    app.navigation?.navigate("jobs");
    await app.loadJobs();
  }

  return { selectedAllowedServerIds, selectedServerId, selectedInputMode, setRawInputMode, syncExecutionModeFields, renderPreflightDiagnosis, inferDefaultManifestRoot, applyDefaultManifestRoot, perWorkerApiConcurrencyMax, activeShardRuntimeMetrics, renderPreflight, updatePreflight, schedulePreflight, buildModelPayload, buildJobRequestPayload, validateWorkerSelection, formatPreflightResult, runJobPreflight, createJob };
}
