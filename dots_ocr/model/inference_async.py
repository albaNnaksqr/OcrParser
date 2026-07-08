import json
import io
import base64
import math
import time
import asyncio
from PIL import Image
Image.MAX_IMAGE_PIXELS = 200000000
from dots_ocr.utils.image_utils import PILimage_to_base64
from openai import AsyncOpenAI, APIError
import os
import httpx
from typing import Optional, Dict, Any
import random

def _bytes_to_data_url(b: bytes, mime_hint: Optional[str] = None) -> str:
    """把二进制图片转成 data URL；尽量自动嗅探 MIME。"""
    if not mime_hint:
        if b.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif b.startswith(b"\xff\xd8"):
            mime = "image/jpeg"
        elif len(b) > 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/jpeg"
    else:
        mime = mime_hint
    enc = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{enc}"

def _pil_to_jpeg_bytes(img: Image.Image, quality: int = 92) -> bytes:
    """把 PIL 图转换成 JPEG 二进制（RGB）"""
    bio = io.BytesIO()
    (img.convert("RGB") if img.mode != "RGB" else img).save(bio, "JPEG", quality=quality)
    return bio.getvalue()

def _coerce_image_to_data_url(image: Any) -> str:
    """
    统一把多种输入形式变成 data-URL 字符串：
    - PIL.Image.Image
    - bytes / bytearray（JPEG/PNG/WebP等）
    - str: 若以 'data:image/' 开头直接返回；若是本地路径则读文件；否则报错
    """
    # 已是 data URL
    if isinstance(image, str) and image.strip().lower().startswith("data:image/"):
        return image

    # 二进制
    if isinstance(image, (bytes, bytearray)):
        return _bytes_to_data_url(bytes(image))

    # PIL 图
    if isinstance(image, Image.Image):
        return _bytes_to_data_url(_pil_to_jpeg_bytes(image))

    # 本地路径
    if isinstance(image, str) and os.path.exists(image):
        with open(image, "rb") as f:
            b = f.read()
        return _bytes_to_data_url(b)

    raise TypeError(
        "Unsupported image type for inference_with_vllm: "
        "expect PIL.Image, bytes/bytearray, data-URL string, or local file path string."
    )


async def prepare_image_payload_for_vllm(image: Any) -> tuple[str, float]:
    """
    Convert an image input into a reusable data URL without blocking the event loop.

    Large file reads and base64 encoding are pushed to a worker thread so the
    asyncio scheduler can keep feeding other OCR requests.
    """
    start_time = time.perf_counter()
    if isinstance(image, str) and image.strip().lower().startswith("data:image/"):
        return image, 0.0

    payload = await asyncio.to_thread(_coerce_image_to_data_url, image)
    return payload, time.perf_counter() - start_time


# 全局客户端池，避免重复创建连接
_client_pool = {}
_client_pool_lock = asyncio.Lock()

async def get_async_client(ip="localhost", port=8000, timeout=100, max_connections=None, api_key: Optional[str] = None):
    """
    获取或创建复用的异步客户端。
    现在总是优先使用传入的 api_key。
    """
    # 使用 API Key 作为客户端池的一部分键，确保不同 Key 使用不同客户端
    addr = f"http://{ip}:{port}/v1"
    client_key = f"{addr}_{timeout}_{max_connections or 'default'}_{api_key or 'default_key'}"
    
    async with _client_pool_lock:
        if client_key not in _client_pool:
            print(f"Creating new async client for {addr} with max_connections={max_connections or 50}")
            
            # 动态调整连接池大小
            max_connections = max_connections or 50
            max_keepalive = min(max_connections // 2, 20)
            
            timeout_value = float(timeout) if timeout is not None else 100.0
            # Fail fast when the endpoint is unreachable: keep a short connect timeout even if
            # the overall request timeout is configured very large (e.g. 21600s).
            connect_timeout = min(10.0, timeout_value) if timeout_value > 0 else 10.0
            httpx_timeout = httpx.Timeout(timeout_value, connect=connect_timeout)

            httpx_client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive,
                    keepalive_expiry=30
                ),
                timeout=httpx_timeout
            )
            
            # 明确的 API Key 处理逻辑
            # 优先使用函数传入的 api_key，其次是环境变量，最后是默认值 "0"
            final_api_key = api_key if api_key is not None else os.environ.get("API_KEY", "0")
            if final_api_key == "0":
                print("Warning: API Key is not provided, using default value '0'. This might fail if the server requires authentication.")

            client = AsyncOpenAI(
                api_key=final_api_key, 
                base_url=addr, 
                timeout=timeout,
                http_client=httpx_client
            )
            _client_pool[client_key] = client
        
        return _client_pool[client_key]

