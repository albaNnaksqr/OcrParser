export const JOBS_API_ROOT = "/api/jobs";

export function jobApiPath(jobId, suffix = "") {
  return `${JOBS_API_ROOT}/${encodeURIComponent(jobId)}${suffix}`;
}
