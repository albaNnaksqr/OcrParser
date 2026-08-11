# OcrParser 生产部署套件

一套公开的 Ansible + systemd 参考方案,用于将 OcrParser Control 和 worker
agent 部署到普通 Linux 主机上。它以不可编辑(non-editable)虚拟环境的方式
安装一个经过校验的确切版本,并通过 systemd 运行。它**不**负责基础设施
的配置:PostgreSQL、共享文件系统和模型服务都是由你自行拥有和运维的外部
系统;本套件只会*校验*这些系统是否可达、配置是否正确,绝不会去配置、
挂载、格式化或启动它们。

## 执行前先了解:sudo 会做什么

Ansible 通过 `become` 完成下列受控操作,不会执行未在 playbook 中声明的
任意 shell 命令。表中路径是默认值;如果 inventory 显式覆盖路径,
`playbooks/privilege-check.yml` 会在任何修改前打印该主机最终使用的准确
路径。

| 等效系统操作 | 默认目标 | 为什么需要 sudo |
| --- | --- | --- |
| `sudo -n true` | 不写入任何路径 | 只读确认部署账户能非交互提权;失败时立即停止,不会触发密码提示或修改主机。 |
| 创建系统用户和组 | 用户 `ocr-platform`,组 `ocr-runtime` | 让 Control 和 Worker 最终以专用非 root 身份运行。 |
| 创建目录并设置 owner/mode | `/opt/ocr-platform` 及其 `source/`、`venv/`、`wheels/` | 保存经过 commit、tag、SHA256 和 provenance 校验的固定版本,并阻止普通用户替换生产代码。 |
| 写入 root-owned `0600` 环境文件 | `/etc/ocr-platform/control.env`、`/etc/ocr-agent/worker.env` | systemd 需要读取运行配置和密钥;`0600` 防止任何非 root 普通账号直接读取。 |
| 创建 Worker 状态目录并设置权限 | `/var/lib/ocr-agent/<worker-id>/work`、`/var/lib/ocr-agent/<worker-id>/spool`、`/var/log/ocr-agent/<worker-id>` | 保存工作状态、断线待重放数据和日志,使重启与网络中断后能够恢复。 |
| 写入 systemd unit | `/etc/systemd/system/ocr-platform-control.service`、`/etc/systemd/system/ocr-agent-worker.service` | 由 systemd 统一管理启动、重启、资源隔离和开机自启。 |
| `systemctl daemon-reload`、`enable`、`start/restart` | 上述 Control/Worker unit | 让新 unit 生效,启用开机启动,并仅在配置或版本变化后重启对应服务。 |
| 可选 canary 目录的创建与清理 | `<ocr_shared_root>/canary` | 仅在显式启用 canary 时放置公开 synthetic 测试输入和产物;完成后清理任务文件。 |

sudo **不会**用于安装系统包、创建 PostgreSQL、创建数据库用户、挂载或
格式化存储、启动模型服务、修改防火墙/DNS/TLS/SSH,或降级数据库。
Control migration 使用你另行提供的数据库账号执行,不属于 sudo 操作。

本目录是公开的。所有示例文件(`inventory.example.yml`、
`group_vars/all.example.yml`)都只包含不可解析的 `.invalid` 占位符。你的
真实 inventory、group vars 和密钥应存放在一个独立的私有仓库中,绝不能
提交到这里。

## sudo 如何被授权

这是 systemd 生产安装,不是无权限 quickstart。在任何修改发生前,
`playbooks/privilege-check.yml` 会展示当前主机的准确权限契约。最终服务以
`ocr-platform` 而非 root 身份运行。

默认 `become_ask_pass=false`,不会突然弹出密码框。只读检查仅接受 root、
`sudo -n true`,或运维人员主动通过 `--ask-become-pass` 提供的凭据;否则在
创建任何内容前停止。

## 拓扑结构

- 恰好**一台 Control 主机**(`ocr_control` 组),提供 HTTP API 并协调
  任务。
