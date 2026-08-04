from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import re
from typing import TYPE_CHECKING

import pandas as pd
from dotenv import load_dotenv

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "topline"
RULES_PATH = Path(__file__).resolve().parent / "config" / "bkfs_rules.json"
SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "media_topline_investment.sql"
TABLE_NAME = "ai_bot_media_topline_investment"

load_dotenv(BASE_DIR / ".env")


SOURCE_COLUMN_MAP = {
    "Category": "category",
    "Brand-R": "brand_r",
    "Universe": "universe",
    "Year": "year",
    "Month": "month",
    "Luxe_Brands": "luxe_brands",
    "DivisionforLOREAL": "division_for_loreal",
    "Cate": "cate",
    "Cate-1": "cate_1",
    "Cate-2": "cate_2",
    "top15group": "top_15_group",
    "Grouptype": "group_type",
    "Media": "media",
    "KeyCompetitors": "key_competitors",
    "Submedia": "submedia",
    "SponsorType": "sponsor_type",
    "Total(M)": "spend_million",
    "TotalwoVAT": "spend_wo_vat_million",
    "StandardAPPName": "standard_app_name",
    "StandardAdFormat": "standard_ad_format",
    "AIT(ROE)": "ait_roe",
}

TEXT_COLUMNS = [
    "category",
    "brand_r",
    "universe",
    "luxe_brands",
    "division_for_loreal",
    "cate",
    "cate_1",
    "cate_2",
    "top_15_group",
    "group_type",
    "media",
    "key_competitors",
    "submedia",
    "sponsor_type",
    "standard_app_name",
    "standard_ad_format",
    "ait_roe",
]


@dataclass
class QualityReport:
    source_file: str
    batch_id: str
    source_rows: int = 0
    clean_rows: int = 0
    partitions: list[dict] = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    negative_spend: dict = field(default_factory=dict)
    duplicate_source_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "batch_id": self.batch_id,
            "source_rows": self.source_rows,
            "clean_rows": self.clean_rows,
            "partitions": self.partitions,
            "labels": self.labels,
            "negative_spend": self.negative_spend,
            "duplicate_source_rows": self.duplicate_source_rows,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def normalize_header(value: object) -> str:
    return re.sub(r"\s+", "", str(value).replace("\u00a0", " ")).strip()


