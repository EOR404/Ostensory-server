# Ostensory 服务端

Ostensory 是一个面向 ComfyUI / AI 绘图工作流的自建图床。服务端负责网页、后台、图片处理、元数据解析和存储调度。

当前版本：**0.1.2**。

主要能力：

- 游客上传、管理员上传、相册、标签、NSFW/地区限制
- 从 PNG/JPEG/WebP/GIF 中解析 ComfyUI / A1111 参数
- 图片加密、回收站、去重、缩略图和预览图
- 多存储后端：本机、WebDAV、S3 兼容对象存储、Cloudflare Worker
- 按容量上限、使用率阈值和优先级自动切换存储
- 中英文界面、默认语言/日间夜间模式、RSS、公开 API、Windows 同步客户端
- 每用户个人配额、低分辨率预览开关、回收站保留策略、整站迁移

## 快速开始

要求 Python 3.10+，推荐 3.12。

```powershell
cd Ostensory-server
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python run.py
```

Linux/macOS：

```bash
cd Ostensory-server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

默认监听 `127.0.0.1:8000`，数据目录是 `data/`。首次打开：

```text
http://127.0.0.1:8000/admin
```

按页面提示创建管理员。之后可以在后台修改后台路径、上传限制、页脚、存储方式等。

> 只运行一个 Uvicorn worker。应用使用单进程内存限流和 SQLite WAL，多 worker 会破坏限流和部分一致性。

## 配置

环境变量都是可选的，常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OSTENSORY_HOST` | `127.0.0.1` | 监听地址 |
| `OSTENSORY_PORT` | `8000` | 监听端口 |
| `OSTENSORY_DATA_DIR` | `data` | 数据库、图片、密钥、缓存 |
| `OSTENSORY_SECURE_COOKIES` | `auto` | `auto` / `always` / `never` |
| `OSTENSORY_TRUSTED_PROXIES` | 本机 | 可信反向代理 CIDR，逗号分隔 |
| `OSTENSORY_MASTER_KEY` | 自动生成 | 64 位十六进制主密钥；不设置时写入 `data/master.key` |
| `OSTENSORY_GEOIP_DB` | 空 | MaxMind `Country.mmdb` 路径 |
| `OSTENSORY_SESSION_DAYS` | `7` | 后台登录有效天数 |
| `OSTENSORY_MIGRATION_STATE_DIR` | 数据目录上级 | 迁移前备份和恢复标记位置 |
| `OSTENSORY_MIGRATION_MAX_MB` | `0` | 迁移包上传上限（MB）；`0`/留空表示按磁盘容量不设应用层上限 |
| `OSTENSORY_MIGRATION_MAX_EXTRACTED_MB` | `0` | 迁移包解压后上限（MB）；`0`/留空表示不设应用层上限 |
| `OSTENSORY_MIGRATION_MAX_FILES` | `0` | 迁移包文件数量上限；`0`/留空表示不设上限 |
| `OSTENSORY_MIGRATION_MAX_MANIFEST_MB` | `0` | 迁移清单大小上限（MB）；`0`/留空表示不设上限 |

完整部署变量见 `deploy/ostensory.env.example`。

生产环境请放在 HTTPS 反向代理后面。静态资源使用同域 `/static/...`，不要改成绝对地址，否则 HTTPS 下会出现混合内容。

## 存储方式

后台 **存储** 页可以添加和排序多个后端：

1. **本机存储**：默认后端，文件在 `data/originals`、`data/thumbs`、`data/previews`。
2. **WebDAV**：填写 DAV 地址、用户名、密码，可加路径前缀。
3. **S3 兼容**：Cloudflare R2、阿里云 OSS、AWS S3、MinIO 等；填写 Endpoint、Bucket、Region、Access Key、Secret Key。
4. **Cloudflare Worker**：填 Worker 地址和管理 Token，适合零成本图床。

每种后端可设置：

- **容量上限（MB）**：只统计本应用写入的原始图片大小；`0` 表示不限。
- **切换阈值（%）**：已用达到上限的该比例后，新上传跳过这一种。
- **顺序**：数字越小越先使用；停用的后端会被跳过。

上传时按顺序选择第一个“未满且未到阈值”的后端；写入失败会自动尝试下一个。**已有图片不会自动迁移**，切换只影响新上传。所有后端都满时，后台上传会弹出提醒，可直接去回收站手动删除，或按最久未处理顺序自动永久删除回收站图片后重试。

## 回收站

后台导航的 **回收站** 页负责真正释放空间：

