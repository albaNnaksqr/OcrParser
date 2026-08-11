export const JOBS_API_ROOT = "/api/jobs";

export function jobApiPath(jobId, suffix = "") {
  return `${JOBS_API_ROOT}/${encodeURIComponent(jobId)}${suffix}`;
}


export function createJobsModule(app) {
  const { state, ui, requestJson } = app;
  function rowProgress(job) {
    const fileText = `${job.completed_files || 0}/${job.total_files || 0} files`;
    const completedPages = Number(job.completed_pages || 0);
    const pageText = job.total_pages == null
      ? `${completedPages} ${completedPages === 1 ? "page" : "pages"} done`
      : `${completedPages}/${job.total_pages} pages`;
    const percent = Number(job.progress_percent || 0);
    const width = Math.max(0, Math.min(percent, 100));
    const pct = job.progress_percent == null ? "" : `${app.formatNumber(job.progress_percent)}%`;
    return `<div class="cell-stack">
      <div class="cell-row"><span class="primary-text">${app.escapeHtml(fileText)}</span><span class="small">${app.escapeHtml(pct)}</span></div>
      <div class="small">${app.escapeHtml(pageText)}</div>
      <div class="progress-track"><div class="progress-fill" style="width: ${app.escapeHtml(width)}%"></div></div>
    </div>`;
  }

  function rowShards(job) {
    const scanUnits = Number(job.total_scan_units || 0);
    const scanLine = scanUnits
      ? `<div class="small">scan ${app.escapeHtml(`${job.succeeded_scan_units || 0}/${scanUnits} done; pending ${job.pending_scan_units || 0}; running ${job.running_scan_units || 0}; stale ${job.stale_scan_units || 0}`)}</div>`
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
      ? `<div><span class="${app.statusClass(job.recovery_status)}">${app.escapeHtml(job.recovery_status)}</span></div>`
      : "";
    return `${app.escapeHtml(done)}${recovery}<div class="small">${app.escapeHtml(counts)}</div>${scanLine}`;
  }

  function rowWorkerSummary(job) {
    const workers = (job.worker_shards || []).filter((item) => item.server_id);
    if (!workers.length) {
      const selectedCount = Array.isArray(job.allowed_server_ids) ? job.allowed_server_ids.length : 0;
      const label = job.assigned_server_id
        ? job.assigned_server_id
        : (selectedCount ? `${selectedCount} selected` : "server pool");
      return `<div class="cell-stack">
        <div class="primary-text">${app.escapeHtml(label)}</div>
        <div class="small">no shards generated</div>
      </div>`;
    }
    const active = workers.filter((item) => Number(item.running_shards || 0) > 0).length;
    const stale = workers.reduce((sum, item) => sum + Number(item.stale_shards || 0), 0);
    const failed = workers.reduce((sum, item) => sum + Number(item.failed_shards || 0), 0);
    const workerLabel = workers.length === 1 ? "worker" : "workers";
    return `<div class="cell-stack">
      <div class="primary-text">${app.escapeHtml(workers.length)} ${workerLabel}</div>
      <div class="small">${app.escapeHtml(active)} active; ${app.escapeHtml(stale)} stale; ${app.escapeHtml(failed)} failed</div>
    </div>`;
  }

  function rowThroughput(job) {
    const pages = job.pages_per_second == null ? "-" : `${app.formatNumber(job.pages_per_second, 3)} page/s`;
    const files = job.files_per_minute == null ? "-" : `${app.formatNumber(job.files_per_minute, 2)} file/min`;
    const eta = job.eta_seconds == null ? "" : `<div class="small">ETA ${app.formatDuration(job.eta_seconds)}</div>`;
    return `${app.escapeHtml(pages)}<div class="small">${app.escapeHtml(files)}</div>${eta}`;
  }

  function formatFailureCategoryCounts(label, counts) {
    if (!counts || typeof counts !== "object") return "";
    const entries = Object.entries(counts)
      .filter(([_category, count]) => Number(count) > 0)
      .sort((left, right) => Number(right[1]) - Number(left[1]) || String(left[0]).localeCompare(String(right[0])));
    if (!entries.length) return "";
    const rendered = entries
      .slice(0, 4)
      .map(([category, count]) => `<span class="status failed">${app.escapeHtml(category)} ${app.escapeHtml(count)}</span>`)
      .join("");
    const suffix = entries.length > 4 ? `<span class="small">+${app.escapeHtml(entries.length - 4)} more</span>` : "";
    return `<div class="small">${app.escapeHtml(label)}</div><div class="cell-row">${rendered}${suffix}</div>`;
  }

  function formatFreshness(job) {
    const eventAt = job.last_heartbeat_at || job.last_event_at;
    const stale = job.is_stale ? `<span class="status stale">stale</span>` : "";
    if (!eventAt) {
      return `${stale}<div class="small">No events yet</div>`;
    }
    const date = new Date(eventAt);
    return `${stale}<div class="small">${app.escapeHtml(date.toLocaleString())}</div>`;
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
      const runningSeconds = app.shardRunningSeconds(shard);
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
    const productionIssues = app.deploymentDoctorJobIssues();
    const active = ["queued", "running", "stopping"].includes(job.status);
    if (active && productionIssues.length) {
      const labels = productionIssues
        .slice(0, 3)
        .map((issue) => app.deploymentDoctorIssueLabel(issue));
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
        message: `${app.formatNumber(runtime.oldest_api_inflight, 1)}s wait · ${runtime.api_waiting} queued${target}.`,
        next: "Check model API capacity.",
      };
    }
    const startupStalledShard = firstStartupStalledShard(runtime);
    if (startupStalledShard) {
      const runningFor = app.formatDuration(app.shardRunningSeconds(startupStalledShard));
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
      ? `<div class="next-action">${app.escapeHtml(explanation.next)}</div>`
      : "";
    return `<div class="status-explanation diagnosis small">
      <div>${app.escapeHtml(explanation.message)}</div>
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
    return `<div class="cell-stack" title="Technical state: ${app.escapeHtml(formatJobTechnicalState(job))}">
      <div class="cell-row">
        <span class="${app.statusClass(explanation.level)}">${app.escapeHtml(explanation.label)}</span>
      </div>
      ${formatJobStatusExplanation(job)}
      ${hasHealthIssue ? formatHealth(job) : ""}
    </div>`;
  }

  function formatJobIdentity(job) {
    const expanded = state.openJobDetails.has(job.id);
    return `<div class="cell-stack">
      <div class="primary-text mono" title="${app.escapeHtml(job.id)}">${app.escapeHtml(job.id.slice(0, 8))}...</div>
      <div class="small">${app.escapeHtml(job.engine)}</div>
      <button type="button" class="link-button jobDetailsToggle" data-id="${app.escapeHtml(job.id)}" aria-expanded="${expanded ? "true" : "false"}">
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
      return `<div class="small">${app.escapeHtml(selectedText)}</div><div class="small">No shard ownership reported yet.</div>`;
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
          const api = `api ${item.api_inflight || 0}/${item.api_inflight_peak || 0} peak; wait ${item.api_waiting || 0}; oldest ${app.formatNumber(item.oldest_api_inflight || 0, 1)}s`;
          return `<div class="worker-detail-item">
            <div class="primary-text path-full">${app.escapeHtml(item.server_id)}</div>
            <div>${app.escapeHtml(done)}</div>
            <div class="small">${app.escapeHtml(detail)}</div>
            <div class="small">${app.escapeHtml(api)}</div>
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
        <div><div class="detail-label">Total files</div><div class="primary-text">${app.escapeHtml(job.total_files || 0)}</div></div>
        <div><div class="detail-label">Scanned files</div><div class="primary-text">${app.escapeHtml(job.scanned_files || 0)}</div></div>
        <div><div class="detail-label">Processed files</div><div class="primary-text">${app.escapeHtml(processedFiles)}</div></div>
        <div><div class="detail-label">Shards created</div><div class="primary-text">${app.escapeHtml(job.shards_created || 0)}</div></div>
        <div><div class="detail-label">Total shards</div><div class="primary-text">${app.escapeHtml(job.total_shards || 0)}</div></div>
        <div><div class="detail-label">Shard done</div><div class="primary-text">${app.escapeHtml(shardDone)}</div></div>
      </div>
      <div class="small">${app.escapeHtml(shardCounts)}</div>
      <div class="small">scan units ${app.escapeHtml(scanDone)}</div>
      ${formatJobVersionWarning(job)}`;
  }

  function formatScanLifecycle(job) {
    const status = job.scan_status || "not_started";
    const files = job.scan_discovered_pdf_count ?? job.scan_progress_files ?? job.scanned_files ?? 0;
    const estimatedFiles = job.scan_estimated_total_pdf_count ?? job.scan_estimated_total_files ?? "-";
    const remainingFiles = job.scan_remaining_pdf_count ?? job.scan_remaining_files ?? "-";
    const scanPercent = job.scan_progress_percent == null ? "-" : `${app.formatNumber(job.scan_progress_percent)}%`;
    const dirs = job.scan_progress_dirs || 0;
    const bytes = app.formatBytes(job.scan_progress_bytes || 0);
    const eta = job.scan_eta_seconds == null ? "-" : app.formatDuration(job.scan_eta_seconds);
    const startedAt = app.formatDateTime(job.scan_started_at);
    const finishedAt = app.formatDateTime(job.scan_finished_at);
    const samples = Array.isArray(job.scan_error_samples) ? job.scan_error_samples : [];
    const sampleText = samples.length
      ? samples.slice(0, 3).map((item) => `${item.path || "-"}: ${item.failure_category || "scan_error"}: ${item.reason || "-"}`).join(" | ")
      : "no sampled scan errors";
    return `<div class="detail-section">
      <div class="cell-row">
        <span class="${app.statusClass(status === "done" ? "succeeded" : status)}">${app.escapeHtml(status)}</span>
        <span class="small">Scanning lifecycle</span>
      </div>
      <div class="metric-pairs">
        <div><div class="detail-label">Found PDFs</div><div class="primary-text">${app.escapeHtml(files)}</div></div>
        <div><div class="detail-label">Estimated PDFs</div><div class="primary-text">${app.escapeHtml(estimatedFiles)}</div></div>
        <div><div class="detail-label">Remaining PDFs</div><div class="primary-text">${app.escapeHtml(remainingFiles)}</div></div>
        <div><div class="detail-label">Scan progress</div><div>${app.escapeHtml(scanPercent)}</div></div>
        <div><div class="detail-label">Scanned dirs</div><div class="primary-text">${app.escapeHtml(dirs)}</div></div>
        <div><div class="detail-label">Scanned bytes</div><div>${app.escapeHtml(bytes)}</div></div>
        <div><div class="detail-label">ETA</div><div>${app.escapeHtml(eta)}</div></div>
        <div><div class="detail-label">Started</div><div>${app.escapeHtml(startedAt)}</div></div>
        <div><div class="detail-label">Finished</div><div>${app.escapeHtml(finishedAt)}</div></div>
      </div>
      <div class="small path-full">current ${app.escapeHtml(job.scan_current_path || "-")}</div>
      <div class="small path-full">${app.escapeHtml(job.scan_error_count || 0)} scan errors sampled: ${app.escapeHtml(sampleText)}</div>
    </div>`;
  }

  function formatJobLifecycle(job) {
    const stage = job.lifecycle_stage || job.status || "queued";
    return `<div class="detail-section">
      <div class="cell-row">
        <span class="${app.statusClass(stage)}">${app.escapeHtml(stage)}</span>
        <span class="small">Lifecycle</span>
      </div>
      <div class="small">scan ${app.escapeHtml(job.scan_status || "not_started")}; recovery ${app.escapeHtml(job.recovery_status || "healthy")}</div>
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
        <div><div class="detail-label">Shards</div><div class="primary-text">${app.escapeHtml(shardDone)}</div></div>
        <div><div class="detail-label">Generated shards</div><div class="primary-text">${app.escapeHtml(job.shards_created || 0)}</div></div>
        <div><div class="detail-label">Executable shards</div><div class="primary-text">${app.escapeHtml(job.executable_shards || 0)}</div></div>
        <div><div class="detail-label">Scan units</div><div class="primary-text">${app.escapeHtml(scanDone)}</div></div>
        <div><div class="detail-label">Snapshot</div><div><span class="${app.statusClass(snapshotStatus === "frozen" ? "succeeded" : snapshotStatus)}">${app.escapeHtml(snapshotStatus)}</span></div></div>
        <div><div class="detail-label">Frozen at</div><div>${app.escapeHtml(frozenAt)}</div></div>
      </div>
      <div class="small">manifest ${app.escapeHtml(manifestStatus)}</div>
      <div class="small">Freeze integrity <span class="${app.statusClass(integrityClass)}">${app.escapeHtml(integrityStatus)}</span>; issues ${app.escapeHtml(integrityIssueCount)}</div>
      <div class="small">${app.escapeHtml(shardCounts)}</div>
      <div class="small">${app.escapeHtml(scanCounts)}</div>`;
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
      : `${app.formatNumber(shard.pages_per_second, 3)} page/s`;
    const fileSpeed = shard.files_per_minute == null
      ? "-"
      : `${app.formatNumber(shard.files_per_minute, 2)} file/min`;
    const lease = shard.lease_seconds_remaining == null
      ? shard.lease_status || "none"
      : `${shard.lease_status || "none"} ${shard.lease_seconds_remaining}s`;
    const attempts = shard.max_attempts
      ? `${shard.attempt_count || 0}/${shard.max_attempts}`
      : `${shard.attempt_count || 0}`;
    const apiInflight = shard.api_inflight == null
      ? "-"
      : `${app.formatNumber(shard.api_inflight, 0)} / ${app.formatNumber(shard.api_inflight_peak || 0, 0)} peak`;
    const apiWaiting = shard.api_waiting == null ? "-" : app.formatNumber(shard.api_waiting, 0);
    const oldestInflight = shard.oldest_api_inflight
      ? `${app.formatNumber(shard.oldest_api_inflight, 1)}s`
      : "-";
    const execution = shard.execution_paused
      ? `paused; api limit ${shard.api_concurrency_limit || "-"}`
      : `running; api limit ${shard.api_concurrency_limit || "-"}`;
    const problem = [shard.failure_category, shard.error_message].filter(Boolean).join(": ");
    return `<div class="worker-detail-item">
      <div class="cell-row">
        <span class="${app.statusClass(shard.status)}">${app.escapeHtml(shard.status)}</span>
        <span class="primary-text mono">#${app.escapeHtml(shard.shard_index)}</span>
      </div>
      <div class="small">id ${app.escapeHtml(shard.id)}</div>
      <div class="metric-pairs">
        <div><div class="detail-label">Files</div><div>${app.escapeHtml(processed)}</div></div>
        <div><div class="detail-label">Pages</div><div>${app.escapeHtml(pages)}</div></div>
        <div><div class="detail-label">Speed</div><div>${app.escapeHtml(speed)}</div><div class="small">${app.escapeHtml(fileSpeed)}</div></div>
        <div><div class="detail-label">Attempt</div><div>${app.escapeHtml(attempts)}</div></div>
        <div><div class="detail-label">API inflight</div><div>${app.escapeHtml(apiInflight)}</div></div>
        <div><div class="detail-label">API wait</div><div>${app.escapeHtml(apiWaiting)}</div></div>
        <div><div class="detail-label">Oldest inflight</div><div>${app.escapeHtml(oldestInflight)}</div></div>
        <div><div class="detail-label">Execution</div><div>${app.escapeHtml(execution)}</div><div class="small">${app.escapeHtml(shard.execution_control_reason || "-")}</div></div>
      </div>
      <div class="cell-row">
        <span class="${app.statusClass(leaseStatusClass(shard.lease_status))}">${app.escapeHtml(lease)}</span>
      </div>
      ${problem ? `<div class="small path-full">${app.escapeHtml(problem)}</div>` : ""}
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
      return `<div class="small">Only active, stale, retrying, and failed shards are shown. ${app.escapeHtml(sampleText)} None need attention right now.</div>`;
    }
    return `<div class="small">Only active, stale, retrying, and failed shards are shown. ${app.escapeHtml(sampleText)}</div>
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
      .map((value) => `<option value="${app.escapeHtml(value)}"${inspector.status === value ? " selected" : ""}>${app.escapeHtml(value)}</option>`)
      .join("");
    if (!inspector.open) {
      return `<button type="button" class="link-button inspectShardsBtn" data-id="${app.escapeHtml(job.id)}">Inspect shards</button>
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
      : (inspector.error ? `<div class="small error">${app.escapeHtml(inspector.error)}</div>` : "");
    return `<div class="shard-inspector">
      <div class="shard-inspector-tools">
        <label class="small">status
          <select class="shardStatusSelect" data-id="${app.escapeHtml(job.id)}">${statusOptions}</select>
        </label>
        <label class="small">worker
          <input class="shardWorkerFilter" data-id="${app.escapeHtml(job.id)}" placeholder="worker id" value="${app.escapeHtml(inspector.workerId || "")}" />
        </label>
        <label class="small">failure
          <input class="shardFailureCategoryFilter" data-id="${app.escapeHtml(job.id)}" placeholder="failure_category" value="${app.escapeHtml(inspector.failureCategory || "")}" />
        </label>
        <label class="small">min attempts
          <input class="shardMinAttemptsFilter" data-id="${app.escapeHtml(job.id)}" type="number" min="0" placeholder="0" value="${app.escapeHtml(inspector.minAttemptCount || "")}" />
        </label>
        <label class="small">running over sec
          <input class="shardRunningLongerFilter" data-id="${app.escapeHtml(job.id)}" type="number" min="1" placeholder="3600" value="${app.escapeHtml(inspector.runningLongerThanSeconds || "")}" />
        </label>
        <button type="button" class="link-button refreshShardsBtn" data-id="${app.escapeHtml(job.id)}">Refresh shards</button>
        <button type="button" class="link-button closeShardsBtn" data-id="${app.escapeHtml(job.id)}">Close</button>
        <span class="small">${app.escapeHtml(rangeStart)}-${app.escapeHtml(rangeEnd)} of ${app.escapeHtml(total)}</span>
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
        <button type="button" class="link-button shardPageBtn" data-id="${app.escapeHtml(job.id)}" data-direction="prev"${previousDisabled}>Previous</button>
        <button type="button" class="link-button shardPageBtn" data-id="${app.escapeHtml(job.id)}" data-direction="next"${nextDisabled}>Next</button>
      </div>
    </div>`;
  }

  function formatJobLogs(job) {
    const stateItem = state.jobLogs.get(job.id);
    if (!stateItem || !stateItem.open) {
      return `<button type="button" class="link-button showJobLogsBtn" data-id="${app.escapeHtml(job.id)}">Job logs</button>
        <div class="small">Loads bounded recent stdout/stderr rows only when opened.</div>`;
    }
    if (stateItem.loading) {
      return `<div class="small">Loading job logs...</div>`;
    }
    if (stateItem.error) {
      return `<div class="small error">${app.escapeHtml(stateItem.error)}</div>`;
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
          <td class="small">${app.escapeHtml(app.formatDateTime(item.created_at))}</td>
          <td>${app.escapeHtml(item.server_id || "-")}</td>
          <td><span class="${app.statusClass(item.stream === "stderr" ? "failed" : "running")}">${app.escapeHtml(item.stream || "-")}</span></td>
          <td class="path-full">${app.escapeHtml(item.line || "")}</td>
        </tr>`).join("")
      : `<tr><td colspan="4" class="muted">No log rows retained for this job</td></tr>`;
    return `<div class="small">Log page ${app.escapeHtml(rangeStart)}-${app.escapeHtml(rangeEnd)} of ${app.escapeHtml(total)}</div>
      <div class="shard-table-wrap">
        <table class="shard-table">
          <thead><tr><th>Time</th><th>Worker</th><th>Stream</th><th>Line</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="shard-inspector-tools">
        <button type="button" class="link-button logPageBtn" data-id="${app.escapeHtml(job.id)}" data-direction="prev" data-limit="${app.escapeHtml(limit)}" data-offset="${app.escapeHtml(offset)}"${previousDisabled}>Previous logs</button>
        <button type="button" class="link-button logPageBtn" data-id="${app.escapeHtml(job.id)}" data-direction="next" data-limit="${app.escapeHtml(limit)}" data-offset="${app.escapeHtml(offset)}"${nextDisabled}>Next logs</button>
      </div>`;
  }

  function formatRecentErrors(job) {
    const stateItem = state.recentErrors.get(job.id);
    if (!stateItem || !stateItem.open) {
      return `<button type="button" class="link-button showRecentErrorsBtn" data-id="${app.escapeHtml(job.id)}">Recent errors</button>
        <div class="small">Loads bounded recent failure events and retained failed-file samples.</div>`;
    }
    if (stateItem.loading) {
      return `<div class="small">Loading recent errors...</div>`;
    }
    if (stateItem.error) {
      return `<div class="small error">${app.escapeHtml(stateItem.error)}</div>`;
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
          <td><span class="${app.statusClass("failed")}">${app.escapeHtml(item.failure_category || "unknown")}</span><div class="small">${app.escapeHtml(item.source || "-")}</div></td>
          <td>${app.escapeHtml(item.event_type || "-")}</td>
          <td class="path-full">${app.escapeHtml(item.file_path || item.filename || "-")}</td>
          <td class="path-full">${app.escapeHtml(item.error || "-")}</td>
        </tr>`).join("")
      : `<tr><td colspan="4" class="muted">No retained recent errors for this job</td></tr>`;
    return `<div class="small">Recent error page ${app.escapeHtml(rangeStart)}-${app.escapeHtml(rangeEnd)} of ${app.escapeHtml(total)}</div>
      <div class="shard-table-wrap">
        <table class="shard-table">
          <thead><tr><th>Category</th><th>Event</th><th>File</th><th>Error</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="shard-inspector-tools">
        <button type="button" class="link-button recentErrorPageBtn" data-id="${app.escapeHtml(job.id)}" data-direction="prev" data-limit="${app.escapeHtml(limit)}" data-offset="${app.escapeHtml(offset)}"${previousDisabled}>Previous errors</button>
        <button type="button" class="link-button recentErrorPageBtn" data-id="${app.escapeHtml(job.id)}" data-direction="next" data-limit="${app.escapeHtml(limit)}" data-offset="${app.escapeHtml(offset)}"${nextDisabled}>Next errors</button>
      </div>`;
  }

  function shardAttemptKey(jobId, shardId) {
    return `${jobId}:${shardId}`;
  }

  function formatShardAttempts(jobId, shard) {
    const key = shardAttemptKey(jobId, shard.id);
    const stateItem = state.shardAttempts.get(key);
    if (!stateItem || !stateItem.open) {
      return `<button type="button" class="link-button showAttemptsBtn" data-job-id="${app.escapeHtml(jobId)}" data-shard-id="${app.escapeHtml(shard.id)}">Attempt history</button>`;
    }
    if (stateItem.loading) {
      return `<div class="small">Loading attempt history...</div>`;
    }
    if (stateItem.error) {
      return `<div class="small error">${app.escapeHtml(stateItem.error)}</div>`;
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
    return `<div class="small">Attempt page ${app.escapeHtml(rangeStart)}-${app.escapeHtml(rangeEnd)} of ${app.escapeHtml(total)}</div>
    <div class="attempt-list">${
      attempts.map((attempt) => {
        const problem = [attempt.failure_category, attempt.error_message].filter(Boolean).join(": ");
        const execution = attempt.execution_paused
          ? `paused; api limit ${attempt.api_concurrency_limit || "-"}`
          : `running; api limit ${attempt.api_concurrency_limit || "-"}`;
        return `<div class="small path-full">
          <span class="${app.statusClass(attempt.status)}">${app.escapeHtml(attempt.status)}</span>
          #${app.escapeHtml(attempt.attempt_number)} ${app.escapeHtml(attempt.server_id)}
          files ${app.escapeHtml(attempt.processed_files || 0)}
          pages ${app.escapeHtml(attempt.completed_pages || 0)}
          execution ${app.escapeHtml(execution)}
          ${attempt.execution_control_reason ? ` ${app.escapeHtml(attempt.execution_control_reason)}` : ""}
          ${problem ? ` ${app.escapeHtml(problem)}` : ""}
        </div>`;
      }).join("")
    }</div>
    <div class="shard-inspector-tools">
      <button type="button" class="link-button attemptPageBtn" data-job-id="${app.escapeHtml(jobId)}" data-shard-id="${app.escapeHtml(shard.id)}" data-direction="prev" data-limit="${app.escapeHtml(limit)}" data-offset="${app.escapeHtml(offset)}"${previousDisabled}>Previous attempts</button>
      <button type="button" class="link-button attemptPageBtn" data-job-id="${app.escapeHtml(jobId)}" data-shard-id="${app.escapeHtml(shard.id)}" data-direction="next" data-limit="${app.escapeHtml(limit)}" data-offset="${app.escapeHtml(offset)}"${nextDisabled}>Next attempts</button>
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
      <td class="mono">${app.escapeHtml(shard.shard_index)}</td>
      <td><span class="${app.statusClass(shard.status)}">${app.escapeHtml(shard.status)}</span></td>
      <td class="path-full">${app.escapeHtml(shard.assigned_server_id || "-")}</td>
      <td>${app.escapeHtml(files)}</td>
      <td>${app.escapeHtml(shard.completed_pages || 0)}</td>
      <td>${app.escapeHtml(api)}<div class="small">oldest ${app.escapeHtml(app.formatNumber(shard.oldest_api_inflight || 0, 1))}s</div><div class="small">Execution ${app.escapeHtml(execution)}</div><div class="small">${app.escapeHtml(shard.execution_control_reason || "-")}</div></td>
      <td class="small">${app.escapeHtml(lease)}</td>
      <td>${app.escapeHtml(attempts)}<div>${formatShardAttempts(shard.job_id, shard)}</div></td>
      <td class="small path-full">${app.escapeHtml(problem || "-")}</td>
    </tr>`;
  }

  function formatManifestIntegrity(job) {
    const integrity = state.manifestIntegrity.get(job.id);
    if (!integrity) {
      return `<button type="button" class="link-button checkManifestIntegrityBtn" data-id="${app.escapeHtml(job.id)}">Check manifest integrity</button>
        <div class="small">Checks manifest file, shard files, and file counts only when requested.</div>`;
    }
    if (integrity.loading) {
      return `<div class="small">Checking manifest integrity...</div>`;
    }
    if (integrity.error) {
      return `<div class="small error">${app.escapeHtml(integrity.error)}</div>`;
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
      ? `<div class="small">checked by worker ${app.escapeHtml(payload.checked_by_server_id || "-")} at ${app.escapeHtml(payload.checked_at ? app.formatDateTime(payload.checked_at) : "-")}</div>`
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
      ? `<div class="small">scan units ${app.escapeHtml(payload.scan_unit_count)}; scan_unit_manifest_count_matches ${app.escapeHtml(Boolean(payload.scan_unit_manifest_count_matches))}</div>
         <div class="small">${app.escapeHtml(badScanUnitSampleText)}</div>
         <div class="small">${app.escapeHtml(badScanUnitText)}</div>`
      : "";
    return `<div class="detail-section">
      <div class="cell-row">
        <span class="${app.statusClass(statusClassName)}">${app.escapeHtml(statusLabel)}</span>
        <button type="button" class="link-button checkManifestIntegrityBtn" data-id="${app.escapeHtml(job.id)}">Refresh integrity</button>
        ${inaccessibleFromControl ? `<button type="button" class="link-button requestWorkerManifestIntegrityBtn" data-id="${app.escapeHtml(job.id)}">Request worker check</button>` : ""}
      </div>
      ${sourceLine}
      ${inaccessibleFromControl ? `<div class="small">Manifest paths are on a worker shared root that this control process cannot read directly; worker_integrity_status ${app.escapeHtml(workerStatus)}.</div>` : ""}
      <div class="small">manifest file ${app.escapeHtml(payload.manifest_file_exists ? "exists" : "missing")}; manifest_file_count_matches ${app.escapeHtml(Boolean(payload.manifest_file_count_matches))}; manifest_error ${app.escapeHtml(manifestError)}; meta_error ${app.escapeHtml(metaError)}</div>
      <div class="small">meta file_count ${app.escapeHtml(payload.meta_actual_file_count ?? "-")}/${app.escapeHtml(payload.meta_expected_file_count ?? 0)}; meta_file_count_matches ${app.escapeHtml(Boolean(payload.meta_file_count_matches))}</div>
      ${scanUnitLine}
      <div class="small">${app.escapeHtml(badShardSampleText)}</div>
      <div class="small">shards ${app.escapeHtml(payload.shard_count || 0)}; shard total ${app.escapeHtml(payload.shard_expected_file_count || 0)}/${app.escapeHtml(payload.shard_reference_file_count || 0)}; shard_file_count_matches_manifest ${app.escapeHtml(Boolean(payload.shard_file_count_matches_manifest))}; ${app.escapeHtml(badText)}</div>
    </div>`;
  }

  function formatManifestFreezeReport(job) {
    const freeze = state.manifestFreezeReports.get(job.id);
    if (!freeze) {
      return `<button type="button" class="link-button checkManifestFreezeBtn" data-id="${app.escapeHtml(job.id)}">Check manifest freeze</button>
        <div class="small">Shows the frozen scan snapshot after distributed scanning finishes.</div>`;
    }
    if (freeze.loading) {
      return `<div class="small">Checking manifest freeze...</div>`;
    }
    if (freeze.error) {
      return `<div class="small error">${app.escapeHtml(freeze.error)}</div>`;
    }
    const payload = freeze.payload || {};
    const report = payload.report || {};
    const scanUnits = report.scan_units || {};
    const shards = report.shards || {};
    const frozen = Boolean(report.frozen);
    const frozenAt = payload.frozen_at ? app.formatDateTime(payload.frozen_at) : "-";
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
        <span class="${app.statusClass(frozen ? "succeeded" : payload.status || "running")}">${app.escapeHtml(frozen ? "frozen" : (payload.status || "not frozen"))}</span>
        <button type="button" class="link-button checkManifestFreezeBtn" data-id="${app.escapeHtml(job.id)}">Refresh freeze</button>
      </div>
      <div class="metric-pairs">
        <div><div class="detail-label">Frozen at</div><div>${app.escapeHtml(frozenAt)}</div></div>
        <div><div class="detail-label">Files</div><div class="primary-text">${app.escapeHtml(report.file_count || 0)}</div></div>
        <div><div class="detail-label">Bytes</div><div>${app.escapeHtml(app.formatBytes(report.total_bytes || 0))}</div></div>
        <div><div class="detail-label">Shards</div><div class="primary-text">${app.escapeHtml(report.shard_count || 0)}</div></div>
      </div>
      <div class="small">scan units: ${app.escapeHtml(scanText)}</div>
      <div class="small">shards: ${app.escapeHtml(shardText)}</div>
      <div class="small">scan errors: ${app.escapeHtml(report.scan_error_count || 0)}</div>
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
              <div class="small path-full">${app.escapeHtml(job.input_dir)}</div>
            </div>
            <div>
              <div class="detail-label">Output</div>
              <div class="small path-full">${app.escapeHtml(job.output_dir)}</div>
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
          ${formatHealth(job) || `<span class="${app.statusClass("healthy")}">healthy</span>`}
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

  function formatJobVersionWarning(job) {
    if (job.worker_version_status !== "mixed") return "";
    const refs = job.worker_version_refs || {};
    const details = Object.entries(refs)
      .slice(0, 4)
      .map(([version, ids]) => `${version}: ${Array.isArray(ids) ? ids.join(", ") : ids}`)
      .join(" | ");
    const message = job.worker_version_warning || "assigned workers report different git_ref or script_version values";
    return `<div class="diagnosis small"><span class="status warning">mixed worker versions</span> ${app.escapeHtml(message)} ${app.escapeHtml(details)}</div>`;
  }

  function formatHealth(job) {
    const parts = [];
    if (job.failure_category) {
      parts.push(`<span class="status failed">${app.escapeHtml(job.failure_category)}</span>`);
    }
    parts.push(formatFailureCategoryCounts("File failures", job.failure_category_counts));
    parts.push(formatFailureCategoryCounts("Shard failures", job.shard_failure_category_counts));
    parts.push(formatFailureCategoryCounts("Scan-unit failures", job.scan_unit_failure_category_counts));
    if ((job.degraded_pages || 0) > 0) {
      parts.push(`<span class="status stopping">image fallback</span>`);
      parts.push(`<div class="small">${app.escapeHtml(job.degraded_pages)} degraded pages</div>`);
    }
    if (job.error_message) {
      parts.push(`<div class="small" title="${app.escapeHtml(job.error_message)}">${app.escapeHtml(job.error_message)}</div>`);
    }
    if (job.quality_flags && job.quality_flags.length && !(job.degraded_pages > 0)) {
      parts.push(`<div class="small">${app.escapeHtml(job.quality_flags.join(", "))}</div>`);
    }
    return parts.join("") || `<span class="muted">ok</span>`;
  }

  function renderJobs(jobs) {
    if (!jobs.length) {
      ui.jobsBody.innerHTML = `<tr><td colspan="6" class="muted">No jobs yet</td></tr>`;
      app.renderOperationsSummary();
      return;
    }

    ui.jobsBody.innerHTML = jobs
      .flatMap((job) => {
        const terminal = ["succeeded", "failed", "stopped"].includes(job.status);
        const actions = terminal
          ? `<div class="cell-row"><button data-id="${app.escapeHtml(job.id)}" class="archiveJobBtn">Archive</button><button data-id="${app.escapeHtml(job.id)}" class="deleteBtn">Delete</button></div>`
          : `<button data-id="${app.escapeHtml(job.id)}" class="stopBtn">Stop</button>`;
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
    app.renderOperationsSummary();
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

  return { rowProgress, rowShards, rowWorkerSummary, rowThroughput, formatFailureCategoryCounts, formatFreshness, jobRuntimeSignals, firstAttentionShard, firstStartupStalledShard, formatWorkPiece, formatJobTechnicalState, explainJobStatus, formatJobStatusExplanation, formatJobStatus, formatJobIdentity, formatWorkerAllocation, formatJobOverview, formatScanLifecycle, formatJobLifecycle, formatWorkPlan, leaseStatusClass, formatCurrentShard, formatAttentionShards, shardInspectorState, formatShardInspector, formatJobLogs, formatRecentErrors, shardAttemptKey, formatShardAttempts, formatShardTableRow, formatManifestIntegrity, formatManifestFreezeReport, formatJobDetailsPanel, formatJobVersionWarning, formatHealth, renderJobs, loadJobs, buildJobsSummaryUrl, renderJobsPagination, loadShardInspector, checkManifestIntegrity, requestWorkerManifestIntegrity, checkManifestFreezeReport, loadShardAttempts, loadJobLogs, loadRecentErrors, stopJob, deleteJob, archiveJob };
}
