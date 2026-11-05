
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tokens (
    uid TEXT PRIMARY KEY,               -- "<chain>:<normalized address>"
    chain TEXT NOT NULL,
    address TEXT NOT NULL,              -- нормализованный (EVM: lower; Solana: как есть)
    address_display TEXT,               -- исходное написание (checksum)
    symbol TEXT, name TEXT, decimals INTEGER,
    first_seen_at TEXT NOT NULL,        -- первое обнаружение НАШЕЙ системой
    discovery_source TEXT, discovery_reason TEXT,
    is_quote_asset INTEGER DEFAULT 0,
    asset_class TEXT,                   -- token | quote | tokenized_stock | launchpad_meme ...
    coingecko_id TEXT,
    protocol_slug TEXT,                 -- подтверждённая связь с DefiLlama
    protocol_link_evidence TEXT,
    level TEXT DEFAULT 'A',             -- A..E
    status TEXT DEFAULT 'UNKNOWN',      -- PASS/FAIL/UNKNOWN/DATA ERROR/STALE DATA/MANUAL REVIEW
    status_reason TEXT,
    score_confirmed REAL, score_max_verifiable REAL, score_preliminary REAL,
    confidence TEXT,
    facts TEXT,                         -- JSON: последняя сводка фактов с источниками
    last_market_at TEXT, last_enriched_at TEXT, last_deep_at TEXT, last_onchain_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tokens_chain ON tokens(chain);
CREATE INDEX IF NOT EXISTS ix_tokens_symbol ON tokens(symbol);
CREATE INDEX IF NOT EXISTS ix_tokens_level ON tokens(level);

CREATE TABLE IF NOT EXISTS pools (
    uid TEXT PRIMARY KEY,               -- "<chain>:<pool address/id lower>"
    chain TEXT NOT NULL, address TEXT NOT NULL,
    dex TEXT, name TEXT,
    base_uid TEXT, quote_uid TEXT,
    pool_created_at TEXT,               -- дата создания ПУЛА (не токена!)
    first_seen_at TEXT, last_seen_at TEXT,
    sources TEXT                        -- json список источников
);
CREATE INDEX IF NOT EXISTS ix_pools_base ON pools(base_uid);
CREATE INDEX IF NOT EXISTS ix_pools_quote ON pools(quote_uid);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, url TEXT, status INTEGER, fetched_at TEXT, body BLOB
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,          -- token | pool | protocol | chain
    entity_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL, value_text TEXT, unit TEXT,
    source TEXT NOT NULL,
    observed_at TEXT,                   -- время, к которому относится значение
    fetched_at TEXT NOT NULL,           -- время получения
    confidence TEXT,                    -- confirmed | reported | derived | estimated
    method TEXT,
    verify_url TEXT,
    raw_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_obs_entity ON observations(entity_type, entity_id, metric, fetched_at);

CREATE TABLE IF NOT EXISTS filter_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT NOT NULL, filter_key TEXT NOT NULL,
    status TEXT NOT NULL, confidence TEXT,
    reason TEXT, evidence TEXT, params TEXT,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fr_token ON filter_results(token_uid, filter_key, evaluated_at);

CREATE TABLE IF NOT EXISTS filter_current (
    token_uid TEXT NOT NULL, filter_key TEXT NOT NULL,
    status TEXT NOT NULL, confidence TEXT, reason TEXT, evidence TEXT, evaluated_at TEXT,
    PRIMARY KEY(token_uid, filter_key)
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT, field TEXT, old_value TEXT, new_value TEXT, reason TEXT, at TEXT
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT, metric TEXT, values_json TEXT, detected_at TEXT, note TEXT, resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT, alert_type TEXT, severity TEXT, title TEXT,
    condition TEXT, values_json TEXT, sources_json TEXT, limitations TEXT,
    dedup_key TEXT UNIQUE, created_at TEXT, acknowledged INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watchlist (token_uid TEXT PRIMARY KEY, added_at TEXT, note TEXT);

CREATE TABLE IF NOT EXISTS manual_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT NOT NULL,
    fact_key TEXT NOT NULL,             -- vc_status | funding | tge_date | utility | official_link | team | catalyst | value_capture | audit
    value TEXT, category TEXT,
    evidence_url TEXT, source_type TEXT, -- official_docs | official_social | third_party | onchain | user
    note TEXT, entered_by TEXT, entered_at TEXT, active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_mf_token ON manual_facts(token_uid, fact_key);

CREATE TABLE IF NOT EXISTS tokenomics_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT NOT NULL, version_label TEXT, published_at TEXT,
    source_url TEXT, source_type TEXT, is_current INTEGER DEFAULT 0,
    params TEXT, note TEXT, entered_at TEXT, superseded_by INTEGER
);

CREATE TABLE IF NOT EXISTS supply_events (
    token_uid TEXT NOT NULL, kind TEXT NOT NULL,      -- mint | burn
    tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
    block INTEGER, ts TEXT, ts_source TEXT,            -- ts_source: block (точное время блока) | interpolated
    counterparty TEXT, amount_raw TEXT,                -- amount_raw: целые минимальные единицы (источник истины)
    amount REAL,                                       -- только для отображения; в расчётах не используется
    PRIMARY KEY(token_uid, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS ix_se_token ON supply_events(token_uid, block);

CREATE TABLE IF NOT EXISTS address_labels (
    chain TEXT, address TEXT, label TEXT, kind TEXT, source_url TEXT, source_type TEXT, token_uid TEXT,
    PRIMARY KEY(chain, address)
);

CREATE TABLE IF NOT EXISTS scan_progress (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT, source TEXT, run_at TEXT, pages INTEGER, pools INTEGER,
    window_start TEXT, window_end TEXT, continuous INTEGER, gap_start TEXT, gap_end TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY, last_success_at TEXT, last_error_at TEXT, last_error TEXT,
    calls_total INTEGER DEFAULT 0, errors_total INTEGER DEFAULT 0, rate_limited_total INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, started_at TEXT, finished_at TEXT,
    status TEXT, progress TEXT, stats TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS protocols (
    slug TEXT PRIMARY KEY, name TEXT, category TEXT, chains TEXT, address TEXT, gecko_id TEXT,
    symbol TEXT, url TEXT, listed_at TEXT, raw TEXT, fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, where_ TEXT, message TEXT
);