async def close_all_clients():
    """关闭所有客户端连接"""
    async with _client_pool_lock:
        clients_to_close = list(_client_pool.values())
        _client_pool.clear()
        for client in clients_to_close:
            await client.close() # 正确关闭httpx客户端


async def warmup_gpu(ip="localhost", port=8000, model_name='model', api_key: Optional[str] = None):
    print("Starting GPU warmup...")
    dummy_image = Image.new('RGB', (100, 100))
    dummy_prompt = "warmup"
    try:
        client = await get_async_client(ip, port, timeout=30, api_key=api_key)
        # 也可以预编码为 bytes（非必需）
        # payload = _pil_to_jpeg_bytes(dummy_image)
        result = await inference_with_vllm(
            dummy_image,
            dummy_prompt,
            ip=ip,
            port=port,
            model_name=model_name,
            max_completion_tokens=128,
            timeout=30,
            client=client,
            api_key=api_key
        )
        print("GPU warmup completed successfully")
        return result is not None
    except Exception as e:
        print(f"GPU warmup failed: {e}")
        return False

async def inference_with_vllm(
        image,
        prompt, 
        ip="localhost",
        port=8000,
        temperature=0.1,
        top_p=0.9,
        frequency_penalty=0,
        max_completion_tokens=32000,
        model_name='model',
        timeout=100,
        client=None,
        max_retries=3,
        retry_delay=1.0,
        retry_callback=None,
        api_key: Optional[str] = None  # 添加了 api_key 参数
        ):
    """
    适配后的版本：image 可为 PIL.Image / bytes / data-URL 字符串 / 本地路径字符串。
    - 只在函数开头把 image 统一成 data-URL，一次完成，重试与并发不再重复编码。
    - 与上层“竞速重试前先把 processed_image 编成 bytes 再多路并发”完全兼容。
    """
    # 如果 client 为 None，则使用 api_key 创建新客户端
    if client is None:
        client = await get_async_client(ip, port, timeout, api_key=api_key)

    # ★ 只做一次：把任意输入统一成 data URL
    try:
        if isinstance(image, str) and image.strip().lower().startswith("data:image/"):
            data_url = image
        else:
            data_url, _ = await prepare_image_payload_for_vllm(image)
    except Exception as e:
        raise TypeError(f"[inference_with_vllm] Failed to coerce image to data-URL: {e}") from e

    # messages 只构造一次；重试不再复制或重建大字符串
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": f"<|img|><|imgpad|><|endofimg|>{prompt}"},
            ],
        }
    ]

    last_error = None
    last_exception: Optional[Exception] = None
    retry_count = 0

    for attempt in range(max_retries + 1):
        start_time = time.time()
        try:
            response = await client.chat.completions.create(
                messages=messages, 
                model=model_name, 
                max_tokens=max_completion_tokens,
                temperature=temperature,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                tool_choice="none"
            )
            response = response.choices[0].message.content

            elapsed_time = time.time() - start_time
            if attempt > 0:
                print(f"Inference completed in {elapsed_time:.2f}s (attempt {attempt + 1})")

            return response

        except httpx.TimeoutException as e:
            elapsed_time = time.time() - start_time
            last_error = f"TimeoutException: {e}"
            last_exception = e
            print(f"Request timed out after {elapsed_time:.2f}s (attempt {attempt + 1}/{max_retries + 1}): {e}")

        except APIError as e:
            elapsed_time = time.time() - start_time
            last_error = f"APIError: {e}"
            last_exception = e
            print(f"OpenAI API error after {elapsed_time:.2f}s (attempt {attempt + 1}/{max_retries + 1}): {e}")

        except Exception as e:
            elapsed_time = time.time() - start_time
            last_error = f"Unexpected error after {elapsed_time:.2f}s (attempt {attempt + 1}/{max_retries + 1}): {e}"
            last_exception = e
            print(last_error)

        if attempt < max_retries:
            delay = retry_delay * (2 ** attempt) + random.uniform(0, 0.5)
            print(f"Waiting {delay:.2f}s before retry...")
            await asyncio.sleep(delay)
            retry_count += 1

    if retry_callback and retry_count > 0:
        retry_callback(retry_count)

    print(f"All {max_retries + 1} attempts failed. Last error: {last_error}")
    if last_exception is not None:
        raise last_exception
    raise RuntimeError(f"All {max_retries + 1} attempts failed. Last error: {last_error}")
