# src/utils.py
import os
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from dotenv import load_dotenv

def load_config(config_name: str):
    """
    Loads a JSON configuration file from the 'configs' directory.
    """
    config_path = Path(__file__).resolve().parent / "configs" / config_name
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_environment():
    """
    Loads environment variables from the .env file.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    load_dotenv(dotenv_path=env_path)

def normalize_endpoint(endpoint: str) -> str:
    """
    Normalize an endpoint URL for comparison (trim spaces and trailing slash).
    """
    return (endpoint or "").strip().rstrip("/")

def load_datasource_mapping(config_name: str = "datasource_mapping.json") -> Dict:
    """
    Safely load the datasource mapping config; returns {} when missing/invalid.
    """
    try:
        return load_config(config_name)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def resolve_datasource_id_from_mapping(
    endpoint: str,
    bucket_name: str,
    mapping_data: Dict,
) -> Optional[str]:
    """
    Given endpoint + bucket, return the datasource_id from mapping config.
    """
    if not mapping_data:
        return None
    entries = mapping_data.get("mappings") or []
    normalized_endpoint = normalize_endpoint(endpoint).lower()
    normalized_bucket = (bucket_name or "").strip().lower()
    for entry in entries:
        ep = normalize_endpoint(entry.get("endpoint", "")).lower()
        bucket = (entry.get("bucket_name", "")).strip().lower()
        datasource_id = entry.get("datasource_id") or entry.get("datasourceID")
        if ep and bucket and datasource_id and ep == normalized_endpoint and bucket == normalized_bucket:
            return str(datasource_id)
    default_ds = mapping_data.get("default_datasource_id")
    return str(default_ds) if default_ds else None

def read_text_file(file_path: Path, max_chars: int = -1):
    """
    Reads content from a text file with various encodings.
    
    Args:
        file_path (Path): The path to the file.
        max_chars (int): The maximum number of characters to read. -1 for unlimited.
    
    Returns:
        str: The content of the file.
    """
    content = ""
    encodings_to_try = ['utf-8', 'gbk', 'latin1']
    
    for encoding in encodings_to_try:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                if max_chars == -1:
                    content = f.read()
                else:
                    content = f.read(max_chars)
            break # Stop if successful
        except (UnicodeDecodeError, FileNotFoundError):
            continue
            
    return content

def ensure_parent_dir(file_path: Path):
    """
    Ensures the parent directory for the target file exists.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

def chunk_list(items: List, size: int) -> Iterable[List]:
    """
    Yields chunks of a list with the given size.
    """
    for i in range(0, len(items), size):
        yield items[i:i + size]

def has_min_chinese_chars(text: str, threshold: int = 5) -> bool:
    """
    Checks if the text has at least `threshold` Chinese characters.
    """
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
    return len(chinese_chars) >= threshold

# Initial load of environment variables when the module is imported
load_environment()
