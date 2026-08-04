CREATE TABLE IF NOT EXISTS ai_bot_brand_resolution_cache (
    normalized_input VARCHAR(255) NOT NULL,
    user_brand VARCHAR(255) NOT NULL,
    search_brand VARCHAR(255) NOT NULL,
    topline_brand VARCHAR(255) NOT NULL,
    ksi_brand VARCHAR(255) NOT NULL,
    tmall_brand VARCHAR(255) NOT NULL,
    dy_brand VARCHAR(255) NULL,
    confidence DECIMAL(5,4) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (normalized_input)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
