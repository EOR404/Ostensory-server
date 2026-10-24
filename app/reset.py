from __future__ import annotations

import secrets
import shutil
import threading
from pathlib import Path

from .config import Config
from .database import init_db
from .geo import GeoResolver
from .storage import Storage

_reset_lock = threading.Lock()


def _write_key(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def reset_application_data(app, config: Config) -> None:
    """Return the application to a fresh-install state.

    The caller must already have authenticated an administrator, checked CSRF
    and verified the experimental feature flag plus all confirmations.  This
    function is intentionally not exposed as a general utility.
    """

    with _reset_lock:
        root = config.data_dir.resolve()
        if root == root.parent or not root.name:
            raise RuntimeError("拒绝重置不安全的数据目录")
        if not config.db_path.resolve().is_relative_to(root):
            raise RuntimeError("数据库不在数据目录内，拒绝重置")
        # Never let a misconfigured OSTENSORY_DATA_DIR erase application code.
        server_root = config.server_dir.resolve()
        templates = config.templates_dir.resolve()
        static = config.static_dir.resolve()
        for protected in (server_root, templates, static):
            if protected == root or protected.is_relative_to(root):
                raise RuntimeError("数据目录包含应用代码，拒绝重置")

        # MMDB readers keep the file open on Windows, so release it before
        # deleting the directory.
        geo = getattr(app.state, "geo", None)
        if geo is not None:
            geo.close()

        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass

        # A fresh install gets new keys; deleting old keys is part of the reset.
        secret_key = secrets.token_bytes(32)
        master_key = secrets.token_bytes(32)
        _write_key(root / "secret.key", secret_key)
        _write_key(root / "master.key", master_key)
        config.secret_key = secret_key
        config.master_key = master_key

        init_db(config)
        app.state.admin_path = "admin"
        app.state.storage = Storage(config)
        app.state.geo = GeoResolver(config)