- **N 台 worker 主机**(`ocr_workers` 组),每台都有唯一的
  `ocr_worker_server_id`,轮询 Control 并处理分片(shard)。
- **外部 PostgreSQL** —— Control 的数据库。由你自行配置和运维;本套件
  只会校验其可达性,并在设置了
  `ocr_control_require_current_migrations` 时校验迁移是否为最新。它绝不
  会执行 `CREATE DATABASE`,不会配置用户,也绝不会降级 schema。
- **外部共享文件系统** —— 由你在每台主机上挂载到 `ocr_shared_root`
  (NFS、云文件系统或其他任何适合你环境的方案)。本套件只会校验挂载点
  及 `ocr_input_root` / `ocr_output_root` / `ocr_platform_root` 子路径
  存在且可用。
- **外部模型服务**(例如 DotsOCR 端点)—— 仅供 canary 使用,且仅通过
  URL、engine、模型名称引用。本套件绝不会部署或启动该服务,也不会持有
  除环境变量名之外的任何凭据。
- Control 前端的 TLS 终止由你自行负责。Control 在
  `ocr_control_listen_host:ocr_control_listen_port` 上绑定纯 HTTP;
  `ocr_control_url` 是 worker 和运维人员对外使用的地址。

## 运维前置条件

在针对真实主机运行任何 playbook 之前:

1. PostgreSQL 已运行,可从 Control 主机访问,且其连接串以
   `ocr_database_url_secret_var` 所命名的环境变量形式,存在于运行
   `ansible-playbook` 的控制机上——绝不能以字面值形式出现在任何
   inventory 文件中。
2. 共享文件系统已在 Control 和每台 worker 上挂载到 `ocr_shared_root`,
   且 `ocr_input_root`、`ocr_output_root`、`ocr_platform_root` 均已存在
   并可被 `ocr_service_user` 写入。
3. 已具备对 `ocr_control` 和 `ocr_workers` 组中每台主机的 SSH 访问权限
   及 `become` 提权权限。
4. 拥有一个独立于本公开仓库的私有仓库,存放真实的 `inventory.yml` 和
   可选的 `group_vars/all.yml`,绝不提交到本仓库。
5. Ansible 控制机使用 `requirements.txt` 固定的 2.19 系列;每台受管主机
   的 `ocr_python_interpreter` 处提供版本 >= `ocr_min_python_version` 的
   Python。
6. 每次上线前都运行 `scripts/validate_inventory.py` 并针对你的私有
   inventory 校验通过——Ansible 自身的断言会重复其中最关键的安全检查,
   但该校验器能更早发现结构性问题和密钥处理错误,并给出更清晰的提示。
7. 在每台受管主机上安装 Parser 所需的原生运行库。Debian/Ubuntu 使用
   `libgl1`、`libglib2.0-0` 和 `libzbar0`;RHEL 系使用对应的
   `mesa-libGL`、`glib2` 和 `zbar`。`preflight.yml` 会验证 OpenCV 与
   pyzbar 能否导入,缺失时给出明确错误并拒绝继续部署。

## 精简私有 inventory 与密钥分离

- 将 `inventory.example.yml` 复制为私有部署仓库中的 `inventory.yml`。
  默认只需填写 release tag、共享根目录、PostgreSQL host、两个密钥环境
  变量引用、一个 Control URL 和 Worker 主机别名。只有需要高级覆盖时才
  复制 `group_vars/all.example.yml`。
- 服务用户、安装路径、共享子目录、Control 监听地址、PostgreSQL 端口、
  rollout 策略、Worker ID 及其运行目录均由安全默认值派生;旧版完整
  inventory 仍然兼容。
- 任何 inventory 或 group-vars 文件都不能包含字面密钥。任何看起来像
  token、密码、密钥或数据库连接串的字段,只允许使用以 `_secret_var`、
  `_env_var` 或 `_secret_ref` 结尾的键名;这些键名指向一个环境变量,由
  各角色在运行时以 `no_log: true` 方式解析。校验器会拒绝其他任何写法。
