# 部署说明

这个目录随 **Ostensory-server** 仓库发布。以下命令默认仓库根目录就是 `/opt/ostensory`。

## Docker（推荐给 Linux 服务器）

```bash
mkdir -p data
sudo chown -R 10001:10001 data
docker compose -f deploy/docker-compose.yml up -d --build
```

- 容器只映射到宿主机 `127.0.0.1:8000`，请用 nginx/Caddy 对外提供 HTTPS。
- 数据在仓库根 `data/`，容器内是 `/data`。
- 如果 nginx 在宿主机，容器看到的客户端是 Docker 网桥网关（常见 `172.17.0.1`），compose 已默认信任该地址；请用 `docker network inspect bridge` 确认，必要时改 `OSTENSORY_TRUSTED_PROXIES`。
- 如果使用独立的 nginx 容器，需要把 `OSTENSORY_TRUSTED_PROXIES` 改为 nginx 容器的网段（例如 `172.18.0.0/16`），不能只信 loopback。
- 如果所有访客都显示同一个国家，先检查可信代理设置；这是地区限制最常见的部署错误。
- 镜像以 UID 10001 运行；Linux 下要确保 `data/` 属于该 UID。Windows Docker Desktop 通常不需要手动 chown。
- 迁移前备份放在 `ostensory-migration` 命名卷（容器内 `/app/migration-backups`）。`/data` 的上级是只读根目录，不能把备份放在 `/data` 旁边；重建容器时也不要删除这个卷，否则中断导入的自动恢复和手工回滚会失去依据。
- 镜像把 `TMPDIR` 设为 `/app/migration-backups/tmp`，这样几十 GB 的导入包不会先写满容器临时层；该目录位于同一个持久卷内。

## systemd

```bash
sudo useradd --system --home /opt/ostensory --shell /usr/sbin/nologin ostensory
sudo mkdir -p /opt/ostensory
# 把仓库放到 /opt/ostensory，然后：
cd /opt/ostensory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo mkdir -p data migration-backups
sudo chown -R ostensory:ostensory data migration-backups
sudo cp deploy/ostensory.service /etc/systemd/system/
sudo cp deploy/ostensory.env.example /etc/ostensory.env
sudo chmod 600 /etc/ostensory.env
sudo systemctl daemon-reload
sudo systemctl enable --now ostensory
```

`/etc/ostensory.env` 至少确认：

```text
OSTENSORY_DATA_DIR=/opt/ostensory/data
OSTENSORY_MIGRATION_STATE_DIR=/opt/ostensory/migration-backups
OSTENSORY_TRUSTED_PROXIES=127.0.0.1/32,::1/128
```

`migration-backups/` 必须属于运行服务的账户；否则后台导入会在“创建导入前备份”阶段安全中止，不会改数据。

## nginx

替换 `deploy/nginx.conf` 里的域名和证书路径，然后：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

普通图片上传由后台“最大上传 MB”控制；迁移包默认不设应用层大小上限，实际能搬多少取决于磁盘剩余空间、文件系统单文件限制和反向代理配置。示例 nginx 使用 `client_max_body_size 0`（不限制请求体），并关闭 `proxy_request_buffering` 和 `proxy_buffering`，避免 nginx 为迁移包在临时目录再缓存一整份；上传和下载都会直接流给应用/浏览器。迁移接口仍要求管理员 Cookie 和 CSRF，不要把这个入口暴露给不受信任的网络。

静态资源使用同域 `/static/...` 相对路径。如果 HTTPS 下 CSS/JS 全部失效，先看浏览器控制台是否把资源报成 `http://...` 混合内容，并检查代理有没有改写页面内容。

## 存储方式

后台 **存储** 页支持：

- 本机磁盘
- WebDAV
- S3 兼容对象存储（Cloudflare R2、阿里云 OSS、AWS S3、MinIO 等）
- Ostensory Worker（Cloudflare Workers + R2 + D1）

每种方式都可以设置容量上限（MB，0 表示不限）、使用率阈值和顺序。新上传按顺序选择第一个未满、未到阈值的存储；某个后端写入失败时自动尝试下一个。已有图片不会自动迁移。

远程存储的密码、Secret Key 和 Token 会用 `data/master.key` 加密后保存在 `data/ostensory.sqlite3` 的 `storage_backends` 表里；数据库和主密钥仍需收紧权限。**替换 `master.key` 会导致这些凭据无法解密**，需要在后台重新填写。

