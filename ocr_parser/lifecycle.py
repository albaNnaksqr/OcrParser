from __future__ import annotations

async def initialize(self):
    from dots_ocr.model.inference_async import get_async_client, warmup_gpu

    if not self.use_hf:
        self._console_write("Initializing async client...")
        concurrent_retries = getattr(self, "concurrent_retries", 4) or 4
        max_connections = int(self.page_concurrency * max(1, concurrent_retries) * 1.2) + 10
        self.client = await get_async_client(self.ip, self.port, self.timeout, max_connections, self.api_key)
        if self.enable_warmup:
            await warmup_gpu(self.ip, self.port, self.model_name, api_key=self.api_key)
    return self


async def shutdown(self):
    from dots_ocr.model.inference_async import close_all_clients

    self._console_write("Shutting down resources...", level="info")
    await close_all_clients()
    if self.process_pool:
        self.process_pool.shutdown(wait=True)
    if self._table_ocr_executor:
        self._table_ocr_executor.shutdown(wait=True, cancel_futures=True)
    self._console_write("Shutdown complete.", level="info")
