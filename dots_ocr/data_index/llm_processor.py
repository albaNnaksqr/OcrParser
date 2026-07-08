import asyncio
import hashlib
import json
import os
import re
from functools import lru_cache
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI
try:
    # OpenAI v1.x exports these; fall back to generic Exception if unavailable
    from openai import APIError, APIStatusError, RateLimitError, APITimeoutError
except Exception:  # pragma: no cover - best-effort import
    APIError = APIStatusError = RateLimitError = APITimeoutError = Exception  # type: ignore

from . import prompts

def _normalize_label_key(value: Any) -> str:
    """Normalizes label strings for consistent dictionary lookups."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).lower()

def _append_unique_label(collection: List[str], label: Optional[str]) -> None:
    """Appends a label to a collection if it is non-empty and not already present."""
    if not label:
        return
    cleaned = label.strip()
    if cleaned and cleaned not in collection:
        collection.append(cleaned)

INVALID_ESCAPE_PATTERN = re.compile(r'\\(?!["\\/bfnrtu])')

def _sanitize_invalid_json_escapes(value: str) -> str:
    """Escapes bare backslashes so json.loads can successfully parse LLM output."""
    if not value or not INVALID_ESCAPE_PATTERN.search(value):
        return value
    return INVALID_ESCAPE_PATTERN.sub(r"\\\\", value)

# --- MinHash Helpers ---

MINHASH_SEEDS: Tuple[int, ...] = tuple(range(1, 65))
MINHASH_SHINGLE_SIZE = 3

def _normalize_for_minhash(value: str) -> str:
    """Normalizes text for MinHash computation."""
    value = value.lower()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)

def _generate_shingles(text: str, width: int = MINHASH_SHINGLE_SIZE) -> List[str]:
    """Generates fixed-width character shingles for the provided text."""
    if not text:
        return [""]
    width = max(1, min(width, len(text)))
    return [text[i : i + width] for i in range(len(text) - width + 1)] or [text]

def _stable_hash(value: str, seed: int) -> int:
    """Produces a stable 64-bit hash for the given value and seed."""
    data = f"{seed}:{value}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big")

@lru_cache(maxsize=4096)
def _minhash_signature(value: str) -> Tuple[int, ...]:
    """Computes a MinHash signature for the provided value."""
    normalized = _normalize_for_minhash(value)
    shingles = _generate_shingles(normalized)
    signature: List[int] = []
    for seed in MINHASH_SEEDS:
        min_hash = min(_stable_hash(shingle, seed) for shingle in shingles)
        signature.append(min_hash)
    return tuple(signature)

def _build_signature_map(labels: List[str]) -> Dict[str, Tuple[int, ...]]:
    """Builds a lookup of labels to their MinHash signatures."""
    signature_map: Dict[str, Tuple[int, ...]] = {}
    for label in labels:
        cleaned = label.strip()
        if cleaned and cleaned not in signature_map:
            signature_map[cleaned] = _minhash_signature(cleaned)
    return signature_map

def _minhash_similarity(sig_a: Tuple[int, ...], sig_b: Tuple[int, ...]) -> float:
    """Computes the similarity between two MinHash signatures."""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return 0.0
    matches = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    return matches / len(sig_a)

def _best_minhash_match(value: str, signature_map: Dict[str, Tuple[int, ...]]) -> Optional[str]:
    """Finds the most similar label within the signature map for the provided value."""
    if not value.strip() or not signature_map:
        return None
    candidate_signature = _minhash_signature(value.strip())
    best_label: Optional[str] = None
    best_score = -1.0
    for label, signature in signature_map.items():
        score = _minhash_similarity(candidate_signature, signature)
        if score > best_score:
            best_label = label
            best_score = score
    return best_label

def _is_probably_english(value: str) -> bool:
    """Heuristically determines whether a label is English or Chinese."""
    ascii_letters = sum(1 for ch in value if ch.isascii() and ch.isalpha())
    non_ascii_letters = sum(1 for ch in value if not ch.isascii() and ch.isalpha())
    return ascii_letters >= non_ascii_letters

def _extract_label_code(value: Any) -> str:
    """Extracts the leading code portion from labels like 'E2-中国军事'."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    match = re.match(r"([A-Za-z0-9/]+)", text)
    if match:
        return match.group(1)
    if "-" in text:
        return text.split("-", 1)[0].strip()
    if " " in text:
        return text.split(" ", 1)[0].strip()
    return text

