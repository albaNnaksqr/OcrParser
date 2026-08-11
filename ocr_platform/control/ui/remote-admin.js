const ROOT = "/api/remote-workers";

export const REMOTE_ADMIN_API = {
  targets: `${ROOT}/targets`,
  preflight: `${ROOT}/preflight`,
  installDryRun: `${ROOT}/install-dry-run`,
  installApply: `${ROOT}/install-apply`,
  scalePlan: `${ROOT}/scale-plan`,
  scaleApply: `${ROOT}/scale-apply`,
  service: `${ROOT}/service`,
};


export function createRemoteAdminModule(app) {
  const { state, ui, requestJson } = app;
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
      return `<option value="${app.escapeHtml(target.id)}">${app.escapeHtml(target.host + suffix)}</option>`;
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
    return app.visibleServers().filter((server) => server.host === host);
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
    ui.workerScaleResult.innerHTML = `<span class="${app.statusClass("ready")}">Scale target selected</span> <span class="small">${app.escapeHtml(server.id)}</span>`;
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
        <td>${app.escapeHtml(item.action || "")}</td>
        <td><span class="${app.statusClass(status)}">${app.escapeHtml(status)}</span></td>
        <td>${app.escapeHtml(item.instance || "-")}</td>
        <td>${app.escapeHtml(item.server_id || "-")}</td>
        <td>${app.escapeHtml(item.message || "")}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="5" class="muted">No structured plan items returned</td></tr>`;
    const debug = `<details class="advanced"><summary>debug output</summary><pre class="log-box">${app.escapeHtml([
      `action: ${payload.action || ""}`,
      `ok: ${payload.ok}`,
      `return_code: ${payload.return_code}`,
      `command: ${(payload.command || []).join(" ")}`,
      "",
      payload.stdout || "",
      payload.stderr || "",
    ].join("\n"))}</pre></details>`;
    ui.workerScaleResult.innerHTML = `
      <div class="small">${app.escapeHtml(waitingText || "")}</div>
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
    return app.visibleServers().filter((server) => {
      const readiness = app.classifyWorkerReadiness(server);
      return server.host === host && String(server.id || "").startsWith(prefix) && readiness.level === "ready";
    }).length;
  }

  async function waitForWorkerScaleHeartbeat(host, prefix, targetCount) {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await app.loadServers();
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

  return { remoteWorkerSharedRoots, renderRemoteWorkerTargets, applyRemoteWorkerTarget, loadRemoteWorkerTargets, remoteWorkerBasePayload, remoteWorkerInstallPayload, renderRemoteWorkerResult, workerScaleSharedRoots, workersOnHost, deriveWorkerScalePrefix, selectWorkerScaleTarget, workerScalePayload, renderWorkerScaleResult, runWorkerScalePlan, countedScaledWorkers, waitForWorkerScaleHeartbeat, applyWorkerScale, runRemoteWorkerPreflight, runRemoteWorkerInstallDryRun, runRemoteWorkerInstallApply, runRemoteWorkerServiceAction };
}
