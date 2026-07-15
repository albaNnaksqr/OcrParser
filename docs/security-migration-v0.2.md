# v0.2 Security Migration

v0.2 intentionally changes four defaults:

1. Control binds to `127.0.0.1`. A non-loopback `OCR_PLATFORM_HOST` requires a
   non-empty `OCR_PLATFORM_API_TOKEN`, otherwise startup fails.
2. `/api/remote-workers/*` remains discoverable but returns `403` until
   `OCR_PLATFORM_ENABLE_REMOTE_ADMIN=1` is set.
3. The UI stores its bearer token in `sessionStorage`; it is cleared when the
   browser session ends.
4. New model profiles and per-job requests cannot save API keys in the database.
   Use `api_key_env_var`. Existing keys are retained for migration and reported by
   Deployment Doctor. `OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS=1` is a
   temporary compatibility escape hatch, not a recommended production setting.

Before exposing Control beyond localhost, set a strong token in the process
environment, verify reverse-proxy TLS, and keep Remote Admin disabled unless the
host inventory and SSH policy have been reviewed.