- 这些环境变量的解析方式由你自己的密钥管理体系决定(vault、CI 密钥
  仓库、运维人员的 shell 等)——本套件刻意不内置自己的密钥后端。

## Release 身份解析

从发布 `deployment-manifest.json` 的版本开始,inventory 只需填写
`ocr_release_tag`。控制机从对应 GitHub Release 下载 manifest;内网环境可
使用 `ocr_release_manifest_url`。之后仍会校验 tag 指向、干净源码、wheel
SHA256 和已安装 wheel provenance,不会降低供应链门禁。

Tag CI 会把 wheel 和 `deployment-manifest.json` 一起保存为发布操作员使用的
artifact。操作员必须将这两个未经修改的文件附加到同名 GitHub Release;
受管主机不会自行生成发布身份。

历史 `v0.4.0` 和离线部署需要同时显式填写 `ocr_release_commit`、
`ocr_source_repo_url`、`ocr_wheel_url`、`ocr_wheel_sha256`。四项必须完整
提供;部分填写或混用 manifest/显式身份都会被拒绝。

## 命令执行顺序

在 `deploy/production/ansible/` 目录下,指向你的私有 inventory 依次运行:

```
ansible-playbook -i /path/to/private/inventory.yml playbooks/privilege-check.yml
python3 scripts/validate_inventory.py /path/to/private/inventory.yml --json
ansible-playbook -i /path/to/private/inventory.yml playbooks/preflight.yml
ansible-playbook -i /path/to/private/inventory.yml playbooks/control.yml
ansible-playbook -i /path/to/private/inventory.yml playbooks/workers.yml
ansible-playbook -i /path/to/private/inventory.yml playbooks/verify.yml
# 可选,需显式开启:
ansible-playbook -i /path/to/private/inventory.yml playbooks/canary.yml -e ocr_canary_enabled=true
```

- **privilege-check.yml** —— 只读展示全部提权路径和操作,验证非交互 sudo
  (或主动提供的 become 凭据),不修改任何内容。
- **preflight.yml** —— 在 `ocr_control` 和 `ocr_workers` 中的每台主机上,
  将经过校验和(checksum)确认的确切版本安装到虚拟环境中。一旦发现版本
  标识不一致、磁盘空间不足、Python 版本过旧,或源码检出不干净/不匹配,
  即会失败并中止(fail closed)。不会启动任何服务。
- **control.yml** —— 配置并(重新)启动 Control 的 systemd 服务。
  `serial: 1`、`max_fail_percentage: 0` —— Control 主机应恰好只有一台,
  一旦拓扑不符或执行失败,本 play 会拒绝继续。
- **workers.yml** —— 对 `ocr_workers` 进行滚动部署,批次大小为
  `ocr_rollout_serial`;若某一批次的失败率超过
  `ocr_rollout_max_fail_percentage`,则整个 play 中止。必须在
  `control.yml` 成功之后才能运行:worker 会向 `ocr_control_url` 注册,
  否则拒绝启动。
- **verify.yml** —— 检查 Control 的 `/healthz`、`/readyz` 和
  `/source.json`,确认 inventory 中的每台 worker 都已注册且版本符合预期,
  并校验外部 PostgreSQL 与共享文件系统依赖是否可达。会写入一份经过脱敏
  的报告(见下文)。应在每次 `control.yml` / `workers.yml` /
  `rollback.yml` 之后运行。
- **canary.yml** —— 仅在显式开启时运行;若未传入
  `ocr_canary_enabled=true`,会拒绝执行。应在 `verify.yml` 通过之后运行。

## Check 模式的局限性

对本套件而言,`ansible-playbook --check` **不是**可靠的演练手段。具体
而言:

