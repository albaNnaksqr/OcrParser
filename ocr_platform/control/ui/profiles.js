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