- 保留天数 `-1`：永久保留（默认）；`0`：关闭回收站，删除图片时立即永久删除并释放磁盘；正数：软删除，按天自动永久删除。
- 回收站里可以逐张或批量永久删除，也可以清空；`0` 模式下不会再产生新的回收站条目。
- 正数保留天数时，每张图片从自己的 `deleted_at` 单独计时；应用启动时、每小时、访问回收站/上传/容量检查时会清理已过期图片。
- 永久删除最后一张引用同一原图的记录后，才会删除文件并释放容量；删除操作不会影响其他人仍引用的同一原图。

> 自动删除不可恢复。生产环境建议先备份 `data/`，再开启较短保留天数。

远程对象使用 `remote/<后端ID>/...` 作为键，本机旧图片的键不变。远程图片首次读取会缓存到 `data/cache/`，删除图片时会同时删除远程对象和缓存。

> WebDAV 密码、S3 Secret Key、Worker Token 会使用 `data/master.key` 以 AES-GCM 加密，再保存到 `data/ostensory.sqlite3` 的 `storage_backends` 表。数据库仍需限制权限；如果替换或丢失 `master.key`，这些凭据将无法解密，需要重新填写。

## 默认外观与个人配额

后台 **设置** 页可以指定访客的默认语言（自动检测、中文、English）和默认界面模式（夜间、日间、跟随系统）。访客手动切换后，浏览器里的选择优先于站点默认值；管理员后台也使用同一套默认主题。

每用户配额在 **设置 → 用户配额** 和每个用户的编辑页管理：

- `0` 表示不限；已有账号升级后默认不限，避免锁定老用户。
- 新用户可统一分配固定配额（默认 1024 MB），也可以按“当前可用总容量 ÷ 活跃用户数”快照式弹性分配。弹性值只在开户时计算，不会随用量悄悄改写已有账号。
- 配额按账号名下所有图片原始大小累计，回收站里的图片仍占磁盘、也仍计入配额；只有彻底删除才释放。
- 配额满时默认拒绝上传并提示。可选“自动清理该用户自己的回收站”后重试；绝不会删除其他用户或前台仍展示的作品。
- 管理员给用户改配额、停用账号、调整上传开关互不影响；后台设置页还提供“恢复默认设置”，只重置站点设置，不删除图片、账号、API Token、存储凭据、密钥或已有用户配额。

## 相册封面与审核状态

- 相册封面优先选非 NSFW 的已公开图片；只有相册里没有非 NSFW 图片时，才用 NSFW 图片做封面。此时如果后台开启“模糊 NSFW”，封面会模糊并显示 NSFW 标记，不会把原图直接露出。
- 相册页的多张预览图同样先排非 NSFW 图片；没有别的选择时，每一张 NSFW 缩略图都会单独模糊，而不是只按整个相册的开关决定。
- 开启上传审核后，待审核图片只有上传者本人能在首页相册封面和相册内看到，并带有“待审核”标记；其他用户和访客看不到、也不会计入相册数量。
- 审核拒绝后图片立即改为“仅自己可见”，只出现在 **我的账户 → 未过审** 页，并显示审核备注和剩余保留天数。其他用户、访客和公开页面都看不到。
- 未过审保留天数在后台 **设置 → 用户、登录与审核** 中调整，默认 7 天；填 `0` 表示不自动清理。到期后图片移入回收站，仍可按回收站规则恢复或彻底删除，不会直接永久丢失。

## 普通用户的图片设置

登录后进入 **我的账户**，每张作品的“图片设置”可以修改：作品名称、纯文本简介、简介字号/样式/颜色/引号、所属相册、公开或仅自己、NSFW 标记、访问密码，以及实验性地区规则。图片详情页右上角也有入口，不用绕回账户页。

普通用户不能修改原图、原始文件名、SHA-256、上传时间、上传 IP、所有者或审核状态；不能启用 HTML/CSS 简介原样渲染（保存时强制关闭，防止公开页面执行不信任代码）；不能管理匣子、切换落盘加密、重新解析元数据或彻底删除绕过回收站。这些仍只在后台由管理员执行。管理员不受此限制。

## 预览图

后台设置可以关闭低分辨率预览图。关闭后新上传不再生成预览，详情页直接加载原图，列表页仍使用缩略图；已有预览文件立即停止使用但不会自动删除。关闭时页面会询问是否同时清理，平时也可以手动清理全部预览；原图和缩略图不受影响。

## 统计页

公开的 `/stats` 页除图片/相册数量、容量和元数据覆盖率外，还会按“使用该模型的图片数”列出底模和 LoRA：

- 一张图片使用多个 LoRA 时，每个 LoRA 各计一次；
- LoRA 名称只统计解析时处于启用状态的条目，停用或未接线的 LoRA 不计入；
- 底模优先读取 `base_model`，缺失时回退到第一个 Checkpoint / UNET；
- 后台“公开统计页”里的“元数据覆盖率、底模与 LoRA 使用排行”勾选项控制这些名称是否公开。