def _register_labels(mapping: Dict[str, Dict[str, str]], entry: Dict[str, str], *labels: str) -> None:
    """Registers multiple label variants (code + full name) for lookup."""
    for label in labels:
        if not label:
            continue
        normalized_label = _normalize_label_key(label)
        if normalized_label and normalized_label not in mapping:
            mapping[normalized_label] = entry
        code = _extract_label_code(label)
        normalized_code = _normalize_label_key(code)
        if normalized_code and normalized_code not in mapping:
            mapping[normalized_code] = entry

def _lookup_label(mapping: Dict[str, Dict[str, str]], value: str) -> Optional[Dict[str, str]]:
    """Finds the best matching label entry from a user/LLM-provided value."""
    for candidate in (value, _extract_label_code(value)):
        normalized = _normalize_label_key(candidate)
        if not normalized:
            continue
        entry = mapping.get(normalized)
        if entry:
            return entry
    return None

# --- Configuration ---

_DATA_INDEX_CONFIG_PATH = (
    Path(__file__).resolve().parent / "configs" / "data_index_config.json"
)


def _load_llm_settings() -> Dict[str, str]:
    """Loads API credentials for the data index pipeline."""
    try:
        with _DATA_INDEX_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Data index config not found at {_DATA_INDEX_CONFIG_PATH}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse {_DATA_INDEX_CONFIG_PATH}: {exc}"
        ) from exc

    api_key = (
        data.get("api_key")
        or os.environ.get("DATA_INDEX_API_KEY")
        or os.environ.get("API_KEY")
    )
    required_keys = ("api_base_url", "model_name")
    missing = [key for key in required_keys if not data.get(key)]
    if not api_key:
        missing.append("api_key")
    if missing:
        raise ValueError(
            f"Missing required fields {missing} in {_DATA_INDEX_CONFIG_PATH}; "
            "set DATA_INDEX_API_KEY or API_KEY for credentials"
        )
    return {
        "api_base_url": str(data["api_base_url"]),
        "model_name": str(data["model_name"]),
        "api_key": str(api_key),
    }


_llm_settings = _load_llm_settings()
API_BASE_URL = _llm_settings["api_base_url"]
MODEL_NAME = _llm_settings["model_name"]
API_KEY = _llm_settings["api_key"]

# Concurrency & retry knobs (fixed numbers per request)
MAX_CONCURRENCY = 36
MAX_RETRY_DELAY = 60

# Number of retries for each task type (now used for logging/backoff, not for stopping)
INFO_EXTRACTION_RETRIES = 10
DOMAIN_LABELING_RETRIES = 10
INDUSTRY_LABELING_RETRIES = 10

# HTTP/Request timeouts (fixed values)
CONNECT_TIMEOUT_S = 30.0
WRITE_TIMEOUT_S = 20.0
READ_TIMEOUT_S = 120.0
# Hard watchdog per API attempt to avoid indefinite hangs
REQUEST_ATTEMPT_TIMEOUT_S = 400.0

# --- Client Initialization ---
# Use a tuned HTTP client for high concurrency deployments.
_max_conn = max(64, MAX_CONCURRENCY * 4)
_keepalive = max(32, MAX_CONCURRENCY * 2)
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(
        connect=CONNECT_TIMEOUT_S,
        read=READ_TIMEOUT_S,
        write=WRITE_TIMEOUT_S,
        pool=REQUEST_ATTEMPT_TIMEOUT_S + 10,
    ),
    limits=httpx.Limits(max_connections=_max_conn, max_keepalive_connections=_keepalive),
    http2=False,  # Set True if your gateway supports HTTP/2
)
client = AsyncOpenAI(
    api_key=API_KEY,
    base_url=API_BASE_URL,
    http_client=http_client,
    max_retries=0,  # we implement our own backoff
)
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# --- Label Mapping and Normalization ---

