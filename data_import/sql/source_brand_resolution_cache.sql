CREATE TABLE IF NOT EXISTS ai_bot_source_brand_resolution_cache (
    normalized_input VARCHAR(255) NOT NULL,
    source_name VARCHAR(32) NOT NULL,
    user_brand VARCHAR(255) NOT NULL,
    source_brand VARCHAR(255) NOT NULL,
    match_method VARCHAR(64) NOT NULL,
    confidence DECIMAL(5,4) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (normalized_input, source_name),
    INDEX idx_source_brand_value (source_name, source_brand)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
