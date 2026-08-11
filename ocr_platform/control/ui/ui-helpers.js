

export function createHelpersModule(app) {
  const { state, ui, requestJson } = app;
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

  function nonnegativeCount(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
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

  function resourcePercent(value) {
    const num = Number(value);
    return Number.isFinite(num) ? `${formatNumber(num, 1)}%` : "-";
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

  return { escapeHtml, setMessage, nonnegativeCount, statusClass, formatNumber, formatBytes, resourceStatus, resourcePercent, formatDuration, timestampAgeSeconds, shardRunningSeconds, formatDateTime };
}