def normalize_label(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()
    return text or None


def load_rules(path: str | Path = RULES_PATH) -> dict:
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    required_scopes = {"overall", "xiaohongshu", "douyin"}
    missing = required_scopes - set(rules)
    if missing:
        raise ValueError(f"BKFS 配置缺少 scope: {sorted(missing)}")
    return rules


def standardize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    source_by_normalized = {
        normalize_header(column): column for column in frame.columns
    }
    missing = [
        source_name
        for source_name in SOURCE_COLUMN_MAP
        if source_name not in source_by_normalized
    ]
    if missing:
        raise ValueError(f"Topline 缺少字段: {missing}")
    rename_map = {
        source_by_normalized[source_name]: target_name
        for source_name, target_name in SOURCE_COLUMN_MAP.items()
    }
    return frame[list(rename_map)].rename(columns=rename_map)


def platform_mask(frame: pd.DataFrame, scope_rules: dict) -> pd.Series:
    tokens = scope_rules.get("platform_contains") or []
    if not tokens:
        return pd.Series(True, index=frame.index)
    app = frame["standard_app_name"].fillna("")
    pattern = "|".join(re.escape(token) for token in tokens)
    return app.str.contains(pattern, case=False, regex=True, na=False)


def classify_scope(frame: pd.DataFrame, scope_rules: dict) -> pd.Series:
    eligible = platform_mask(frame, scope_rules)
    labels = pd.Series(pd.NA, index=frame.index, dtype="object")

    for source_value, label in (scope_rules.get("ait_roe") or {}).items():
        mask = (
            eligible
            & labels.isna()
            & frame["ait_roe"].fillna("").str.casefold().eq(source_value.casefold())
        )
        labels.loc[mask] = label

    ad_format = frame["standard_ad_format"].fillna("")
    for label, tokens in (
        scope_rules.get("standard_ad_format_contains") or {}
    ).items():
        if not tokens:
            continue
        pattern = "|".join(re.escape(token) for token in tokens)
        mask = (
            eligible
            & labels.isna()
            & ad_format.str.contains(pattern, case=False, regex=True, na=False)
        )
        labels.loc[mask] = label

    return labels.where(eligible, pd.NA)


def make_record_key(source_file: str, source_sheet: str, row_number: int) -> str:
    payload = f"{source_file}|{source_sheet}|{row_number}"
    return sha256(payload.encode("utf-8")).hexdigest()


def summarize_scope(
    frame: pd.DataFrame,
    label_column: str,
    scope_rules: dict,
) -> dict:
    eligible = platform_mask(frame, scope_rules)
    scoped = frame.loc[
        eligible,
        [label_column, "spend_million", "standard_ad_format", "ait_roe"],
    ].copy()
    scoped["label"] = scoped[label_column].fillna("UNTAGGED")
    grouped = (
        scoped.groupby("label", dropna=False)["spend_million"]
        .agg(["size", "sum"])
        .reset_index()
    )
    unmatched = (
        scoped[scoped[label_column].isna()]
        .groupby(["ait_roe", "standard_ad_format"], dropna=False)["spend_million"]
        .agg(["size", "sum"])
        .sort_values("sum", ascending=False)
        .head(20)
        .reset_index()
    )
    return {
        "eligible_rows": int(eligible.sum()),
        "eligible_spend_million": round(
            float(frame.loc[eligible, "spend_million"].sum()), 6
        ),
        "by_label": {
            str(row["label"]): {
                "rows": int(row["size"]),
                "spend_million": round(float(row["sum"]), 6),
            }
            for _, row in grouped.iterrows()
        },
        "top_untagged": [
            {
                "ait_roe": normalize_label(row["ait_roe"]),
                "standard_ad_format": normalize_label(row["standard_ad_format"]),
                "rows": int(row["size"]),
                "spend_million": round(float(row["sum"]), 6),
            }
            for _, row in unmatched.iterrows()
        ],
    }


def transform_workbook(
    input_path: str | Path,
    *,
    rules_path: str | Path = RULES_PATH,
    batch_id: str | None = None,
) -> tuple[pd.DataFrame, QualityReport]:
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    batch_id = batch_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quality = QualityReport(source_file=path.name, batch_id=batch_id)
    rules = load_rules(rules_path)
    workbook = pd.ExcelFile(path)
    if len(workbook.sheet_names) != 1:
        quality.warnings.append(
            f"工作簿包含 {len(workbook.sheet_names)} 个 sheet，仅处理第一个"
        )
    sheet_name = workbook.sheet_names[0]
    source = pd.read_excel(path, sheet_name=sheet_name, dtype=object)
    quality.source_rows = int(len(source))
    frame = standardize_columns(source)

    for column in TEXT_COLUMNS:
        frame[column] = frame[column].map(normalize_label)
    frame["year"] = pd.to_numeric(frame["year"], errors="coerce")
    frame["month"] = pd.to_numeric(frame["month"], errors="coerce")
    frame["spend_million"] = pd.to_numeric(
        frame["spend_million"], errors="coerce"
    )
    frame["spend_wo_vat_million"] = pd.to_numeric(
        frame["spend_wo_vat_million"], errors="coerce"
    )

    invalid_required = (
        frame["year"].isna()
        | frame["month"].isna()
        | frame["brand_r"].isna()
        | frame["spend_million"].isna()
    )
    if invalid_required.any():
        raise ValueError(
            f"发现 {int(invalid_required.sum())} 行缺少年份、月份、品牌或 Total(M)"
        )
    invalid_month = ~frame["month"].between(1, 12)
    if invalid_month.any():
        raise ValueError(f"发现 {int(invalid_month.sum())} 行月份不在 1–12")

    frame["year"] = frame["year"].astype(int)
    frame["month"] = frame["month"].astype(int)
    frame["period_month"] = pd.to_datetime(
        {
            "year": frame["year"],
            "month": frame["month"],
            "day": 1,
        }
    )
    frame["bkfs_overall"] = classify_scope(frame, rules["overall"])
    frame["bkfs_xiaohongshu"] = classify_scope(frame, rules["xiaohongshu"])
    frame["bkfs_douyin"] = classify_scope(frame, rules["douyin"])

    source_columns = list(frame.columns)
    quality.duplicate_source_rows = int(
        frame[source_columns].duplicated(keep=False).sum()
    )
    if quality.duplicate_source_rows:
        quality.warnings.append(
            f"发现 {quality.duplicate_source_rows} 行完全重复的源记录；"
            "缺少投放唯一键，按原始行保留，不做去重"
        )

    source_rows = pd.Series(range(2, len(frame) + 2), index=frame.index)
    frame.insert(
        0,
        "record_key",
        [
            make_record_key(path.name, sheet_name, int(row_number))
            for row_number in source_rows
        ],
    )
    frame["source_file"] = path.name
    frame["source_sheet"] = sheet_name
    frame["source_row_number"] = source_rows.astype(int)
    frame["import_batch_id"] = batch_id

    quality.clean_rows = int(len(frame))
    partition_summary = (
        frame.groupby(["year", "month"], dropna=False)["spend_million"]
        .agg(["size", "sum"])
        .reset_index()
    )
    quality.partitions = [
        {
            "year": int(row["year"]),
            "month": int(row["month"]),
            "rows": int(row["size"]),
            "spend_million": round(float(row["sum"]), 6),
        }
        for _, row in partition_summary.iterrows()
    ]
    quality.labels = {
        "overall": summarize_scope(frame, "bkfs_overall", rules["overall"]),
        "xiaohongshu": summarize_scope(
            frame, "bkfs_xiaohongshu", rules["xiaohongshu"]
        ),
        "douyin": summarize_scope(frame, "bkfs_douyin", rules["douyin"]),
    }
    negative = frame["spend_million"] < 0
    quality.negative_spend = {
        "rows": int(negative.sum()),
        "spend_million": round(
            float(frame.loc[negative, "spend_million"].sum()), 6
        ),
    }
    if negative.any():
        quality.warnings.append(
            f"保留 {int(negative.sum())} 行负数 Total(M)，"
            "主要用于 Rebate 等净额调整"
        )

    ordered_columns = [
        "record_key",
        "period_month",
        "year",
        "month",
        "category",
        "brand_r",
        "universe",
        "luxe_brands",
        "division_for_loreal",
        "cate",
        "cate_1",
        "cate_2",
        "top_15_group",
        "group_type",
        "media",
        "key_competitors",
        "submedia",
        "sponsor_type",
        "spend_million",
        "spend_wo_vat_million",
        "standard_app_name",
        "standard_ad_format",
        "ait_roe",
        "bkfs_overall",
        "bkfs_xiaohongshu",
        "bkfs_douyin",
        "source_file",
        "source_sheet",
        "source_row_number",
        "import_batch_id",
    ]
    frame = frame[ordered_columns].sort_values(
        ["year", "month", "source_row_number"]
    ).reset_index(drop=True)
    return frame, quality


def get_engine() -> "Engine":
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL

    url = URL.create(
        "mysql+pymysql",
        username=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        database=os.environ["MYSQL_DATABASE"],
        query={"charset": "utf8mb4"},
    )
    return create_engine(url, pool_pre_ping=True)


def create_table(engine: "Engine") -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text(SCHEMA_PATH.read_text(encoding="utf-8")))


