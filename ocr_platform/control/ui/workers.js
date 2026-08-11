export const SERVERS_API_ROOT = "/api/servers";

export function serverApiPath(serverId) {
  return `${SERVERS_API_ROOT}/${encodeURIComponent(serverId)}`;
}


export function createWorkersModule(app) {
  const { state, ui, requestJson } = app;
  function isPoolInputMode() {
    return app.selectedInputMode() !== "directory";
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

  function workerEventSpoolBacklog(server) {
    const spool = (server.capabilities && server.capabilities.event_spool) || {};
    const pendingEvents = app.nonnegativeCount(spool.pending_events);
    const pendingLogs = app.nonnegativeCount(spool.pending_logs);
    const failedEvents = app.nonnegativeCount(spool.failed_events);
    const failedLogs = app.nonnegativeCount(spool.failed_logs);
    const droppedEvents = app.nonnegativeCount(spool.dropped_events);
    const droppedLogs = app.nonnegativeCount(spool.dropped_logs);
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
    const pending = app.nonnegativeCount(pendingUpdates.pending);
    const failed = app.nonnegativeCount(pendingUpdates.failed);
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
    const matched = readiness.matchedPath ? `<div class="small">matched ${app.escapeHtml(readiness.matchedPath)}</div>` : "";
    const noteText = notes.length ? `<div class="small">${app.escapeHtml(notes.slice(0, 3).join("; "))}</div>` : "";
    return `<span class="${app.statusClass(readiness.level)}">${app.escapeHtml(readiness.label)}</span>${matched}${noteText}`;
  }

  function workerReadinessNotes(readiness) {
    const notes = readiness.blockers.length ? readiness.blockers : readiness.warnings;
    const matched = readiness.matchedPath ? `<div class="small">matched ${app.escapeHtml(readiness.matchedPath)}</div>` : "";
    const noteText = notes.length ? `<div class="small">${app.escapeHtml(notes.slice(0, 3).join("; "))}</div>` : "";
    return `${matched}${noteText}`;
  }

  function workerActions(server) {
    if (server.is_stale || server.status === "offline") {
      return `<button data-id="${app.escapeHtml(server.id)}" class="deleteBtn removeServerBtn">Remove</button>`;
    }
    return `<div class="cell-stack">
      <button type="button" data-id="${app.escapeHtml(server.id)}" class="scaleWorkerBtn">Scale</button>
      <span class="muted">active</span>
    </div>`;
  }

  function formatServerHeartbeat(server) {
    const stale = server.is_stale ? `<span class="status stale">stale</span>` : "";
    if (!server.last_heartbeat_at) {
      return `${stale}<div class="small">No heartbeat yet</div>`;
    }
    const date = new Date(server.last_heartbeat_at);
    return `${stale}<div class="small">${app.escapeHtml(date.toLocaleString())}</div>`;
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
          <div><span class="${app.statusClass(cls)}">${app.escapeHtml(label)}</span></div>
          <div class="small path-full">${app.escapeHtml(item.path)}</div>
          <div class="small">${app.escapeHtml(details)}</div>
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
      <div class="cell-row"><span class="${app.statusClass(cls)}">${app.escapeHtml(label)}</span><span class="small">${app.escapeHtml(detail)}</span></div>
      <div class="small path-text" title="${app.escapeHtml(primary.path || "")}">${app.escapeHtml(primary.path || "")}</div>
    </div>`;
  }

  function formatWorkerVersion(server) {
    const caps = server.capabilities || {};
    const version = caps.git_ref || "unknown git";
    const script = caps.script_version || "unknown script";
    return `<div class="cell-stack">
      <div class="primary-text mono">${app.escapeHtml(version)}</div>
      <div class="small">${app.escapeHtml(script)}</div>
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
    return `<div class="diagnosis small"><span class="status warning">mixed worker versions</span> ${app.escapeHtml(details)}</div>`;
  }

  function formatWorkerIdentity(server) {
    const expanded = state.openWorkerDetails.has(server.id);
    return `<div class="cell-stack">
      <div class="primary-text">${app.escapeHtml(server.id)}</div>
      <div class="small">${app.escapeHtml(server.host)}</div>
      ${formatServerHeartbeat(server)}
      <button type="button" class="link-button workerDetailsToggle" data-id="${app.escapeHtml(server.id)}" aria-expanded="${expanded ? "true" : "false"}">
        ${expanded ? "▼" : "▶"} details
      </button>
    </div>`;
  }

  function formatWorkerHealth(server, readiness) {
    return `<div class="cell-stack">
      <div class="cell-row">
        <span class="${app.statusClass(server.status)}">${app.escapeHtml(server.status)}</span>
        <span class="${app.statusClass(readiness.level)}">${app.escapeHtml(readiness.label)}</span>
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
      ? `<div class="small">CPU ${app.escapeHtml(app.resourcePercent(cpuPercent))}; MEM ${app.escapeHtml(app.resourcePercent(memoryPercent))}; DISK ${app.escapeHtml(app.resourcePercent(diskPercent))}</div>`
      : "";
    return `<div class="cell-stack">
      <div class="primary-text">${app.escapeHtml(runningShards)} / ${app.escapeHtml(capacity)}</div>
      <div class="small">running shards</div>
      <div class="small">${app.escapeHtml(activeJobs)} active jobs</div>
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
          <div class="detail-label">${app.escapeHtml(label)}</div>
          <div class="small path-full">${app.escapeHtml(value)}</div>
        </div>`)
        .join("")
    }</div>`;
  }

  function formatWorkerLoadDetails(server) {
    const capacity = Number(server.capacity_slots || 1);
    const runningShards = Number(server.running_shards || 0);
    const activeJobs = Number(server.active_jobs || 0);
    return `<div class="metric-pairs">
      <div><div class="detail-label">Running shards</div><div class="primary-text">${app.escapeHtml(runningShards)}</div></div>
      <div><div class="detail-label">Capacity</div><div class="primary-text">${app.escapeHtml(capacity)}</div></div>
      <div><div class="detail-label">Active jobs</div><div class="primary-text">${app.escapeHtml(activeJobs)}</div></div>
      <div><div class="detail-label">Status</div><div><span class="${app.statusClass(server.status)}">${app.escapeHtml(server.status)}</span></div></div>
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
          <div class="cell-row"><span class="${app.statusClass(app.resourceStatus(disk.percent))}">${app.escapeHtml(app.resourcePercent(disk.percent))}</span><span class="small">${app.escapeHtml(disk.exists ? "exists" : "missing")}</span></div>
          <div class="small path-full">${app.escapeHtml(disk.path || "")}</div>
          <div class="small">${app.escapeHtml(app.formatBytes(disk.free_bytes))} free / ${app.escapeHtml(app.formatBytes(disk.total_bytes))}</div>
        </div>`).join("")
      : `<span class="muted">no disk paths reported</span>`;
    return `<div class="detail-section">
      <div>
        <div class="detail-label">Guard</div>
        <div class="cell-row">
          <span class="${app.statusClass(pressure.constrained ? "blocked" : (pressure.level || "ready"))}">${app.escapeHtml(pressure.constrained ? "resource constrained" : (pressure.level || "ready"))}</span>
        </div>
        ${Array.isArray(pressure.reasons) && pressure.reasons.length ? `<div class="small">${app.escapeHtml(pressure.reasons.join("; "))}</div>` : ""}
      </div>
      <div class="metric-pairs">
        <div>
          <div class="detail-label">CPU load</div>
          <div><span class="${app.statusClass(app.resourceStatus(cpu.load_percent_1m))}">${app.escapeHtml(app.resourcePercent(cpu.load_percent_1m))}</span></div>
          <div class="small">1m ${app.escapeHtml(app.formatNumber(cpu.load_avg_1m, 2))}; ${app.escapeHtml(cpu.logical_count || "-")} cores</div>
        </div>
        <div>
          <div class="detail-label">Memory</div>
          <div><span class="${app.statusClass(app.resourceStatus(memory.percent))}">${app.escapeHtml(app.resourcePercent(memory.percent))}</span></div>
          <div class="small">${app.escapeHtml(app.formatBytes(memory.available_bytes))} available / ${app.escapeHtml(app.formatBytes(memory.total_bytes))}</div>
        </div>
      </div>
      <div>
        <div class="detail-label">Disks</div>
        <div class="worker-detail-list scroll-detail-list">${diskItems}</div>
      </div>
      <div class="small">checked ${app.escapeHtml(new Date(resources.checked_at).toLocaleString())}</div>
    </div>`;
  }

  function formatWorkerEventSpool(server) {
    const backlog = workerEventSpoolBacklog(server);
    const status = backlog.total > 0 ? "warning" : "succeeded";
    const label = backlog.total > 0 ? `${backlog.total} queued, quarantined, or dropped` : "clear";
    const dir = backlog.dir
      ? `<div class="small path-full">${app.escapeHtml(backlog.dir)}</div>`
      : `<div class="small muted">spool dir not reported</div>`;
    return `<div class="detail-section">
      <div class="cell-row">
        <span class="${app.statusClass(status)}">${app.escapeHtml(label)}</span>
      </div>
      <div class="metric-pairs">
        <div><div class="detail-label">Pending events</div><div class="primary-text">${app.escapeHtml(backlog.pending_events)}</div></div>
        <div><div class="detail-label">Pending logs</div><div class="primary-text">${app.escapeHtml(backlog.pending_logs)}</div></div>
        <div><div class="detail-label">Failed events</div><div class="primary-text">${app.escapeHtml(backlog.failed_events)}</div></div>
        <div><div class="detail-label">Failed logs</div><div class="primary-text">${app.escapeHtml(backlog.failed_logs)}</div></div>
        <div><div class="detail-label">Dropped events</div><div class="primary-text">${app.escapeHtml(backlog.dropped_events)}</div></div>
        <div><div class="detail-label">Dropped logs</div><div class="primary-text">${app.escapeHtml(backlog.dropped_logs)}</div></div>
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
        <span class="${app.statusClass(status)}">${app.escapeHtml(label)}</span>
      </div>
      <div class="metric-pairs">
        <div><div class="detail-label">Pending updates</div><div class="primary-text">${app.escapeHtml(backlog.pending)}</div></div>
        <div><div class="detail-label">Failed updates</div><div class="primary-text">${app.escapeHtml(backlog.failed)}</div></div>
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
    const filter = (ui.workerFilter?.value || "").trim().toLowerCase();
    const filtered = filter
      ? visible.filter((server) => {
          const readiness = classifyWorkerReadiness(server);
          return [server.id, server.name, server.host, server.status, readiness.level]
            .some((value) => String(value || "").toLowerCase().includes(filter));
        })
      : visible;
    const online = onlineWorkers(servers);
    const readiness = visible.map((server) => classifyWorkerReadiness(server));
    const ready = readiness.filter((item) => item.level === "ready");
    const warning = readiness.filter((item) => item.level === "warning");
    const blocked = readiness.filter((item) => item.level === "blocked");
    ui.serverMetrics.innerHTML = [
      `<div class="metric"><strong>${app.escapeHtml(ready.length)}</strong><span class="small">Ready workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(warning.length)}</strong><span class="small">Warning workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(blocked.length)}</strong><span class="small">Blocked workers</span></div>`,
      `<div class="metric"><strong>${app.escapeHtml(online.reduce((sum, server) => sum + (server.running_shards || 0), 0))}</strong><span class="small">running shards</span></div>`,
    ].join("") + formatVersionWarning(servers);
    app.renderOperationsSummary();
    if (!filtered.length) {
      ui.serversBody.innerHTML = `<tr><td colspan="6" class="muted">${visible.length ? "No workers match this filter" : "No servers yet"}</td></tr>`;
      return;
    }
    ui.serversBody.innerHTML = filtered
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

  function renderWorkerSelectors(servers, selectedIds) {
    const selectable = visibleServers(servers);
    const options = selectable.map((server) =>
      `<option value="${app.escapeHtml(server.id)}">${app.escapeHtml(server.id)} (${app.escapeHtml(server.status)}, ${app.escapeHtml(server.host)})</option>`
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
        return `<label class="check-item"><input type="checkbox" value="${app.escapeHtml(server.id)}"${checked} /> ${app.escapeHtml(label)}</label>`;
      })
      .join("");
  }

  async function loadServers() {
    const servers = await requestJson(SERVERS_API_ROOT);
    renderServers(servers);
    const selected = app.selectedAllowedServerIds();
    renderWorkerSelectors(servers, selected);
    app.syncExecutionModeFields();
    app.schedulePreflight();
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
        app.setMessage(ui.serverMessage, `Failed to parse capabilities JSON: ${error.message}`, true);
        return;
      }
    }
    const payload = { id: serverId, name, host, capacity_slots: capacity, capabilities };
    await requestJson(`${SERVERS_API_ROOT}/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    app.setMessage(ui.serverMessage, `Registered/updated server: ${serverId}`);
    await loadServers();
  }

  async function archiveServer(serverId) {
    await requestJson(serverApiPath(serverId), { method: "DELETE" });
    await loadServers();
  }

  return { isPoolInputMode, visibleServers, onlineWorkers, sharedPathReady, workerEventSpoolBacklog, workerPendingShardUpdateBacklog, classifyWorkerReadiness, workerReadinessDetail, workerReadinessNotes, workerActions, formatServerHeartbeat, formatSharedPaths, formatSharedPathSummary, formatWorkerVersion, formatVersionWarning, formatWorkerIdentity, formatWorkerHealth, formatWorkerLoad, formatWorkerRuntime, formatWorkerLoadDetails, formatWorkerResourceDetails, formatWorkerEventSpool, formatWorkerPendingShardUpdates, formatWorkerDetailsPanel, renderServers, renderWorkerSelectors, loadServers, registerServer, archiveServer };
}
