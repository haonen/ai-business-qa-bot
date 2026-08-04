from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import re
from typing import TYPE_CHECKING, Iterable

import pandas as pd
from dotenv import load_dotenv

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
TABLE_NAME = "ai_bot_media_search_index"
GRAIN_BRAND = "brand"
GRAIN_BRAND_CATEGORY = "brand_category"
YOY_TOLERANCE = 0.0002

load_dotenv(BASE_DIR / ".env")


@dataclass
class QualityReport:
    source_file: str
    batch_id: str
    sheets: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "batch_id": self.batch_id,
            "sheets": self.sheets,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def normalize_header(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).replace("\u00a0", " ")).strip()


def normalize_label(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    label = re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()
    return label or None


def parse_integer(value: object) -> int | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    parsed = pd.to_numeric(str(value).replace(",", "").strip(), errors="coerce")
    if pd.isna(parsed):
        return None
    return int(round(float(parsed)))


def parse_rate(value: object) -> float | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.endswith("%"):
            parsed = pd.to_numeric(cleaned[:-1], errors="coerce")
            return None if pd.isna(parsed) else float(parsed) / 100
        parsed = pd.to_numeric(cleaned, errors="coerce")
    else:
        parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def calculate_yoy(current_value: int | None, previous_value: int | None) -> float | None:
    if current_value is None or previous_value in (None, 0):
        return None
    return (current_value - previous_value) / previous_value


def parse_sheet_month(sheet_name: str) -> int:
    match = re.fullmatch(r"\s*(\d{1,2})月\s*", sheet_name)
    if not match:
        raise ValueError(f"无法从 sheet 名解析月份: {sheet_name!r}")
    month = int(match.group(1))
    if not 1 <= month <= 12:
        raise ValueError(f"sheet 月份超出范围: {sheet_name!r}")
    return month


def find_column(columns: Iterable[object], normalized_name: str) -> object | None:
    for column in columns:
        if normalize_header(column) == normalized_name:
            return column
    return None


def find_index_columns(
    columns: Iterable[object],
    *,
    sheet_month: int,
    fallback_report_year: int | None,
) -> tuple[object, object, int, int, list[str]]:
    current_candidates: list[tuple[object, int, int]] = []
    previous_candidates: list[tuple[object, int, int]] = []
    warnings: list[str] = []

    for column in columns:
        name = normalize_header(column)
        match = re.fullmatch(r"(\d{4})年(\d{2})月搜索指数", name)
        if not match:
            continue
        year, header_month = int(match.group(1)), int(match.group(2))
        if year >= 2000:
            current_candidates.append((column, year, header_month))

    if current_candidates:
        current_column, current_year, current_header_month = max(
            current_candidates, key=lambda item: item[1]
        )
        previous = [
            item for item in current_candidates
            if item[1] == current_year - 1
        ]
        if not previous:
            raise ValueError(f"缺少 {current_year - 1} 年搜索指数列")
        previous_column, previous_year, previous_header_month = previous[0]
        if current_header_month != sheet_month or previous_header_month != sheet_month:
            warnings.append(
                f"sheet={sheet_month}月，但搜索指数表头月份为"
                f"{current_header_month}/{previous_header_month}月；按 sheet 月份入库"
            )
        return current_column, previous_column, current_year, previous_year, warnings

    current_column = find_column(columns, "now_date_search_index")
    previous_column = find_column(columns, "last_date_search_index")
    if current_column is None or previous_column is None:
        raise ValueError("未找到当年/上年搜索指数列")
    if fallback_report_year is None:
        raise ValueError(
            "通用搜索指数字段无法推断年份，请通过 --report-year 指定报告年份"
        )
    warnings.append(
        f"{sheet_month}月使用通用搜索指数字段，报告年份按 {fallback_report_year} 处理"
    )
    return (
        current_column,
        previous_column,
        fallback_report_year,
        fallback_report_year - 1,
        warnings,
    )


def infer_report_year(path: Path) -> int | None:
    years: list[int] = []
    workbook = pd.ExcelFile(path)
    for sheet_name in workbook.sheet_names:
        frame = pd.read_excel(path, sheet_name=sheet_name, nrows=0)
        for column in frame.columns:
            match = re.fullmatch(
                r"(\d{4})年\d{2}月搜索指数",
                normalize_header(column),
            )
            if match:
                years.append(int(match.group(1)))
    return max(years) if years else None


def make_record_key(
    report_year: int,
    report_month_num: int,
    grain_level: str,
    brand: str,
    category: str | None,
) -> str:
    payload = "|".join(
        [
            str(report_year),
            str(report_month_num),
            grain_level,
            brand,
            category or "",
        ]
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def transform_sheet(
    frame: pd.DataFrame,
    *,
    source_file: str,
    sheet_name: str,
    fallback_report_year: int | None,
    batch_id: str,
) -> tuple[pd.DataFrame, dict, list[str]]:
    sheet_month = parse_sheet_month(sheet_name)
    rank_column = find_column(frame.columns, "排名")
    brand_column = find_column(frame.columns, "品牌")
    category_column = find_column(frame.columns, "类目")
    source_yoy_column = find_column(frame.columns, "同比增长率")
    if rank_column is None or brand_column is None:
        raise ValueError(f"{sheet_name}: 缺少排名或品牌列")

    (
        current_index_column,
        previous_index_column,
        report_year,
        previous_year,
        warnings,
    ) = find_index_columns(
        frame.columns,
        sheet_month=sheet_month,
        fallback_report_year=fallback_report_year,
    )

    has_category = (
        category_column is not None
        and frame[category_column].map(normalize_label).notna().any()
    )
    grain_level = GRAIN_BRAND_CATEGORY if has_category else GRAIN_BRAND
    records: list[dict] = []
    invalid_rows = 0
    yoy_mismatch_rows = 0

    for source_index, source_row in frame.iterrows():
        brand = normalize_label(source_row.get(brand_column))
        if not brand:
            if source_row.isna().all():
                continue
            invalid_rows += 1
            continue
        category = (
            normalize_label(source_row.get(category_column))
            if has_category
            else None
        )
        current_index = parse_integer(source_row.get(current_index_column))
        previous_index = parse_integer(source_row.get(previous_index_column))
        current_rank = parse_integer(source_row.get(rank_column))
        source_yoy = (
            parse_rate(source_row.get(source_yoy_column))
            if source_yoy_column is not None
            else None
        )
        calculated_yoy = calculate_yoy(current_index, previous_index)

        if has_category and not category:
            invalid_rows += 1
            continue
        if current_rank is None or current_index is None or previous_index is None:
            invalid_rows += 1
            continue
        if (
            source_yoy is not None
            and calculated_yoy is not None
            and abs(source_yoy - calculated_yoy) > YOY_TOLERANCE
        ):
            yoy_mismatch_rows += 1

        record_key = make_record_key(
            report_year,
            sheet_month,
            grain_level,
            brand,
            category,
        )
        records.append(
            {
                "record_key": record_key,
                "report_month": pd.Timestamp(report_year, sheet_month, 1),
                "report_year": report_year,
                "report_month_num": sheet_month,
                "previous_year": previous_year,
                "grain_level": grain_level,
                "brand": brand,
                "category": category,
                "current_rank": current_rank,
                "current_search_index": current_index,
                "previous_search_index": previous_index,
                "source_yoy_rate": source_yoy,
                "calculated_yoy_rate": calculated_yoy,
                "source_file": source_file,
                "source_sheet": sheet_name,
                "source_row_number": int(source_index) + 2,
                "import_batch_id": batch_id,
            }
        )

    cleaned = pd.DataFrame.from_records(records)
    if cleaned.empty:
        raise ValueError(f"{sheet_name}: 清洗后没有有效数据")
    duplicate_count = int(cleaned["record_key"].duplicated(keep=False).sum())
    if duplicate_count:
        raise ValueError(f"{sheet_name}: 发现 {duplicate_count} 行重复业务键")
    if invalid_rows:
        warnings.append(f"{sheet_name}: 跳过 {invalid_rows} 行无效数据")
    if yoy_mismatch_rows:
        warnings.append(
            f"{sheet_name}: {yoy_mismatch_rows} 行源同比与重算同比差异超过"
            f" {YOY_TOLERANCE:.4%}"
        )

    sheet_summary = {
        "sheet": sheet_name,
        "report_year": report_year,
        "month": sheet_month,
        "grain_level": grain_level,
        "source_rows": int(len(frame)),
        "clean_rows": int(len(cleaned)),
        "invalid_rows": invalid_rows,
        "yoy_mismatch_rows": yoy_mismatch_rows,
    }
    return cleaned, sheet_summary, warnings


def transform_workbook(
    input_path: str | Path,
    *,
    report_year: int | None = None,
    batch_id: str | None = None,
) -> tuple[pd.DataFrame, QualityReport]:
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    batch_id = batch_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inferred_year = report_year or infer_report_year(path)
    quality = QualityReport(source_file=path.name, batch_id=batch_id)
    workbook = pd.ExcelFile(path)
    frames: list[pd.DataFrame] = []

    for sheet_name in workbook.sheet_names:
        try:
            frame = pd.read_excel(path, sheet_name=sheet_name, dtype=object)
            cleaned, summary, warnings = transform_sheet(
                frame,
                source_file=path.name,
                sheet_name=sheet_name,
                fallback_report_year=inferred_year,
                batch_id=batch_id,
            )
            frames.append(cleaned)
            quality.sheets.append(summary)
            quality.warnings.extend(warnings)
        except Exception as exc:
            quality.errors.append(f"{sheet_name}: {exc}")

    if quality.errors:
        raise ValueError(
            "搜索数据清洗失败:\n- " + "\n- ".join(quality.errors)
        )
    combined = pd.concat(frames, ignore_index=True)
    if combined["record_key"].duplicated().any():
        raise ValueError("工作簿跨 sheet 存在重复业务键")
    combined = combined.sort_values(
        ["report_year", "report_month_num", "grain_level", "current_rank", "brand"]
    ).reset_index(drop=True)
    return combined, quality


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

    schema_path = Path(__file__).resolve().parent / "sql" / "media_search_index.sql"
    ddl = schema_path.read_text(encoding="utf-8")
    with engine.begin() as connection:
        connection.execute(text(ddl))


def serialize_records(frame: pd.DataFrame) -> list[dict]:
    records = frame.copy()
    records["report_month"] = pd.to_datetime(records["report_month"]).dt.date
    records = records.astype(object).where(pd.notnull(records), None)
    return records.to_dict(orient="records")


def load_mysql(
    frame: pd.DataFrame,
    engine: "Engine",
    *,
    chunk_size: int = 2000,
) -> int:
    from sqlalchemy import MetaData, Table

    create_table(engine)
    table = Table(TABLE_NAME, MetaData(), autoload_with=engine)
    rows = serialize_records(frame)
    partitions = (
        frame[["report_year", "report_month_num", "grain_level"]]
        .drop_duplicates()
        .to_dict(orient="records")
    )
    total = 0
    with engine.begin() as connection:
        # Each source sheet is a complete monthly grain. Replacing its partition
        # removes stale rows when a corrected source file drops a brand/category.
        for partition in partitions:
            connection.execute(
                table.delete().where(
                    (table.c.report_year == int(partition["report_year"]))
                    & (
                        table.c.report_month_num
                        == int(partition["report_month_num"])
                    )
                    & (table.c.grain_level == partition["grain_level"])
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
    parquet_path = directory / "media_search_index.parquet"
    csv_path = directory / "media_search_index.csv"
    quality_path = directory / "media_search_quality_report.json"
    frame.to_parquet(parquet_path, index=False)
    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    quality_path.write_text(
        json.dumps(quality.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parquet_path, csv_path, quality_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清洗 Search Report，并可按月度粒度分区幂等写入 MySQL。"
    )
    parser.add_argument("--input", required=True, help="Search Report.xlsx 路径")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="清洗结果和质量报告输出目录",
    )
    parser.add_argument(
        "--report-year",
        type=int,
        help="报告年份；仅在字段名无法推断年份时必填",
    )
    parser.add_argument("--batch-id", help="导入批次标识；默认使用 UTC 时间")
    parser.add_argument(
        "--load-mysql",
        action="store_true",
        help="清洗通过后创建表，并替换本次文件覆盖的 MySQL 月度粒度分区",
    )
    parser.add_argument("--chunk-size", type=int, default=2000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cleaned, quality = transform_workbook(
        args.input,
        report_year=args.report_year,
        batch_id=args.batch_id,
    )
    parquet_path, csv_path, quality_path = write_outputs(
        cleaned,
        quality,
        args.output_dir,
    )
    print(f"clean rows: {len(cleaned):,}")
    print(f"parquet: {parquet_path}")
    print(f"csv: {csv_path}")
    print(f"quality report: {quality_path}")
    for warning in quality.warnings:
        print(f"warning: {warning}")
    if args.load_mysql:
        inserted = load_mysql(cleaned, get_engine(), chunk_size=args.chunk_size)
        print(f"MySQL partition replace completed: {inserted:,} rows")


if __name__ == "__main__":
    main()
