# v0.2 安全迁移

v0.2 有四项有意调整的默认行为：

1. Control 默认监听 `127.0.0.1`。如果 `OCR_PLATFORM_HOST` 不是 loopback，必须
   配置非空 `OCR_PLATFORM_API_TOKEN`，否则拒绝启动。
2. `/api/remote-workers/*` 路由继续保留，但只有设置
   `OCR_PLATFORM_ENABLE_REMOTE_ADMIN=1` 后才可操作；默认返回 `403`。
3. UI bearer token 改存 `sessionStorage`，浏览器会话结束后自动清除。
4. 新 model profile 和单任务请求默认不能把 API key 保存到数据库，应该使用
   `api_key_env_var`。已有 key 不会自动删除，Deployment Doctor 会提示迁移。
   `OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS=1` 只用于短期兼容，不建议用于生产。

将 Control 暴露到 localhost 之外前，应在进程环境中配置强 token，确认反向代理
TLS，并在完成主机清单和 SSH 策略审查前保持 Remote Admin 关闭。
