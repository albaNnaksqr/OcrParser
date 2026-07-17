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
