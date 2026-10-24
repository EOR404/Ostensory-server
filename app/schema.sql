-- Ostensory SQLite schema
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO settings(key, value) VALUES
    ('site_name', 'Ostensory'),
    ('site_subtitle', 'ComfyUI 作品与提示词图床'),
    ('site_description', '私人 ComfyUI 图片与生成参数归档'),
    ('admin_path', 'admin'),
    ('public_base_url', ''),
    ('experimental_features', '0'),
    ('external_image_enabled', '0'),
    ('duplicate_check_completed', '0'),
    ('show_duplicate_images', '1'),
    ('duplicate_checked_at', ''),
    ('cursor_style', 'default'),
    ('cursor_custom_url', ''),
    ('cursor_hotspot_x', '4'),
    ('cursor_hotspot_y', '4'),
    ('blur_nsfw', '1'),
    ('nsfw_blocked_countries', 'CN'),
    ('geoip_fail_mode', 'block'),
    ('per_page', '48'),
    ('max_upload_mb', '200'),
    ('allowed_extensions', 'png,jpg,jpeg,webp,gif'),
    ('default_album_path', '未分类'),
    ('client_download_enabled', '1'),
    ('home_mode', 'waterfall'),
    ('home_layout', 'masonry'),
    ('home_waterfall_source', 'latest'),
    ('guest_upload_log_ip', '0'),
    ('default_language', 'auto'),
    ('default_theme', 'dark'),
    ('registration_enabled', '0'),
    ('preview_enabled', '1'),
    ('trash_retention_days', '-1'),
    ('user_quota_mode', 'fixed'),
    ('user_quota_default_mb', '1024'),
    ('user_quota_full_action', 'reject');

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    quota_mb INTEGER NOT NULL DEFAULT 0,
    oauth_provider TEXT NOT NULL DEFAULT '',
    oauth_subject TEXT NOT NULL DEFAULT '',
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS login_throttle (
    client_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at INTEGER NOT NULL,
    blocked_until INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS boxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    cover_asset_id INTEGER,
    access_password_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    cover_asset_id INTEGER,
    nsfw INTEGER NOT NULL DEFAULT 0,
    access_password_hash TEXT,
    region_mode TEXT NOT NULL DEFAULT 'inherit',
    region_countries TEXT NOT NULL DEFAULT '[]',
    guest_quota_mb INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_backends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    quota_mb INTEGER NOT NULL DEFAULT 0,
    threshold_pct INTEGER NOT NULL DEFAULT 90,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_storage_backends_order
    ON storage_backends(enabled, priority, id);

CREATE TABLE IF NOT EXISTS blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    thumb_key TEXT,
    preview_key TEXT,
    backend_id INTEGER NOT NULL DEFAULT 1,
    encrypted INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_id INTEGER NOT NULL REFERENCES blobs(id) ON DELETE RESTRICT,
    album_id INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL,
    original_name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    intro_font_size TEXT NOT NULL DEFAULT '15',
    intro_font_style TEXT NOT NULL DEFAULT 'italic',
    intro_quote INTEGER NOT NULL DEFAULT 1,
    intro_color TEXT NOT NULL DEFAULT '',
    intro_allow_html INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    positive_prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    lora_names TEXT NOT NULL DEFAULT '',
    nsfw INTEGER NOT NULL DEFAULT 0,
    access_password_hash TEXT,
    region_mode TEXT NOT NULL DEFAULT 'inherit',
    region_countries TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'web',
    upload_ip TEXT NOT NULL DEFAULT '',
    client_path TEXT,
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'approved',
    visibility TEXT NOT NULL DEFAULT 'public',
    views INTEGER NOT NULL DEFAULT 0,
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_assets_album ON assets(album_id, deleted_at, id DESC);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(deleted_at, id DESC);
CREATE INDEX IF NOT EXISTS idx_assets_client_path ON assets(album_id, client_path);
CREATE INDEX IF NOT EXISTS idx_assets_blob ON assets(blob_id);
CREATE INDEX IF NOT EXISTS idx_assets_nsfw ON assets(nsfw);


CREATE TABLE IF NOT EXISTS asset_likes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, actor_key)
);
CREATE INDEX IF NOT EXISTS idx_asset_likes_asset ON asset_likes(asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_likes_user ON asset_likes(user_id);

CREATE TABLE IF NOT EXISTS asset_view_seen (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    actor_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(asset_id, actor_key)
);
CREATE INDEX IF NOT EXISTS idx_blobs_sha ON blobs(sha256);
CREATE INDEX IF NOT EXISTS idx_albums_parent ON albums(parent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(id DESC);