def serialize_records(frame: pd.DataFrame) -> list[dict]:
    records = frame.copy()
    records["period_month"] = pd.to_datetime(records["period_month"]).dt.date
    records = records.astype(object).where(pd.notnull(records), None)
    return records.to_dict(orient="records")


def load_mysql(
    frame: pd.DataFrame,
    engine: "Engine",
    *,
    chunk_size: int = 1000,
) -> int:
    from sqlalchemy import MetaData, Table

    create_table(engine)
    table = Table(TABLE_NAME, MetaData(), autoload_with=engine)
    rows = serialize_records(frame)
    partitions = (
        frame[["year", "month"]].drop_duplicates().to_dict(orient="records")
    )
    total = 0
    with engine.begin() as connection:
        for partition in partitions:
            connection.execute(
                table.delete().where(
                    (table.c.year == int(partition["year"]))
                    & (table.c.month == int(partition["month"]))
                )
            )
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            connection.execute(table.insert(), chunk)
            total += len(chunk)
    return total


def write_outputs(
    frame: pd.DataFrame,
    quality: QualityReport,
    output_dir: str | Path,
) -> tuple[Path, Path, Path]:
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    parquet_path = directory / "media_topline_investment.parquet"
    sample_path = directory / "media_topline_investment_sample.csv"
    quality_path = directory / "media_topline_quality_report.json"
    frame.to_parquet(parquet_path, index=False)
    frame.head(2000).to_csv(sample_path, index=False, encoding="utf-8-sig")
    quality_path.write_text(
        json.dumps(quality.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parquet_path, sample_path, quality_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清洗 Topline 媒体投资数据，打 BKFS 标签并可写入 MySQL。"
    )
    parser.add_argument("--input", required=True, help="Topline.xlsx 路径")
    parser.add_argument(
        "--rules",
        default=str(RULES_PATH),
        help="BKFS JSON 规则文件",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="清洗结果和质量报告输出目录",
    )
    parser.add_argument("--batch-id", help="导入批次；默认使用 UTC 时间")
    parser.add_argument(
        "--load-mysql",
        action="store_true",
        help="创建表并替换本次文件覆盖的 MySQL 年月分区",
    )
    parser.add_argument("--chunk-size", type=int, default=1000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cleaned, quality = transform_workbook(
        args.input,
        rules_path=args.rules,
        batch_id=args.batch_id,
    )
    parquet_path, sample_path, quality_path = write_outputs(
        cleaned,
        quality,
        args.output_dir,
    )
    print(f"clean rows: {len(cleaned):,}")
    print(f"parquet: {parquet_path}")
    print(f"sample csv: {sample_path}")
    print(f"quality report: {quality_path}")
    for warning in quality.warnings:
        print(f"warning: {warning}")
    if args.load_mysql:
        inserted = load_mysql(cleaned, get_engine(), chunk_size=args.chunk_size)
        print(f"MySQL partition replace completed: {inserted:,} rows")


if __name__ == "__main__":
    main()
