async def write_document_outputs(*args, **kwargs):
    from .markdown_writer import write_document_outputs as _write_document_outputs

    return await _write_document_outputs(*args, **kwargs)

__all__ = ["write_document_outputs"]
