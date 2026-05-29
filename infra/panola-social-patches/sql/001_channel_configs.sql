-- panola-social Faz 2 multi-channel schema
-- 2026-05-29
-- DB: /opt/panola-social/data/social.db (SQLite)

-- Kanal konfigurasyon: hangi urun hangi kanaldan yayinlasin
CREATE TABLE IF NOT EXISTS channel_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,                          -- 'kuafor','petvet','panola_erp'
    channel TEXT NOT NULL,                          -- 'telegram','whatsapp','gbp','linkedin','tiktok','instagram'
    enabled INTEGER NOT NULL DEFAULT 0,             -- 0=disabled, 1=enabled
    config_json TEXT NOT NULL DEFAULT '{}',         -- channel-specific (chat_id, page_id, vb.)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product, channel)
);

CREATE INDEX IF NOT EXISTS idx_channel_configs_product_enabled
    ON channel_configs(product, enabled);

-- Per-post yayin kayitlari (instagram mevcut benzeri yapida)
CREATE TABLE IF NOT EXISTS channel_publishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER,                                -- engine post id (varsa FK posts.id)
    product TEXT NOT NULL,
    channel TEXT NOT NULL,
    success INTEGER NOT NULL,
    external_id TEXT,                               -- platform message_id/post_id
    external_url TEXT,
    error TEXT,
    raw_response TEXT,                              -- JSON raw response
    posted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_channel_publishes_post
    ON channel_publishes(post_id);
CREATE INDEX IF NOT EXISTS idx_channel_publishes_product_channel_date
    ON channel_publishes(product, channel, posted_at);

-- WhatsApp contact opt-in (alt yapi, kullanim icin _IMPLEMENTED=True bekliyor)
CREATE TABLE IF NOT EXISTS whatsapp_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    phone TEXT NOT NULL,                            -- E.164 +905551112233
    opt_in INTEGER NOT NULL DEFAULT 1,
    opt_out_at TEXT,
    notes TEXT,                                     -- opt-in kaynak (form, sms_replied, vb.)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product, phone)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_product_optin
    ON whatsapp_contacts(product, opt_in);

-- Seed: kuafor + petvet icin Telegram aktif (bot setup sonrasi chat_id ekle)
-- INSERT OR IGNORE syntax: var olan kayitlari korur
INSERT OR IGNORE INTO channel_configs(product, channel, enabled, config_json)
VALUES
    ('kuafor', 'telegram', 0, '{"chat_id": ""}'),  -- chat_id eklenince enabled=1 yap
    ('petvet', 'telegram', 0, '{"chat_id": ""}'),
    ('panola_erp', 'telegram', 0, '{"chat_id": ""}'),
    ('kuafor', 'whatsapp', 0, '{}'),               -- whatsapp DORMANT
    ('petvet', 'whatsapp', 0, '{}');