# Maps language-agnostic codes to Chinese labels
DOMAIN_CODE_TO_ZH_MAP: Dict[str, Dict[str, str]] = {}
DOMAIN_SECONDARY_LABELS_ZH: List[str] = []
DOMAIN_SECONDARY_LABELS_EN: List[str] = []
for category in prompts.DOMAIN_CONFIG:
    primary_zh = category["一级标签"]
    secondary_en = category.get("二级标签_en", [])
    for idx, sec_zh in enumerate(category.get("二级标签", [])):
        sec_en = secondary_en[idx] if idx < len(secondary_en) else ""
        entry = {"domain_1": primary_zh, "domain_2": sec_zh}
        _register_labels(DOMAIN_CODE_TO_ZH_MAP, entry, sec_zh, sec_en)
        _append_unique_label(DOMAIN_SECONDARY_LABELS_ZH, sec_zh)
        _append_unique_label(DOMAIN_SECONDARY_LABELS_EN, sec_en)

INDUSTRY_CODE_TO_ZH_MAP: Dict[str, Dict[str, str]] = {}
INDUSTRY_TERTIARY_LABELS_ZH: List[str] = []
INDUSTRY_TERTIARY_LABELS_EN: List[str] = []
for l1_item in prompts.INDUSTRY_CONFIG:
    l1_name = l1_item["一级标签"]
    for l2_item in l1_item.get("二级标签", []):
        l2_name = l2_item["二级标签"]
        tertiary_cn = l2_item.get("三级标签", [])
        tertiary_en = l2_item.get("三级标签_en", [])
        for idx, l3_name in enumerate(tertiary_cn):
            l3_en_name = tertiary_en[idx] if idx < len(tertiary_en) else ""
            entry = {
                "industry_1": l1_name,
                "industry_2": l2_name,
                "industry_3": l3_name,
            }
            _register_labels(INDUSTRY_CODE_TO_ZH_MAP, entry, l3_name, l3_en_name)
            _append_unique_label(INDUSTRY_TERTIARY_LABELS_ZH, l3_name)
            _append_unique_label(INDUSTRY_TERTIARY_LABELS_EN, l3_en_name)

DOMAIN_MINHASH_SIGNATURES = {
    "zh": _build_signature_map(DOMAIN_SECONDARY_LABELS_ZH),
    "en": _build_signature_map(DOMAIN_SECONDARY_LABELS_EN),
}

INDUSTRY_MINHASH_SIGNATURES = {
    "zh": _build_signature_map(INDUSTRY_TERTIARY_LABELS_ZH),
    "en": _build_signature_map(INDUSTRY_TERTIARY_LABELS_EN),
}

def _soft_lookup_domain_label(value: str) -> Optional[Dict[str, str]]:
    """Uses MinHash similarity to find the closest domain label."""
    bucket = "en" if _is_probably_english(value) else "zh"
    signature_map = DOMAIN_MINHASH_SIGNATURES.get(bucket, {})
    matched_label = _best_minhash_match(value, signature_map)
    if not matched_label:
        return None
    return _lookup_label(DOMAIN_CODE_TO_ZH_MAP, matched_label)

def _soft_lookup_industry_label(value: str) -> Optional[Dict[str, str]]:
    """Uses MinHash similarity to find the closest industry label."""
    bucket = "en" if _is_probably_english(value) else "zh"
    signature_map = INDUSTRY_MINHASH_SIGNATURES.get(bucket, {})
    matched_label = _best_minhash_match(value, signature_map)
    if not matched_label:
        return None
    return _lookup_label(INDUSTRY_CODE_TO_ZH_MAP, matched_label)


# Maps English content types to Chinese content types for normalization
CONTENT_TYPE_EN_TO_ZH_MAP = {
    en.strip().lower(): zh.strip()
    for zh, en in zip(prompts.CONTENT_TYPES_ZH, prompts.CONTENT_TYPES_EN)
}

