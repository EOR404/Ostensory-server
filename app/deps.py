from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Depends, HTTPException, Request

from .access import parse_countries
from .config import Config
from .database import connect
from .geo import GeoResolver
from .security import authenticate_api_token, current_admin
from .storage import Storage
from .web import admin_url


class RedirectToLogin(Exception):
    def __init__(self, url: str):
        self.url = url


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_geo(request: Request) -> GeoResolver:
    return request.app.state.geo


def get_db_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.config.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def optional_admin(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
) -> sqlite3.Row | None:
    return current_admin(db, request)


def require_admin(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
) -> sqlite3.Row:
    admin = current_admin(db, request)
    if not admin:
        raise RedirectToLogin(admin_url(request, "/login"))
    return admin


def require_api_user(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
) -> sqlite3.Row:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    user = authenticate_api_token(db, token)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="需要有效的 API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_csrf(request: Request, token: str | None, config: Config) -> None:
    from .security import check_csrf

    if not check_csrf(request, token, config):
        raise HTTPException(status_code=400, detail="CSRF 校验失败，请刷新页面后重试")


def country_list(raw: str | None) -> list[str]:
    return parse_countries(raw)