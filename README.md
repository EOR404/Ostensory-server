# Ostensory 服务端

Ostensory 是一个面向 ComfyUI / AI 绘图工作流的自建图床。服务端负责网页、后台、图片处理、元数据解析和存储调度。

当前版本：**0.0.1dev**。

主要能力：

- 游客上传、管理员上传、相册、标签、NSFW/地区限制
- 从 PNG/JPEG/WebP/GIF 中解析 ComfyUI / A1111 参数
- 图片加密、回收站、去重、缩略图和预览图
- 多存储后端：本机、WebDAV、S3 兼容对象存储、Cloudflare Worker
- 按容量上限、使用率阈值和优先级自动切换存储
- 中英文界面、RSS、公开 API、Windows 同步客户端

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

- 删除图片默认只做软删除，原图、缩略图仍占用容量。
- 回收站里可以逐张或批量永久删除，也可以清空。
- “自动永久删除”按天设置，每张图片从自己的 `deleted_at` 单独计时；`0` 表示永不删除（默认）。
- 应用启动时、每小时、访问回收站/上传/容量检查时会清理已过期图片。
- 永久删除最后一张引用同一原图的记录后，才会删除文件并释放容量。

> 自动删除不可恢复。生产环境建议先备份 `data/`，再开启较短保留天数。

远程对象使用 `remote/<后端ID>/...` 作为键，本机旧图片的键不变。远程图片首次读取会缓存到 `data/cache/`，删除图片时会同时删除远程对象和缓存。

> WebDAV 密码、S3 Secret Key、Worker Token 会使用 `data/master.key` 以 AES-GCM 加密，再保存到 `data/ostensory.sqlite3` 的 `storage_backends` 表。数据库仍需限制权限；如果替换或丢失 `master.key`，这些凭据将无法解密，需要重新填写。

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
python -m app.cli geoip 8.8.8.8
```

忘记后台用户名或密码时运行 `python -m app.cli reset-admin`，按提示输入新用户名（回车保留当前）和新密码；命令会注销旧会话、撤销 API Token 并清除登录冷却。

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
- 游客上传和管理员上传分开计数，管理员上传不占游客配额。
- 不要把 `data/`、`.env`、`master.key`、`secret.key` 提交到仓库。

## 相关仓库

- [Ostensory-client](https://github.com/EOR404/Ostensory-client)：Windows 同步客户端
- [Ostensory-worker](https://github.com/EOR404/Ostensory-worker)：Cloudflare Worker 图床

## License

MIT