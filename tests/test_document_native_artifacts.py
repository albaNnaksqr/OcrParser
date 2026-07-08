import asyncio


class Engine:
    async def finalize_document(self, page_results, save_dir, filename):
        return [{"engine": "mineru", "kind": "document_markdown", "path": f"{save_dir}/{filename}.md"}]


class Parser:
    ocr_engine = Engine()


def test_finalize_document_contract():
    artifacts = asyncio.run(Parser().ocr_engine.finalize_document([], "/tmp/doc", "doc"))
    assert artifacts == [{"engine": "mineru", "kind": "document_markdown", "path": "/tmp/doc/doc.md"}]