- `common` 角色中的版本标识、Python 版本和磁盘空间断言会执行真实命令
  (`git`、`df`、一个 Python 子进程),在 check 模式下能如实报告;但
  `git checkout`、wheel 下载以及虚拟环境/pip 安装任务会被 Ansible 的
  check 模式语义跳过——因此紧随其后的构建溯源(build-provenance)断言
  也会一并被跳过,而不是真正校验。
- `control` 和 `worker` 角色中的 systemd 服务重启在 `--check` 下是空
  操作,因此在同一次 check 模式运行中执行 `verify.yml`,观察到的将是
  *此前*正在运行的版本,而不是本次要检查的版本。
- `--check` 只应用于在正式执行前对主机连通性和变量解析做初步检查,
  绝不能作为上线会成功的证据。

## 失败与回滚流程

如果 `control.yml`、`workers.yml` 或 `verify.yml` 执行到一半失败:

1. 如果失败发生在 `workers.yml` 的某个 serial 批次中,不要盲目地对整个
   集群重新执行——此时部分 worker 已在新版本上,部分还未更新。重新执行
   本身是安全的(每个任务都是幂等的),但请先用 `verify.yml` 确认各主机
   当前所处的状态。
2. 若要回退到已知良好的版本,使用 `rollback.yml`。它要求在调用时通过
   `-e` 显式提供以下参数(绝不使用默认值):
   - `ocr_rollback_release_tag`、`ocr_rollback_release_commit`、
     `ocr_rollback_wheel_url`、`ocr_rollback_wheel_sha256` —— 确切的
     前一个版本标识。
   - 以下两者中**恰好选择一个**:`ocr_rollback_migration_compatible=true`
     (表示旧版本代码与*当前*、未被降级的数据库 schema 兼容),或
     `ocr_rollback_restore_plan_reference="..."`(指向你自己制定的、
     用于协调 schema 与代码的带外(out-of-band)方案的引用)。
   本套件本身绝不会降级数据库;它只会在你未提供上述两种兼容性断言之一
   时拒绝继续执行。
3. `rollback.yml` 会先回滚 Control(`serial: 1`、
   `max_fail_percentage: 0`),再以与正向上线相同的 `ocr_rollout_serial`
   / `ocr_rollout_max_fail_percentage` 约束回滚 worker。
4. 任何一次回滚之后都应再次运行 `verify.yml`。

示例:

```
ansible-playbook -i /path/to/private/inventory.yml playbooks/rollback.yml \
  -e ocr_rollback_release_tag=v0.3.2 \
  -e ocr_rollback_release_commit=<40 位十六进制 commit> \
  -e ocr_rollback_wheel_url=https://artifacts.example.invalid/ocrparser/v0.3.2/ocrparser_platform-0.3.2-py3-none-any.whl \
  -e ocr_rollback_wheel_sha256=<64 位十六进制 sha256> \
  -e ocr_rollback_migration_compatible=true
```

## 报告与脱敏

`verify.yml` 和 `canary.yml` 每次运行都会在 `ocr_report_dir`(默认为
`reports`,存放于控制机本地,已被 gitignore)下写入一份结构化的 JSON
事实文件。报告只包含白名单内的版本标识、健康结果、Worker 数量以及
(canary 场景下)任务完成状态,不包含主机名、地址、路径、文档正文、密钥、
token 或数据库连接串。报告属于部署证据而非仓库源码;即使渲染器还会
进行防御性脱敏,也不要将这些文件提交到版本库。

## 无远程管理(No Remote Admin)

`ocr_control_enable_remote_admin` 默认值为 `0`,本套件绝不会覆盖它。
削弱该项设置,以及 `group_vars/all.example.yml` 中标注为会影响文档所
述安全姿态的其他任何设置(`ocr_control_require_postgres`、
`ocr_control_auto_migrate`、`ocr_control_require_current_migrations`、
`ocr_control_api_auth_required`、`ocr_control_allow_saved_model_profile_keys`),
都是你在自己的私有 `group_vars/all.yml` 中做出的、针对具体部署的审慎
决定——本套件绝不会替你做这个决定,也不应在未记录原因的情况下随意
更改。
