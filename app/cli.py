from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from .config import Config
from .database import connect, init_db, utc_now
from .geo import GeoResolver
from .security import create_api_token, hash_password, password_warning
from .services import reparse_asset
from .storage import Storage

ADMIN_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
RESERVED_ADMIN_PATHS = {
    "api",
    "media",
    "static",
    "images",
    "albums",
    "download",
    "unlock",
    "search",
    "browse",
    "favicon.ico",
    "healthz",
    "upload",
    "stats",
    "admin",
}


def _config(args) -> Config:
    if args.data_dir:
        os.environ["OSTENSORY_DATA_DIR"] = str(Path(args.data_dir).resolve())
    return Config.from_env(server_dir=Path(args.server_dir) if args.server_dir else None)


def _db(config: Config) -> sqlite3.Connection:
    init_db(config)
    return connect(config.db_path)


def cmd_init(config: Config, args) -> int:
    db = _db(config)
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'admin_path'").fetchone()
        admin_path = row["value"] if row else "admin"
        users = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        db.close()
    print(f"数据目录：{config.data_dir}")
    print(f"数据库：{config.db_path}")
    print(f"后台地址：/{admin_path}")
    print(f"管理员数量：{users}")
    if not users:
        print("尚未初始化管理员，请启动服务后访问后台创建。")
    return 0


def cmd_show_admin_path(config: Config, args) -> int:
    db = _db(config)
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'admin_path'").fetchone()
        print(row["value"] if row else "admin")
    finally:
        db.close()
    return 0


def cmd_set_admin_path(config: Config, args) -> int:
    value = args.path.strip().strip("/")
    reserved = value.lower() in RESERVED_ADMIN_PATHS and value.lower() != "admin"
    if not ADMIN_PATH_RE.match(value) or reserved:
        print("路径需为 3-32 位字母、数字、下划线或连字符，且不能是系统保留名称。", file=sys.stderr)
        return 2
    db = _db(config)
    try:
        db.execute(
            "INSERT INTO settings(key, value) VALUES('admin_path', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        db.commit()
    finally:
        db.close()
    print(f"后台路径已改为 /{value}，重启服务后生效。")
    return 0


def cmd_reset_password(config: Config, args) -> int:
    db = _db(config)
    try:
        user = db.execute("SELECT * FROM users WHERE username = ?", (args.username,)).fetchone()
        if not user:
            print("用户不存在。", file=sys.stderr)
            return 1
        password = args.password
        if not password:
            password = getpass.getpass("新密码：")
            again = getpass.getpass("再次输入：")
            if password != again:
                print("两次输入不一致。", file=sys.stderr)
                return 2
        if not password:
            print("密码不能为空。", file=sys.stderr)
            return 2
        warning = password_warning(password)
        if warning:
            print(warning, file=sys.stderr)
        db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(password), utc_now(), user["id"]),
        )
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        db.execute(
            "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (utc_now(), user["id"]),
        )
        db.execute("DELETE FROM login_throttle")
        db.commit()
    finally:
        db.close()
    print(f"已重置 {args.username} 的密码，并注销所有网页会话、API Token 和登录冷却。")
    return 0


def cmd_reset_admin(config: Config, args) -> int:
    """Reset the administrator username and password from the server console."""
    db = _db(config)
    try:
        if args.user:
            user = db.execute(
                "SELECT * FROM users WHERE username = ?", (args.user.strip(),)
            ).fetchone()
        else:
            user = db.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
        if not user:
            print("没有找到管理员，请先启动服务并完成初始化。", file=sys.stderr)
            return 1
        current_username = str(user["username"])
        new_username = (args.new_username or "").strip()
        if not new_username:
            try:
                new_username = input(f"新用户名 [{current_username}]：").strip() or current_username
            except (EOFError, KeyboardInterrupt):
                print("已取消。", file=sys.stderr)
                return 2
        if not USERNAME_RE.fullmatch(new_username):
            print("用户名需为 3-64 位字母、数字或 _ . @ - 字符。", file=sys.stderr)
            return 2
        if new_username != current_username:
            exists = db.execute(
                "SELECT 1 FROM users WHERE username = ? AND id <> ?",
                (new_username, user["id"]),
            ).fetchone()
            if exists:
                print("新用户名已被其他用户占用。", file=sys.stderr)
                return 2
        password = args.password or ""
        if not password:
            try:
                password = getpass.getpass("新密码：")
                again = getpass.getpass("再次输入：")
            except (EOFError, KeyboardInterrupt):
                print("已取消。", file=sys.stderr)
                return 2
            if password != again:
                print("两次输入不一致。", file=sys.stderr)
                return 2
        if not password:
            print("密码不能为空。", file=sys.stderr)
            return 2
        warning = password_warning(password)
        if warning:
            print(warning, file=sys.stderr)
        try:
            db.execute(
                "UPDATE users SET username = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                (new_username, hash_password(password), utc_now(), user["id"]),
            )
        except sqlite3.IntegrityError:
            print("新用户名已被其他用户占用。", file=sys.stderr)
            return 2
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        db.execute(
            "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (utc_now(), user["id"]),
        )
        db.execute("DELETE FROM login_throttle")
        db.commit()
    finally:
        db.close()
    if new_username == current_username:
        print(f"已重置管理员 {new_username} 的用户名和密码，并注销所有网页会话、API Token 和登录冷却。")
    else:
        print(
            f"已重置管理员：{current_username} -> {new_username}，"
            "并注销所有网页会话、API Token 和登录冷却。"
        )
    return 0


