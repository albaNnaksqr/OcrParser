export const API_TOKEN_STORAGE_KEY = "OCR_PLATFORM_UI_TOKEN";

export function loadSessionToken() {
  return sessionStorage.getItem(API_TOKEN_STORAGE_KEY) || "";
}

export function saveSessionToken(token) {
  sessionStorage.setItem(API_TOKEN_STORAGE_KEY, token);
}

export function clearSessionToken() {
  sessionStorage.removeItem(API_TOKEN_STORAGE_KEY);
}

export function apiRequestOptions(token, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set("X-API-Key", token);
  return { ...options, headers };
}