def _ensure_list(value: Any) -> List[str]:
    """Converts a string or list into a cleaned list of strings."""
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = re.split(r'[,\n;，、]+', value)
    else:
        return []
    return [str(item).strip() for item in values if str(item).strip()]

def _normalize_keywords(values: List[str]) -> List[str]:
    """Normalizes and deduplicates keywords."""
    return list(dict.fromkeys(values))

def _normalize_content_types(values: List[str], lang: str) -> List[str]:
    """
    Normalizes content type labels and ensures they are unique.
    """
    return list(dict.fromkeys([str(item).strip() for item in values if str(item).strip()]))[:3]

def _extract_response_labels(response: Dict[str, Any], candidate_keys: Tuple[str, ...]) -> List[str]:
    """Returns the first non-empty label list from the provided response keys."""
    for key in candidate_keys:
        if key not in response:
            continue
        values = _ensure_list(response.get(key))
        if values:
            return values
    return []

def get_domain_labels_from_codes(
    codes: List[str], allow_soft_matching: bool = False
) -> Tuple[List[str], List[str]]:
    """Derives primary and secondary Chinese domain labels from codes."""
    primary_list, secondary_list = [], []
    for code in codes:
        labels = _lookup_label(DOMAIN_CODE_TO_ZH_MAP, code)
        if not labels and allow_soft_matching:
            labels = _soft_lookup_domain_label(code)
        if not labels:
            continue
        if labels["domain_1"] not in primary_list:
            primary_list.append(labels["domain_1"])
        if labels["domain_2"] not in secondary_list:
            secondary_list.append(labels["domain_2"])
    return primary_list, secondary_list

def get_industry_labels_from_codes(
    codes: List[str], allow_soft_matching: bool = False
) -> Tuple[List[str], List[str], List[str]]:
    """Derives Chinese industry labels from tertiary label inputs."""
    primary_list, secondary_list, tertiary_list = [], [], []
    for code in codes:
        labels = _lookup_label(INDUSTRY_CODE_TO_ZH_MAP, code)
        if not labels and allow_soft_matching:
            labels = _soft_lookup_industry_label(code)
        if not labels:
            continue
        if labels["industry_1"] not in primary_list:
            primary_list.append(labels["industry_1"])
        if labels["industry_2"] not in secondary_list:
            secondary_list.append(labels["industry_2"])
        if labels["industry_3"] not in tertiary_list:
            tertiary_list.append(labels["industry_3"])
    return primary_list, secondary_list, tertiary_list

# --- Core LLM Interaction ---

def _decode_llm_json_payload(json_str: str) -> Dict[str, Any]:
    """Parses a JSON string, retrying with sanitized escapes if needed."""
    decoder = json.JSONDecoder()
    stripped = json_str.strip()
    try:
        parsed_json, _ = decoder.raw_decode(stripped)
        return parsed_json
    except json.JSONDecodeError:
        sanitized = _sanitize_invalid_json_escapes(stripped)
        if sanitized != stripped:
            parsed_json, _ = decoder.raw_decode(sanitized)
            return parsed_json
        raise


def _compute_retry_delay(attempt: int, retry_after: Optional[float]) -> float:
    """Computes exponential backoff with jitter."""
    if retry_after is not None:
        return max(1.0, retry_after)
    base = min(MAX_RETRY_DELAY, 2 ** (attempt - 1))
    jitter = random.uniform(0.2, 0.8)
    return max(1.0, base * jitter)


