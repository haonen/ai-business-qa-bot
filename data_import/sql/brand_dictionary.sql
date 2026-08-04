CREATE TABLE IF NOT EXISTS ai_bot_brand_dictionary (
    source_name VARCHAR(32) NOT NULL,
    source_brand VARCHAR(255) NOT NULL,
    normalized_brand VARCHAR(255) NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_name, source_brand),
    INDEX idx_brand_dictionary_lookup (source_name, normalized_brand)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