def cmd_create_token(config: Config, args) -> int:
    db = _db(config)
    try:
        user = db.execute("SELECT * FROM users WHERE username = ?", (args.username,)).fetchone()
        if not user:
            print("用户不存在。", file=sys.stderr)
            return 1
        token = create_api_token(db, user["id"], args.name)
        db.commit()
    finally:
        db.close()
    print(token)
    print("请立即保存，Token 只显示这一次。", file=sys.stderr)
    return 0


def cmd_reparse(config: Config, args) -> int:
    storage = Storage(config)
    db = _db(config)
    try:
        sql = (
            "SELECT a.*, b.storage_key, b.width, b.height, b.encrypted "
            "FROM assets a JOIN blobs b ON b.id = a.blob_id WHERE a.deleted_at IS NULL"
        )
        params: list = []
        if args.asset_id:
            sql += " AND a.id = ?"
            params.append(args.asset_id)
        rows = db.execute(sql, params).fetchall()
        if not rows:
            print("没有匹配的图片。")
            return 1
        ok = 0
        failed = 0
        for row in rows:
            try:
                reparse_asset(db, storage, row)
                ok += 1
                print(f"#{row['id']} 已解析")
            except Exception as exc:
                failed += 1
                print(f"#{row['id']} 解析失败：{exc}", file=sys.stderr)
        db.commit()
        print(f"完成：成功 {ok}，失败 {failed}")
        return 0 if failed == 0 else 2
    finally:
        db.close()


def cmd_geoip(config: Config, args) -> int:
    resolver = GeoResolver(config)
    try:
        country, source = resolver.lookup(args.ip)
        status = resolver.status()
        print(
            json.dumps(
                {
                    "ip": args.ip,
                    "country": country,
                    "source": source,
                    "available": status.available,
                    "database": status.database,
                    "overrides": status.overrides,
                    "error": status.error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        resolver.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ostensory", description="Ostensory 服务端管理命令")
    parser.add_argument("--data-dir", help="覆盖数据目录")
    parser.add_argument("--server-dir", help="覆盖 server 目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库并显示信息")
    sub.add_parser("show-admin-path", help="显示后台路径")
    p = sub.add_parser("set-admin-path", help="修改后台路径")
    p.add_argument("path")
    p = sub.add_parser("reset-password", help="重置管理员密码并撤销会话和 Token")
    p.add_argument("username", nargs="?", default="admin")
    p.add_argument("--password", help="非交互式提供新密码（不建议写在命令历史里）")
    p = sub.add_parser("reset-admin", help="重置管理员用户名和密码（适合忘记登录信息时使用）")
    p.add_argument("--user", help="要重置的现有用户名；默认第一个管理员")
    p.add_argument("--new-username", help="新用户名；不填则交互询问，直接回车保留当前用户名")
    p.add_argument("--password", help="非交互式提供新密码（不建议写在命令历史里）")
    p = sub.add_parser("create-token", help="创建 API Token 并打印一次")
    p.add_argument("name", nargs="?", default="CLI 创建的 Token")
    p.add_argument("--username", default="admin")
    p = sub.add_parser("reparse", help="重新解析全部或单张图片的元数据")
    p.add_argument("--asset-id", type=int)
    p = sub.add_parser("geoip", help="查询 IP 的国家")
    p.add_argument("ip")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config(args)
    handlers = {
        "init": cmd_init,
        "show-admin-path": cmd_show_admin_path,
        "set-admin-path": cmd_set_admin_path,
        "reset-password": cmd_reset_password,
        "reset-admin": cmd_reset_admin,
        "create-token": cmd_create_token,
        "reparse": cmd_reparse,
        "geoip": cmd_geoip,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    raise SystemExit(main())