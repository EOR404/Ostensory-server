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

## systemd

```bash
sudo useradd --system --home /opt/ostensory --shell /usr/sbin/nologin ostensory
sudo mkdir -p /opt/ostensory
# 把仓库放到 /opt/ostensory，然后：
cd /opt/ostensory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo mkdir -p data
sudo chown -R ostensory:ostensory data
sudo cp deploy/ostensory.service /etc/systemd/system/
sudo cp deploy/ostensory.env.example /etc/ostensory.env
sudo chmod 600 /etc/ostensory.env
sudo systemctl daemon-reload
sudo systemctl enable --now ostensory
```

`/etc/ostensory.env` 至少确认：

```text
OSTENSORY_DATA_DIR=/opt/ostensory/data
OSTENSORY_TRUSTED_PROXIES=127.0.0.1/32,::1/128
```

## nginx

替换 `deploy/nginx.conf` 里的域名和证书路径，然后：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

上传大小在 nginx 的 `client_max_body_size` 和后台“最大上传 MB”两处都要够大。

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

## 升级

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

## 反向代理检查清单

- 只信任自己的代理 IP/CIDR。
- 保留 `X-Forwarded-For`、`X-Forwarded-Proto`、`Host`。
- 静态资源使用同域 `/static/...` 相对路径；如果 HTTPS 下 CSS/JS 全部失效，先看浏览器控制台是否把资源报成 `http://...` 混合内容，并检查代理是否改写了页面内容。
- 启用 HTTPS，后台 Cookie 才会带 Secure。
- 不要在生产开启 `OSTENSORY_ALLOW_TEST_GEO_HEADER`。
- 只跑一个 Uvicorn worker；需要横向扩容时先换 PostgreSQL/对象存储。