Worker 的部署见 [Ostensory-worker 仓库](https://github.com/EOR404/Ostensory-worker)。

## GeoIP

1. 合法取得 MaxMind GeoLite2/GeoIP2 的 `Country.mmdb`。
2. 放到 `data/geoip/Country.mmdb`。
3. 设置 `OSTENSORY_GEOIP_DB` 指向它并重启。
4. 后台“GeoIP”页应显示数据库可用；测试 IP 能返回国家。

没有数据库时，受限内容默认按“失败关闭”拒绝。也可以不用 MMDB，只用 `data/geoip/overrides.json` 固定 CIDR。

## 备份与恢复

```bash
python deploy/backup.py --data-dir data --output backups
```

备份是 `tar.gz`，里面有 SQLite 一致性快照、`master.key`、`secret.key` 和本机图片。恢复时解压到目标数据目录，保持文件权限，再启动服务；**主密钥丢失后加密图片无法恢复**。

远程存储（WebDAV/S3/Worker）里的原图不在这份备份里，需要按对应云厂商的方式另外备份；数据库里只保存对象路径和元数据。

### 后台数据迁移

后台左侧 **数据迁移** 页可导出整站迁移包，或在目标服务器导入。导出包含数据库快照、密钥、账户、设置和全部图片；远程图片会下载后转存进包内。导入会先校验清单、SHA256、数据库和全部图片引用，全部通过后才替换数据，并在替换前生成完整备份。

- 迁移包是最高机密：包含 `master.key`、密码哈希和 API Token 哈希。用 HTTPS 传输，迁移后立即删除。
- 备份/恢复标记默认在数据目录上级；设置 `OSTENSORY_MIGRATION_STATE_DIR` 后优先放在该目录。Docker 用 `OSTENSORY_MIGRATION_STATE_DIR=/app/migration-backups`，并挂载持久卷。否则 `/data` 的上级不可写，导入会在备份阶段安全中止。
- 导入过程中若进程中断，下次启动会先自动恢复备份，再读取数据库和密钥；恢复失败会拒绝启动并提示备份路径。
- 迁移只搬数据，不搬程序代码、域名、nginx、证书或 Docker 挂载。目标服务器代码版本不能低于导出方。
- nginx 要放行大请求体：示例用 `client_max_body_size 0`（不设固定上限）、`client_body_timeout 7200s`、`proxy_read_timeout 7200s`。默认的 1m/220m 会在大迁移包上直接返回 413/504；如果反向代理或云防火墙另有请求体上限，需要同步调大。
- 如果目标环境设置了 `OSTENSORY_MASTER_KEY`，必须与包内主密钥一致；否则拒绝导入，避免出现无法解密的图片。

## 升级

### 后台或命令行更新（推荐）

后台左侧最下方 **关于** 页可以检查版本并更新；也可以在服务器上运行：

```bash
# 先停止服务，避免运行中的进程读到一半旧、一半新代码
sudo systemctl stop ostensory

# 检查并安装最新 Release；事先会自动备份程序和数据库
cd /opt/ostensory
.venv/bin/python -m app.cli update --check
.venv/bin/python -m app.cli update --yes

# 如果依赖文件有变化，确认 pip 安装成功
.venv/bin/pip install -r requirements.txt

sudo systemctl start ostensory
```

更新器只替换程序文件，不覆盖 `data/`；版本比较后直接选最新 Release，不会逐版本升级。备份位于 `data/backups/update-.../`，包含旧程序、SQLite 一致性快照和密钥。

生产环境建议先做一次完整备份：

```bash
python deploy/backup.py --data-dir data --output backups
```

依赖由外部镜像或只读虚拟环境管理时，设置 `OSTENSORY_UPDATE_SKIP_PIP=1`，并在重启前自行安装新依赖。

### Docker

容器内的代码更新不会写入镜像，重建容器后就会丢失。请在宿主机更新源码或镜像后重建：

```bash
docker compose -f deploy/docker-compose.yml build --pull
docker compose -f deploy/docker-compose.yml up -d
```

### 传统 git 方式

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ostensory
```

升级后可在后台逐张“重新解析”或运行：

```bash
.venv/bin/python -m app.cli reparse
```

### 自更新注意事项

- 后台“关于”页的自动检查默认开启，自动安装默认关闭。
- 更新后必须重启服务；页面不会自动杀进程。
- 更新前后台路径、账户、数据库、图片和设置都不会重置。
- 校验失败、版本不匹配、压缩包路径异常时会停止，不会替换程序。
- 回滚时先停止服务，把备份的 `code-before/` 覆盖回程序目录，再用备份的 `ostensory.sqlite3` 替换数据库。
- 可设置 `OSTENSORY_UPDATE_REPO`（默认 `EOR404/Ostensory-server`）、`OSTENSORY_UPDATE_TOKEN`（私有仓库或提高 GitHub API 频率）和 `OSTENSORY_UPDATE_RESTART_CMD`（CLI `--restart` 使用）。

## 反向代理检查清单

- 只信任自己的代理 IP/CIDR。
- 保留 `X-Forwarded-For`、`X-Forwarded-Proto`、`Host`。
- 静态资源使用同域 `/static/...` 相对路径；如果 HTTPS 下 CSS/JS 全部失效，先看浏览器控制台是否把资源报成 `http://...` 混合内容，并检查代理是否改写了页面内容。
- 启用 HTTPS，后台 Cookie 才会带 Secure。
- 不要在生产开启 `OSTENSORY_ALLOW_TEST_GEO_HEADER`。
- 只跑一个 Uvicorn worker；需要横向扩容时先换 PostgreSQL/对象存储。