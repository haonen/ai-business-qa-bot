CREATE TABLE IF NOT EXISTS ai_bot_tmall_brand_index (
    normalized_alias VARCHAR(191) NOT NULL,
    source_brand VARCHAR(255) NOT NULL,
    alias_value VARCHAR(255) NOT NULL,
    alias_source VARCHAR(32) NOT NULL,
    observation_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (normalized_alias, source_brand, alias_source),
    INDEX idx_tmall_brand_index_source (source_brand)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