## Cloudflare Worker 图床

配套仓库：[Ostensory-worker](https://github.com/EOR404/Ostensory-worker)

Worker 可以独立当图床使用（R2 存图、D1 存信息、自带上传页），也可以作为本服务端的远程存储网关。部署完成后在后台新增“Cloudflare Worker”，填入地址和 `ADMIN_TOKEN` 即可。

## 管理命令

```powershell
python -m app.cli init
python -m app.cli show-admin-path
python -m app.cli set-admin-path my-secret
python -m app.cli reset-admin
python -m app.cli reset-password admin
python -m app.cli create-token "我的客户端"
python -m app.cli reparse
python -m app.cli update --check
python -m app.cli update
python -m app.cli geoip 8.8.8.8
```

忘记后台用户名或密码时运行 `python -m app.cli reset-admin`，按提示输入新用户名（回车保留当前）和新密码；命令会注销旧会话、撤销 API Token 并清除登录冷却。

## 版本更新

后台导航最下方的 **关于** 页显示当前版本，并提供自动检查和更新开关：

- **自动检查更新**：默认开启。发现新版本只提示，不修改文件。
- **自动更新**：默认关闭。开启后自动下载、校验、备份并替换程序文件，仍需重启服务才生效。

关闭自动更新时，管理员可在关于页点击“更新到最新版”。也可以使用命令行：

```powershell
python -m app.cli update --check   # 只检查
python -m app.cli update           # 确认后安装
python -m app.cli update --yes     # 脚本中免确认
python -m app.cli update --yes --restart  # 安装后执行环境变量里的重启命令
```

更新规则：

- 检查更新默认读取 GitHub Releases 的公开 Atom 订阅，并直接拼出 Release 资产地址；这条路径不消耗 GitHub API 限额，普通部署不需要配置 Token。GitHub API 只作为备用路径。
- 如果部署在 GitHub 之外的镜像，可用 `OSTENSORY_UPDATE_FEED` 指向 Atom 订阅，用 `OSTENSORY_UPDATE_DOWNLOAD_ROOT` 指向下载根地址（默认从订阅地址推导）。只有访问私有 Release 或需要 API 备用路径时才考虑 `OSTENSORY_UPDATE_TOKEN`。
- 始终按版本号选择最新 Release，直接跳版，不要求逐个版本升级。
- 当前版本含 `dev`/`alpha`/`beta`/`rc` 时允许预发布版本；稳定版只接受正式 Release。
- 更新器只替换 `app/`、`deploy/`、`run.py`、`requirements.txt` 等程序文件，**不会覆盖 `data/`**。数据库、图片、密钥、后台路径、账户和设置都保留。
- 安装前生成 `data/backups/update-<旧版本>-to-<新版本>-<时间>/`：其中 `code-before/` 是旧程序，`ostensory.sqlite3` 是 SQLite 一致性快照，另含 `master.key`、`secret.key` 和清单。
- 发布包必须有 SHA256 校验；校验失败、版本号不一致、包含路径穿越/符号链接、解压过大都会停止。
- 更新后重启服务。systemd 用户可在环境变量中设置 `OSTENSORY_UPDATE_RESTART_CMD` 并用 CLI `--restart`；后台页面不会未经确认杀掉当前进程。
- Docker 请在宿主机拉取新镜像并重建容器；容器内更新不会持久保存，默认拒绝。
- 依赖文件变化时会尝试 `pip install -r requirements.txt`。如由外部管理依赖，可设置 `OSTENSORY_UPDATE_SKIP_PIP=1` 并在重启前手动安装。生产环境要保证服务账户对虚拟环境有写权限。
- 回滚：停止服务，把备份中的 `code-before/` 覆盖回程序目录，再用备份的 `ostensory.sqlite3` 替换 `data/` 中的数据库；操作前先另做一份完整 `data/` 备份。

服务端 Release 由 `.github/workflows/release.yml` 生成：发布前先修改 `app/__init__.py` 和 `pyproject.toml` 的版本号，再推送对应 `v*` 标签；工作流会校验标签与 `app/__init__.py` 的 `__version__` 完全一致，打包 `ostensory-server.tar.gz` 和 `SHA256SUMS.txt`，并创建 GitHub Release。标签去掉 `v` 后必须等于版本号；含 `dev`/`alpha`/`beta`/`rc`/`pre` 的版本会自动标记为预发布。稳定版更新器只接受正式 Release，开发版才会接受预发布版本。

## 数据迁移

后台 **数据迁移** 页提供导出和导入，用于整站搬家。

导出包是 `.tar.gz`，包含数据库一致性快照、`master.key`、`secret.key`、全部本机图片、站点图标、GeoIP、账户/设置/API Token/审计日志。`tmp/`、`cache/`、`backups/`、`update.lock` 和 SQLite WAL 不打包。远程存储的图片会先下载并转存进包内，数据库引用改写为本地路径；任何一张远程图片下载失败都会中止导出，避免生成缺图包。

导出完成后会回到迁移页显示下载链接，并自动开始下载；下载结束才删除临时包，如果浏览器中断下载可以回页面重新下载。这样不会出现“文件已经下载完，但原来的处理页面一直转圈”的情况。

下载后临时迁移包会保留，支持重复下载和断点续传；确认文件完整后可在迁移页点“删除临时迁移包”，再次打包也会清理旧包，避免大文件在服务器上长期占用空间。

应用层默认不限制迁移包大小、解压后大小、文件数量或清单大小；真正上限是服务器磁盘剩余空间、文件系统单文件限制和反向代理请求体限制。需要主动限制时设置 `OSTENSORY_MIGRATION_MAX_MB`、`OSTENSORY_MIGRATION_MAX_EXTRACTED_MB`、`OSTENSORY_MIGRATION_MAX_FILES` 或 `OSTENSORY_MIGRATION_MAX_MANIFEST_MB`，填 `0` 或留空就是不限制。导出和导入前都会检查磁盘空间；导入需要同时容纳“上传包 + 解压包 + 当前数据备份”。

导入前会在临时目录完成全部校验：清单格式、文件集合、逐文件大小和 SHA256、数据库完整性与外键、每个图片引用、后台路径、主密钥一致性、解压大小和符号链接/路径穿越。校验全部通过后才进入维护模式，先复制一份完整当前数据，再替换。替换失败会从备份恢复；进程中断则下次启动在读取密钥/数据库前自动恢复。若自动回滚本身失败，进程会停止对外提供数据（所有请求包括健康检查都返回 503），需保留备份和标记后重启。恢复标记和备份默认放在数据目录的上级目录；设置 `OSTENSORY_MIGRATION_STATE_DIR` 后优先放在该目录，Docker 必须通过 `OSTENSORY_MIGRATION_STATE_DIR=/app/migration-backups` 指向持久卷。

FastAPI 默认把大上传落到系统临时目录；Docker 示例已把 `TMPDIR` 指向迁移持久卷，手工部署若 `/tmp` 较小请自行设置 `TMPDIR`。无论包多大，真正替换数据时才会短暂进入维护模式；上传、解压和校验期间站点仍可用。

迁移包包含主密钥、密码哈希和 API Token 哈希，必须按最高机密保管，传输完立即删除。它不是日常备份的替代品。

## 部署

- Docker：`docker compose -f deploy/docker-compose.yml up -d --build`
- systemd：见 `deploy/README.md`
- nginx 示例：`deploy/nginx.conf`
- 备份：`python deploy/backup.py --data-dir data --output backups`

详见 [deploy/README.md](deploy/README.md)。

## 目录

```text
app/
├─ main.py              FastAPI 入口
├─ routes_public.py     首页、图片页、相册、上传
├─ routes_admin.py      后台
├─ routes_api.py        客户端/API
├─ services.py          业务逻辑
├─ storage.py           本机与远程存储调度
├─ storage_backends.py  WebDAV / S3 / Worker 适配
├─ updates.py           版本检查、校验、备份与安全更新
├─ migration.py         整站导出/导入、校验、维护模式和回滚
├─ migration_recovery.py 中断导入的启动恢复
├─ schema.sql           SQLite 表结构
├─ templates/           Jinja2 页面
└─ static/              CSS / JS
deploy/                 部署、备份、nginx、systemd
run.py                  启动脚本
```

## 安全边界

- 后台使用 Cookie 会话和 CSRF；API 使用可撤销的 Bearer Token。
- 图片加密使用 `data/master.key`；丢失主密钥后加密图片无法恢复。
- 后台登录有失败冷却；生产环境建议在反向代理再加一层限流。
- 游客上传和管理员上传分开计数，管理员上传不占游客配额。即使关闭“用户上传”和“游客上传”，管理员仍可从导航进入自己的上传页，避免管理员把自己锁在上传功能之外；普通用户和匿名访客仍按开关返回 403/404。
- 管理员上传仍受服务器磁盘容量、单文件大小和（如已设置）管理员个人配额约束。
- 不要把 `data/`、`.env`、`master.key`、`secret.key` 提交到仓库。

## 相关仓库

- [Ostensory-client](https://github.com/EOR404/Ostensory-client)：Windows 同步客户端
- [Ostensory-worker](https://github.com/EOR404/Ostensory-worker)：Cloudflare Worker 图床

## License

MIT