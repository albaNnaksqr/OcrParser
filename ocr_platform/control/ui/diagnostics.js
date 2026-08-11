export const DATABASE_STATUS_API = "/api/system/database";
export const DIAGNOSTICS_API = "/api/system/diagnostics";

const MIGRATION_COMMAND = "ocr-platform-migrate status && ocr-platform-migrate plan && ocr-platform-migrate apply && ocr-platform-migrate verify";

const REMEDIATIONS = {
  database_not_postgres: {
    title: "Production database is not PostgreSQL",
    impact: "SQLite is intended for local development and does not provide the production claim and migration guarantees.",
    action: "Configure OCR_PLATFORM_DATABASE_URL for PostgreSQL 16, then run the migration workflow before accepting jobs.",
    command: MIGRATION_COMMAND,
  },
  database_migrations_missing: {
    title: "Migration history is missing",
    impact: "Control cannot prove that the database schema matches this release.",
    action: "Plan and apply the repository migration catalog, then verify every checksum.",
    command: MIGRATION_COMMAND,
  },
  database_migration_not_current: {
    title: "Database migrations are not current",
    impact: "Business APIs may be unavailable or operate against an incompatible schema.",
    action: "Review the pending plan, apply it once, and verify the resulting checksums.",
    command: MIGRATION_COMMAND,
  },
  api_auth_disabled: {
    title: "Control API authentication is disabled",
    impact: "Any client with network access can call operational APIs.",
    action: "Set OCR_PLATFORM_API_TOKEN and OCR_PLATFORM_REQUIRE_API_TOKEN=1, then restart Control through the deployment procedure.",
  },
  no_workers: {
    title: "No workers are registered",
    impact: "Jobs can be created but no Agent can scan or claim work.",
    action: "Deploy or start at least one Agent, then confirm its heartbeat in Workers.",
    route: "workers",
  },
  no_ready_workers: {
    title: "No workers are ready",
    impact: "Registered Agents are offline, stale, blocked by path access, or resource constrained.",
    action: "Open Workers and inspect heartbeat age, shared paths, version, capacity, and spool status.",
    route: "workers",
  },
  resource_constrained_workers: {
    title: "Workers report resource pressure",
    impact: "New shards may wait or run below the expected throughput.",
    action: "Open Workers and relieve CPU, memory, disk, file-descriptor, or API-capacity pressure before scaling the queue.",
    route: "workers",
  },
  shared_root_unavailable: {
    title: "Workers have no confirmed shared root",
    impact: "Control cannot safely assign distributed scan or output work to these Agents.",
    action: "Open Workers and verify that the same shared root is mounted and reported on every eligible host.",
    route: "workers",
  },
  worker_spool_backlog: {
    title: "Worker spool backlog requires attention",
    impact: "Events, logs, or shard updates have not yet reached Control.",
    action: "Restore Control connectivity and confirm replay drains the backlog without quarantine records.",
    route: "workers",
  },
};

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function normalizedIssues(payload) {
  const issues = Array.isArray(payload?.issues) ? [...payload.issues] : [];
  const workers = payload?.workers || {};
  if (Number(workers.total || 0) > 0 && Number(workers.with_shared_roots || 0) === 0) {
    issues.push({ code: "shared_root_unavailable", severity: "warning" });
  }
  const activeAlerts = Array.isArray(payload?.alerts?.active) ? payload.alerts.active : [];
  if (activeAlerts.some((item) => [
    "event_spool_backlog", "log_spool_backlog", "shard_update_spool_backlog"
  ].includes(item.code))) {
    issues.push({ code: "worker_spool_backlog", severity: "warning" });
  }
  const unique = new Map();
  issues.forEach((issue) => {
    if (issue?.code && !unique.has(issue.code)) unique.set(issue.code, issue);
  });
  return Array.from(unique.values());
}

