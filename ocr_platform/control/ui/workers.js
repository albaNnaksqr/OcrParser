export const SERVERS_API_ROOT = "/api/servers";

export function serverApiPath(serverId) {
  return `${SERVERS_API_ROOT}/${encodeURIComponent(serverId)}`;
}
