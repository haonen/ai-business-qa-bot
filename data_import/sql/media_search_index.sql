CREATE TABLE IF NOT EXISTS ai_bot_media_search_index (
    record_key CHAR(64) NOT NULL,
    report_month DATE NOT NULL,
    report_year SMALLINT NOT NULL,
    report_month_num TINYINT UNSIGNED NOT NULL,
    previous_year SMALLINT NOT NULL,
    grain_level VARCHAR(32) NOT NULL,
    brand VARCHAR(255) NOT NULL,
    category VARCHAR(255) NULL,
    current_rank INT UNSIGNED NOT NULL,
    current_search_index BIGINT UNSIGNED NOT NULL,
    previous_search_index BIGINT UNSIGNED NOT NULL,
    source_yoy_rate DECIMAL(18,8) NULL,
    calculated_yoy_rate DECIMAL(18,8) NULL,
    source_file VARCHAR(255) NOT NULL,
    source_sheet VARCHAR(100) NOT NULL,
    source_row_number INT UNSIGNED NOT NULL,
    import_batch_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (record_key),
    INDEX idx_search_month_grain (report_month, grain_level),
    INDEX idx_search_brand_month (brand, report_month),
    INDEX idx_search_category_month (category, report_month),
    CONSTRAINT chk_search_month_num CHECK (report_month_num BETWEEN 1 AND 12),
    CONSTRAINT chk_search_grain CHECK (
        grain_level IN ('brand', 'brand_category')
    )
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