function issueCard(issue) {
  const remediation = REMEDIATIONS[issue.code] || {
    title: issue.code || "Deployment issue",
    impact: issue.message || "Control reported an operational condition that needs review.",
    action: "Review diagnostics and the deployment guide before creating new work.",
  };
  const route = remediation.route
    ? `<a class="button-link" href="#${escapeHtml(remediation.route)}">Open ${escapeHtml(remediation.route)}</a>`
    : "";
  const command = remediation.command
    ? `<code class="doctor-command">${escapeHtml(remediation.command)}</code>`
    : "";
  return `<article class="doctor-issue ${issue.severity === "error" ? "error" : "warning"}">
    <div>
      <span class="status ${issue.severity === "error" ? "failed" : "warning"}">${escapeHtml(issue.severity || "warning")}</span>
      <h3>${escapeHtml(remediation.title)}</h3>
      <p class="small">${escapeHtml(issue.code || "diagnostic")}</p>
    </div>
    <div class="doctor-action">
      <p><strong>Impact:</strong> ${escapeHtml(remediation.impact)}</p>
      <p><strong>Recommended action:</strong> ${escapeHtml(remediation.action)}</p>
      ${command}${route}
    </div>
  </article>`;
}

function renderCapacity(capacity = {}) {
  const target = document.getElementById("systemCapacity");
  if (!target) return;
  const eta = capacity.estimated_drain_seconds == null
    ? "not available"
    : `${Math.round(Number(capacity.estimated_drain_seconds))}s`;
  target.innerHTML = [
    [capacity.ready_worker_slots || 0, "ready slots"],
    [capacity.available_worker_slots || 0, "available slots"],
    [capacity.pending_shard_queue_depth || 0, "pending shards"],
    [capacity.observed_pages_per_hour || 0, "pages/hour"],
    [eta, "estimated drain"],
    [capacity.confidence || "none", "confidence"],
  ].map(([value, label]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span class="small">${escapeHtml(label)}</span></div>`).join("");
}

function renderAlerts(alerts = {}) {
  const target = document.getElementById("systemAlerts");
  if (!target) return;
  const active = Array.isArray(alerts.active) ? alerts.active : [];
  target.innerHTML = active.length
    ? active.map((item) => `<div class="doctor-issue ${item.severity === "error" ? "error" : "warning"}"><div><strong>${escapeHtml(item.code)}</strong><div class="small">${escapeHtml(item.severity || "warning")}</div></div><div class="doctor-action">${escapeHtml(item.recommendation_code || "review_diagnostics")} · ${escapeHtml(item.count || 0)} finding(s)</div></div>`).join("")
    : `<div class="metric"><strong>0</strong><span class="small">active alerts</span></div>`;
}

function renderAudit(audit = {}) {
  const target = document.getElementById("systemAudit");
  if (!target) return;
  const manifest = audit.manifest_integrity || {};
  const artifacts = audit.artifacts || {};
  const execution = audit.execution || {};
  const stageFailures = Object.values(execution.stage_failure_category_counts || {})
    .reduce((total, count) => total + Number(count || 0), 0);
  target.innerHTML = [
    [manifest.reports_present || 0, "manifest reports"],
    [manifest.reports_missing || 0, "missing reports"],
    [artifacts.missing_declared_records || 0, "missing artifacts"],
    [stageFailures, "stage failures"],
    [audit.output_audit?.status || "not reported", "output audit"],
  ].map(([value, label]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span class="small">${escapeHtml(label)}</span></div>`).join("");
}

export function renderGuidedDiagnostics(payload) {
  const target = document.getElementById("deploymentDoctorIssues");
  if (target) {
    const issues = normalizedIssues(payload);
    target.innerHTML = issues.length
      ? issues.map(issueCard).join("")
      : `<div class="preflight-banner ready"><strong>No blocking deployment findings</strong><span>Refresh after infrastructure or configuration changes.</span></div>`;
  }
  renderCapacity(payload?.capacity || {});
  renderAlerts(payload?.alerts || {});
  renderAudit(payload?.audit || {});
}

export function clearGuidedDiagnostics(message = "Diagnostics are unavailable.") {
  const target = document.getElementById("deploymentDoctorIssues");
  if (target) {
    target.innerHTML = `<div class="preflight-banner blocked"><strong>Unable to load diagnostics</strong><span>${escapeHtml(message)}</span></div>`;
  }
  renderCapacity({});
  renderAlerts({});
  renderAudit({});
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
export function createDiagnosticsModule(app) {
  const { state, ui, requestJson } = app;
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
    const visible = app.visibleServers();
    const readiness = visible.map((server) => app.classifyWorkerReadiness(server));
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
      `<strong>${app.escapeHtml(running)}</strong>`,
      `<span class="summary-note">${app.escapeHtml(queued)} queued jobs</span>`,
    ].join("");
    ui.opsAttentionSummary.innerHTML = [
      `<span class="summary-label">Needs attention</span>`,
      `<strong>${app.escapeHtml(attention)}</strong>`,
      `<span class="summary-note">retrying, failed, or stale work</span>`,
    ].join("");
    ui.opsWorkerSummary.innerHTML = [
      `<span class="summary-label">Workers</span>`,
      `<strong>${app.escapeHtml(ready)}/${app.escapeHtml(workerTotal)}</strong>`,
      `<span class="summary-note">ready / visible workers</span>`,
    ].join("");
    ui.opsReadinessSummary.innerHTML = [
      `<span class="summary-label">Readiness</span>`,
      `<strong><span class="${app.statusClass(doctor && doctor.ok ? "succeeded" : "warning")}">${app.escapeHtml(doctorText)}</span></strong>`,
      `<span class="summary-note">${app.escapeHtml(doctorNote)}</span>`,
    ].join("");
  }

  function renderDatabaseStatus(payload, error = "") {
    if (error) {
      ui.databaseStatus.innerHTML = `<div class="metric"><strong>error</strong><span class="small">${app.escapeHtml(error)}</span></div>`;
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
      `<div class="metric"><strong>${app.escapeHtml(payload.dialect || "unknown")}</strong><span class="small">dialect</span></div>`,
      `<div class="metric"><strong><span class="${app.statusClass(latestClass)}">${app.escapeHtml(latest)}</span></strong><span class="small">latest_applied_migration</span></div>`,
      `<div class="metric"><strong><span class="${app.statusClass(currentClass)}">${app.escapeHtml(currentStatus)}</span></strong><span class="small">migration currency</span></div>`,
      `<div class="metric"><strong><span class="${app.statusClass(tableClass)}">${app.escapeHtml(tableStatus)}</span></strong><span class="small">schema_migrations table</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(appliedMigrations.length)}</strong><span class="small">applied migrations</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(knownMigrations.length)}</strong><span class="small">known migrations</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(missingMigrations.length)}</strong><span class="small">missing migrations</span></div>`,
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

  function renderDeploymentDoctor(payload, error = "") {
    if (error) {
      ui.deploymentDoctorStatus.innerHTML = `<div class="metric"><strong>error</strong><span class="small">${app.escapeHtml(error)}</span></div>`;
      clearGuidedDiagnostics(error);
      renderOperationsSummary();
      return;
    }
    if (!payload) {
      ui.deploymentDoctorStatus.innerHTML = `<div class="metric"><strong>-</strong><span class="small">readiness</span></div>`;
      clearGuidedDiagnostics("Deployment diagnostics have not loaded yet.");
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
      `<div class="metric"><strong><span class="${app.statusClass(readinessClass)}">${app.escapeHtml(readinessText)}</span></strong><span class="small">readiness</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(database.dialect || "unknown")}</strong><span class="small">database</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(workers.ready || 0)}/${app.escapeHtml(workers.total || 0)}</strong><span class="small">ready workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(workers.with_shared_roots || 0)}</strong><span class="small">shared-root workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(apiAuth.enabled ? "enabled" : "disabled")}</strong><span class="small">API auth</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(issues.length)}</strong><span class="small">${app.escapeHtml(issueText)}</span></div>`,
    ].join("");
    renderGuidedDiagnostics(payload);
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

  async function refreshOperationsData({ quiet = false } = {}) {
    const results = await Promise.allSettled([
      loadDatabaseStatus(),
      loadDeploymentDoctor(),
      app.loadServers(),
      app.loadJobs(),
    ]);
    const errors = results
      .filter((result) => result.status === "rejected")
      .map((result) => result.reason && result.reason.message ? result.reason.message : String(result.reason));
    if (errors.length) {
      if (!quiet) app.setMessage(ui.jobsMessage, errors.join("; "), true);
      return false;
    }
    app.setMessage(ui.jobsMessage, "");
    return true;
  }

  return { jobNeedsAttention, renderOperationsSummary, renderDatabaseStatus, loadDatabaseStatus, renderDeploymentDoctor, loadDeploymentDoctor, deploymentDoctorJobIssues, refreshOperationsData };
}
