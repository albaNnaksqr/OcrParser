export const DEFAULT_MODEL_PROFILE = "dotsocr_15";
const API_TOKEN_STORAGE_KEY = "OCR_PLATFORM_UI_TOKEN";
export const MODEL_PROFILES = {
  paddleocr_vl_local: {
    label: "PaddleOCR-VL @ worker-1.example.internal",
    engine: "paddleocr-vl",
    ip: "127.0.0.1",
    port: 30001,
    model_name: "paddleocr-vl",
    page_concurrency: 4,
    extra_args: {
      skip_blank_pages: true,
      file_concurrency: 4,
      api_concurrency_start: 8,
      api_concurrency_max: 8,
      block_concurrency: 8,
      paddle_layout_concurrency: 2,
      paddle_block_backpressure_high_watermark: 24,
      paddle_block_backpressure_low_watermark: 8,
      num_cpu_workers: 16,
      max_retries: 1,
      retry_delay: 1,
      timeout: 900,
      max_completion_tokens: 4096,
      no_warmup: true,
      layout_detection_url: "http://127.0.0.1:30002"
    }
  },
  mineru_v25: {
    label: "MinerU 2.5 @ 127.0.0.1",
    engine: "mineru",
    ip: "127.0.0.1",
    port: 30090,
    model_name: "MinerU2.5",
    page_concurrency: 4,
    extra_args: {
      skip_blank_pages: true,
      file_concurrency: 4,
      api_concurrency_start: 8,
      api_concurrency_max: 8,
      block_concurrency: 8,
      mineru_layout_reserved_api_slots: 2,
      mineru_recognition_api_concurrency: 6,
      num_cpu_workers: 16,
      max_retries: 1,
      retry_delay: 1,
      timeout: 900,
      max_completion_tokens: 4096,
      no_warmup: true
    }
  },
  dotsocr_15: {
    label: "DotsOCR 1.5 @ 127.0.0.1",
    engine: "dotsocr",
    ip: "127.0.0.1",
    port: 13080,
    model_name: "DotsOCR",
    page_concurrency: 80,
    extra_args: {
      skip_blank_pages: true,
      file_concurrency: 8,
      api_concurrency_start: 80,
      api_concurrency_max: 80,
      num_cpu_workers: 56,
      max_retries: 1,
      retry_delay: 1,
      timeout: 180,
      max_completion_tokens: 4096,
      no_warmup: true
    },
    requires_api_key: true
  }
};

export const MODEL_PROFILES_API = "/api/model-profiles";
export function modelProfileApiPath(profileId) {
  return `${MODEL_PROFILES_API}/${encodeURIComponent(profileId)}`;
}


