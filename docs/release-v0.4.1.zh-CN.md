# v0.4.1 发布说明

[English](release-v0.4.1.md) | 中文

`0.4.1` 是一个以生产部署为重点的维护版本。它新增适用于一台 Control 和多台
Worker 的 Ansible + systemd 参考套件，不改变 Parser、HTTP、数据库、manifest、
输出、调度或引擎行为。

## 主要内容

- 精简 inventory 从少量基础设施信息派生服务身份、路径、Worker ID、滚动策略和
  Control 安全默认值。
- 只读权限检查在任何主机修改前解释全部 sudo 路径与 systemd 操作；无法进行
  非交互提权时不提示密码并直接停止。
- Release 身份解析校验 tag target、干净源码 revision、wheel SHA256 和已安装
  build provenance。
- 准确 Release 源码包含完整生产依赖约束；约束缺失时 Bundle 拒绝安装，避免同一
  wheel 在不同主机或不同日期解析出不同依赖。
- 包含部署后 verify、可选 synthetic canary、脱敏证据报告、Worker 滚动上线和
  fail-closed rollback。

## 部署边界

Bundle 只校验、不创建 PostgreSQL、共享存储、模型服务、TLS、DNS、防火墙或 SSH
配置。受控安装完成后，服务以专用非 root 账号运行。

请按照[生产部署指南](../deploy/production/ansible/README.zh-CN.md)执行。`v0.4.1`
GitHub Release 会同时提供 release wheel 与 `deployment-manifest.json`；运维人员
必须使用这些准确资产。
