"""
Data Index integration package.

Exposes the runner used by parser_async_v13 to trigger metadata extraction and
LLM-based indexing once OCR output is available.
"""

from .runner import DataIndexRunner, DataIndexJob

__all__ = ["DataIndexRunner", "DataIndexJob"]