async def get_llm_response(
    system_prompt: str,
    user_prompt: str,
    task_name: str,
    max_retries: int,
    validation_fn: Optional[callable] = None,
    return_last_response_on_failure: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Generic LLM API call function with bounded retries, JSON parsing,
    and validation logic.
    """
    last_parsed_response: Optional[Dict[str, Any]] = None
    async with semaphore:
        for attempt in range(1, max_retries + 1):
            raw_response_for_log = "N/A"
            try:
                # Enforce a watchdog timeout around the SDK call to avoid hangs
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        extra_body={
                            "thinking": {"type": "enabled"},  # 深度思考
                        },
                        max_tokens=8000,
                    ),
                    timeout=REQUEST_ATTEMPT_TIMEOUT_S,
                )

                raw_content = response.choices[0].message.content
                if raw_content:
                    raw_content = raw_content.strip()
                else:
                    raw_content = ""
                raw_response_for_log = raw_content

                if "```json" in raw_content:
                    json_str = raw_content.split("```json", 1)[1].split("```", 1)[0]
                else:
                    json_str = raw_content

                start = json_str.find('{')
                end = json_str.rfind('}')
                if start != -1 and end != -1:
                    json_str = json_str[start : end + 1]

                parsed_json = _decode_llm_json_payload(json_str)
                last_parsed_response = parsed_json

                if validation_fn and not validation_fn(parsed_json):
                    raise ValueError(f"Response validation failed for task '{task_name}'.")

                return parsed_json

            except asyncio.TimeoutError:
                print(
                    f"Warning: Task '{task_name}' watchdog timeout after {REQUEST_ATTEMPT_TIMEOUT_S}s on attempt {attempt}/{max_retries}."
                )
                if attempt >= max_retries:
                    break
                delay = _compute_retry_delay(attempt, None)
                print(f"Info: Retrying task '{task_name}' in {delay:.2f} seconds.")
                await asyncio.sleep(delay)
                continue
            except (APITimeoutError, RateLimitError, APIStatusError, APIError, httpx.HTTPError) as e:
                # Special handling for 429/503 with Retry-After if available
                retry_after_s: Optional[float] = None
                if isinstance(e, APIStatusError):
                    try:
                        status = e.status_code  # type: ignore[attr-defined]
                        response_obj = getattr(e, "response", None)
                        headers = getattr(response_obj, "headers", {}) if response_obj else {}
                        if headers:
                            ra = headers.get("retry-after") or headers.get("Retry-After")
                            retry_after_s = float(ra) if ra else None
                        print(f"Warning: Task '{task_name}' APIStatusError {status} on attempt {attempt}: {e}. Raw: '{raw_response_for_log}'")
                    except Exception:
                        pass  # Best-effort parsing
                else:
                    print(f"Warning: Task '{task_name}' transient error on attempt {attempt}: {e}. Raw: '{raw_response_for_log}'")

                if attempt >= max_retries:
                    break
                delay = _compute_retry_delay(attempt, retry_after_s)
                print(f"Info: Retrying task '{task_name}' in {delay:.2f} seconds.")
                await asyncio.sleep(delay)
                continue
            except ValueError as e:
                print(
                    f"Warning: Task '{task_name}' validation failed on attempt {attempt}/{max_retries}: {e}. Raw response: '{raw_response_for_log}'"
                )
                if attempt >= max_retries:
                    break
                delay = _compute_retry_delay(attempt, None)
                print(f"Info: Retrying task '{task_name}' in {delay:.2f} seconds.")
                await asyncio.sleep(delay)
                continue
            except asyncio.CancelledError:
                # Propagate cancellations cleanly
                raise
            except Exception as e:
                # Non-retryable unexpected errors: do not spin forever
                print(f"Error: Task '{task_name}' encountered non-retryable error: {e}")
                return last_parsed_response if return_last_response_on_failure else None

    print(f"Error: Task '{task_name}' failed after {max_retries} attempts.")
    if return_last_response_on_failure and last_parsed_response is not None:
        return last_parsed_response
    return None

# --- Task-Specific Processors ---

async def run_info_extraction(text_content: str, lang: str) -> Dict[str, Any]:
    """Runs the information extraction task."""
    system_prompt = prompts.INFO_EXTRACTION_SYSTEM_PROMPT_ZH if lang == "zh" else prompts.INFO_EXTRACTION_SYSTEM_PROMPT_EN
    user_prompt = prompts.build_user_prompt(text_content, lang)
    
    validation_fn = lambda r: r.get("title") and r.get("content_type")
    
    response = await get_llm_response(
        system_prompt, user_prompt, "info_extraction", INFO_EXTRACTION_RETRIES, validation_fn=validation_fn
    )
    
    if not response:
        return {"title": "Untitled Document", "keyword": [], "content_type": []} # Fallback

    return {
        "title": str(response.get("title") or "Untitled Document").strip(),
        "author": str(response.get("author") or "").strip(),
        "public_date": str(response.get("public_date") or "").strip(),
        "isbn": re.sub(r'\s+', '', str(response.get("isbn") or "")),
        "doi": re.sub(r'\s+', '', str(response.get("doi") or "")),
        "issn": re.sub(r'\s+', '', str(response.get("issn") or "")),
        "abstract": str(response.get("abstract") or "").strip(),
        "keyword": _normalize_keywords(_ensure_list(response.get("keyword"))),
        "content_type": _normalize_content_types(_ensure_list(response.get("content_type")), lang),
    }

async def run_domain_labeling(text_content: str, lang: str) -> Dict[str, List[str]]:
    """Runs the domain labeling task."""
    system_prompt = prompts.DOMAIN_LABEL_SYSTEM_PROMPT_ZH if lang == "zh" else prompts.DOMAIN_LABEL_SYSTEM_PROMPT_EN
    user_prompt = prompts.build_user_prompt(text_content, lang)
    response_keys = ("domain_2_labels", "domain_2", "domain_2_codes")

    def _has_valid_domain_labels(response: Dict[str, Any]) -> bool:
        candidate_labels = _extract_response_labels(response, response_keys)
        _, domain_2_labels = get_domain_labels_from_codes(candidate_labels)
        return bool(domain_2_labels)
    
    response = await get_llm_response(
        system_prompt,
        user_prompt,
        "domain_labeling",
        DOMAIN_LABELING_RETRIES,
        validation_fn=_has_valid_domain_labels,
        return_last_response_on_failure=True,
    )

    if not response:
        return {"domain_1": [], "domain_2": []}

    candidate_labels = _extract_response_labels(response, response_keys)
    domain_1, domain_2 = get_domain_labels_from_codes(candidate_labels)
    if not domain_2:
        domain_1, domain_2 = get_domain_labels_from_codes(candidate_labels, allow_soft_matching=True)
    return {"domain_1": domain_1, "domain_2": domain_2}

async def run_industry_labeling(text_content: str, lang: str) -> Dict[str, List[str]]:
    """Runs the industry labeling task."""
    system_prompt = prompts.INDUSTRY_LABEL_SYSTEM_PROMPT_ZH if lang == "zh" else prompts.INDUSTRY_LABEL_SYSTEM_PROMPT_EN
    user_prompt = prompts.build_user_prompt(text_content, lang)
    response_keys = ("industry_3_labels", "industry_3")

    def _has_valid_industry_labels(response: Dict[str, Any]) -> bool:
        candidate_labels = _extract_response_labels(response, response_keys)
        _, _, industry_3_labels = get_industry_labels_from_codes(candidate_labels)
        return bool(industry_3_labels)

    response = await get_llm_response(
        system_prompt,
        user_prompt,
        "industry_labeling",
        INDUSTRY_LABELING_RETRIES,
        validation_fn=_has_valid_industry_labels,
        return_last_response_on_failure=True,
    )

    if not response:
        return {"industry_1": [], "industry_2": [], "industry_3": []}

    candidate_labels = _extract_response_labels(response, response_keys)
    industry_1, industry_2, industry_3 = get_industry_labels_from_codes(candidate_labels)
    if not industry_3:
        industry_1, industry_2, industry_3 = get_industry_labels_from_codes(
            candidate_labels, allow_soft_matching=True
        )
    return {"industry_1": industry_1, "industry_2": industry_2, "industry_3": industry_3}

async def process_text_with_llm(text_content: str, lang: str) -> Dict[str, Any]:
    """
    Main orchestrator to run all LLM tasks concurrently for a given text.
    """
    info_task = asyncio.create_task(run_info_extraction(text_content, lang))
    domain_task = asyncio.create_task(run_domain_labeling(text_content, lang))
    industry_task = asyncio.create_task(run_industry_labeling(text_content, lang))
    
    info_result, domain_result, industry_result = await asyncio.gather(info_task, domain_task, industry_task)

    # Combine results
    final_result = {**info_result, **domain_result, **industry_result}
    return final_result