export function createProfilesModule(app) {
  const { state, ui, requestJson } = app;
  function normalizeModelProfile(profile) {
    return {
      ...profile,
      extra_args: profile && profile.extra_args && typeof profile.extra_args === "object" ? profile.extra_args : {},
      requires_api_key: Boolean(profile && profile.requires_api_key),
      has_api_key: Boolean(profile && profile.has_api_key),
      api_key_env_var: profile && profile.api_key_env_var ? String(profile.api_key_env_var) : "",
      is_default: Boolean(profile && profile.is_default)
    };
  }

  function renderModelProfileOptions(previousValue = "") {
    const entries = Object.entries(state.modelProfiles);
    ui.modelProfile.innerHTML = entries
      .map(([id, profile]) => `<option value="${app.escapeHtml(id)}">${app.escapeHtml(profile.label || id)}</option>`)
      .join("");
    const defaultEntry = entries.find(([, profile]) => profile.is_default);
    const nextValue = previousValue && state.modelProfiles[previousValue]
      ? previousValue
      : (defaultEntry ? defaultEntry[0] : DEFAULT_MODEL_PROFILE);
    if (state.modelProfiles[nextValue]) {
      ui.modelProfile.value = nextValue;
    }
  }

  async function loadModelProfiles() {
    const previousValue = ui.modelProfile.value;
    try {
      const profiles = await requestJson(MODEL_PROFILES_API);
      state.modelProfiles = Object.fromEntries(
        profiles.map((profile) => [profile.id, normalizeModelProfile(profile)])
      );
      app.setMessage(ui.modelProfileMessage, "");
    } catch (error) {
      state.modelProfiles = Object.fromEntries(
        Object.entries(MODEL_PROFILES).map(([id, profile]) => [id, normalizeModelProfile({ id, ...profile })])
      );
      app.setMessage(ui.modelProfileMessage, `Using built-in profiles: ${error.message}`, true);
    }
    renderModelProfileOptions(previousValue);
    applyModelProfile();
  }

  function selectedModelProfile() {
    return state.modelProfiles[ui.modelProfile.value] || null;
  }

  function renderResolvedModelConfig(profile) {
    if (!profile) {
      ui.resolvedModelConfig.innerHTML = "";
      return;
    }
    const items = [
      ["engine", profile.engine],
      ["ip", profile.ip],
      ["port", profile.port],
      ["model_name", profile.model_name],
      ["file_concurrency", profile.extra_args && profile.extra_args.file_concurrency],
    ];
    ui.resolvedModelConfig.innerHTML = items
      .map(([key, value]) => `<span class="config-chip"><strong>${app.escapeHtml(key)}</strong> ${app.escapeHtml(value)}</span>`)
      .join("");
  }

  function formatProfileKeyStatus(profile) {
    if (!profile) return "No selected profile";
    if (profile.api_key_env_var && profile.has_api_key) {
      return `API key resolved from env var ${profile.api_key_env_var}`;
    }
    if (profile.api_key_env_var) {
      return `Env var ${profile.api_key_env_var} is configured but not resolved`;
    }
    return profile.has_api_key ? "API key saved for this profile" : "No saved API key";
  }

  function renderModelProfileEditor(profile) {
    if (!profile) {
      ui.profileLabel.value = "";
      ui.profileEngine.value = "";
      ui.profileIp.value = "";
      ui.profilePort.value = "";
      ui.profileModelName.value = "";
      ui.profilePageConcurrency.value = "";
      ui.profileExtraArgs.value = "{}";
      ui.profileApiKey.value = "";
      ui.profileApiKeyEnvVar.value = "";
      ui.profileHasApiKey.textContent = formatProfileKeyStatus(null);
      ui.profileRequiresApiKey.checked = false;
      ui.profileClearApiKey.checked = false;
      ui.profileIsDefault.checked = false;
      return;
    }
    ui.profileLabel.value = profile.label || "";
    ui.profileEngine.value = profile.engine || "";
    ui.profileIp.value = profile.ip || "";
    ui.profilePort.value = profile.port || "";
    ui.profileModelName.value = profile.model_name || "";
    ui.profilePageConcurrency.value = profile.page_concurrency || "";
    ui.profileExtraArgs.value = JSON.stringify(profile.extra_args || {}, null, 2);
    ui.profileApiKey.value = "";
    ui.profileApiKeyEnvVar.value = profile.api_key_env_var || "";
    ui.profileHasApiKey.textContent = formatProfileKeyStatus(profile);
    ui.profileRequiresApiKey.checked = Boolean(profile.requires_api_key);
    ui.profileClearApiKey.checked = false;
    ui.profileIsDefault.checked = Boolean(profile.is_default);
  }

  function applyModelProfile() {
    const profile = selectedModelProfile();
    if (!profile) return;

    document.getElementById("pageConcurrency").value = profile.page_concurrency || "";
    document.getElementById("fileConcurrency").value = (profile.extra_args && profile.extra_args.file_concurrency) || "";
    document.getElementById("apiConcurrencyStart").value = (profile.extra_args && profile.extra_args.api_concurrency_start) || "";
    document.getElementById("apiConcurrencyMax").value = (profile.extra_args && profile.extra_args.api_concurrency_max) || "";
    document.getElementById("apiAutotuneInterval").value = (profile.extra_args && profile.extra_args.api_autotune_interval) || "";
    document.getElementById("enableApiAutotune").checked = Boolean(profile.extra_args && profile.extra_args.enable_api_autotune);
    document.getElementById("numCpuWorkers").value = (profile.extra_args && profile.extra_args.num_cpu_workers) || "";
    ui.apiKey.placeholder = profile.requires_api_key && !profile.has_api_key
      ? "Required unless saved in profile"
      : "Optional per-job override";
    renderResolvedModelConfig(profile);
    renderModelProfileEditor(profile);
  }

  async function saveModelProfile() {
    const profileId = ui.modelProfile.value;
    if (!profileId) {
      app.setMessage(ui.modelProfileMessage, "Please select a model profile first.", true);
      return;
    }
    let extraArgs = {};
    try {
      extraArgs = JSON.parse(ui.profileExtraArgs.value || "{}");
      if (!extraArgs || Array.isArray(extraArgs) || typeof extraArgs !== "object") {
        throw new Error("extra_args must be a JSON object");
      }
      const secretLikeKeys = Object.keys(extraArgs).filter((key) => {
        const normalized = key.toLowerCase().replace(/-/g, "_");
        return normalized === "api_key"
          || normalized === "api_key_env_var"
          || normalized === "authorization"
          || normalized === "password"
          || normalized.endsWith("_token")
          || normalized.endsWith("_secret")
          || normalized.endsWith("_password");
      });
      if (secretLikeKeys.length) {
        throw new Error(`extra_args must not contain secret-like keys (${secretLikeKeys.join(", ")}); use saved_api_key or api_key_env_var instead`);
      }
    } catch (error) {
      app.setMessage(ui.modelProfileMessage, `Failed to parse extra_args JSON: ${error.message}`, true);
      return;
    }
    const payload = {
      label: ui.profileLabel.value.trim() || profileId,
      engine: ui.profileEngine.value.trim(),
      ip: ui.profileIp.value.trim() || null,
      port: Number(ui.profilePort.value || 0) || null,
      model_name: ui.profileModelName.value.trim() || null,
      page_concurrency: Number(ui.profilePageConcurrency.value || 0) || null,
      extra_args: extraArgs,
      requires_api_key: ui.profileRequiresApiKey.checked,
      is_default: ui.profileIsDefault.checked,
      api_key_env_var: ui.profileApiKeyEnvVar.value.trim() || null,
      clear_api_key: ui.profileClearApiKey.checked
    };
    const apiKey = ui.profileApiKey.value.trim();
    if (apiKey) {
      payload.api_key = apiKey;
    }
    if (!payload.engine) {
      app.setMessage(ui.modelProfileMessage, "engine is required.", true);
      return;
    }
    const saved = await requestJson(modelProfileApiPath(profileId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.modelProfiles[profileId] = normalizeModelProfile(saved);
    renderModelProfileOptions(profileId);
    applyModelProfile();
    const keyStatus = state.modelProfiles[profileId].has_api_key
      ? "API key saved and available for jobs"
      : (payload.clear_api_key ? "API key cleared" : "No saved API key");
    app.setMessage(ui.modelProfileMessage, `Saved profile: ${profileId}. ${keyStatus}.`);
  }

  return { normalizeModelProfile, renderModelProfileOptions, loadModelProfiles, selectedModelProfile, renderResolvedModelConfig, formatProfileKeyStatus, renderModelProfileEditor, applyModelProfile, saveModelProfile };
}
