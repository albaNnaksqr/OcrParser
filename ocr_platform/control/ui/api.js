import { apiRequestOptions } from "./auth.js";

export function createJsonRequester(getToken) {
  return async function requestJson(url, options = {}) {
    const response = await fetch(url, apiRequestOptions(getToken(), options));
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = payload.detail || payload.message || `HTTP ${response.status}`;
      throw new Error(typeof message === "string" ? message : JSON.stringify(message));
    }
    return payload;
  };
}
