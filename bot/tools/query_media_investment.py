from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.utils import safe_div, safe_evol


_TAG_ORDERS = {
    "overall": ["B", "K", "F", "S", "T"],
    "red": ["B", "K", "F", "S"],
    "douyin": ["B", "K", "F", "S", "T"],
}

_CHANNEL_RULES = {
    "tmall": lambda media, submedia: (
        media == "tmall"
        or (
            media in {"live stream", "live streaming"}
            and "austin" in submedia
        )
    ),
    "douyin": lambda media, submedia: (
        media == "douyin qianchuan"
        or (
            media in {"live stream", "live streaming"}
            and submedia == "douyin kol live"
        )
    ),
    "jd": lambda media, submedia: media == "jd",
}

_AIT_ORDER = ["Awareness", "Influencer", "Transaction"]


def _comparison_status(current_rows: int, prior_rows: int, prior_value: float) -> str:
    if current_rows == 0:
        return "missing_current"
    if prior_rows == 0:
        return "missing_prior"
    if prior_value == 0:
        return "base_zero"
    return "ok"


def _mix_rows(
    df: pd.DataFrame,
    column: str,
    scope: str,
    current_year: int,
    prior_year: int,
) -> list[dict]:
    subset = df[df[column].notna()].copy()
    total_current = float(subset.loc[subset["year"] == current_year, "spend_million"].sum())
    total_prior = float(subset.loc[subset["year"] == prior_year, "spend_million"].sum())
    total_current_rows = int(
        subset.loc[subset["year"] == current_year, "row_count"].sum()
    )
    total_prior_rows = int(
        subset.loc[subset["year"] == prior_year, "row_count"].sum()
    )
    weight_status = _comparison_status(
        total_current_rows, total_prior_rows, total_prior
    )
    rows = []
    for label in _TAG_ORDERS[scope]:
        current = float(
            subset.loc[(subset["year"] == current_year) & (subset[column] == label), "spend_million"].sum()
        )
        prior = float(
            subset.loc[(subset["year"] == prior_year) & (subset[column] == label), "spend_million"].sum()
        )
        current_weight = safe_div(current, total_current)
        prior_weight = safe_div(prior, total_prior)
        rows.append({
            "label": label,
            "weight": current_weight,
            "weight_prior": prior_weight,
            "weight_change": (
                round(current_weight - prior_weight, 6)
                if (
                    weight_status == "ok"
                    and current_weight is not None
                    and prior_weight is not None
                )
                else None
            ),
            "weight_comparison_status": weight_status,
            "current_spend_million": round(current, 6),
            "prior_spend_million": round(prior, 6),
        })
    return rows


def _spend_summary(
    subset: pd.DataFrame,
    source: pd.DataFrame,
    current_year: int,
    prior_year: int,
) -> dict:
    current = float(subset.loc[subset["year"] == current_year, "spend_million"].sum())
    prior = float(subset.loc[subset["year"] == prior_year, "spend_million"].sum())
    current_rows = int(subset.loc[subset["year"] == current_year, "row_count"].sum())
    prior_rows = int(subset.loc[subset["year"] == prior_year, "row_count"].sum())
    source_current_rows = int(source.loc[source["year"] == current_year, "row_count"].sum())
    source_prior_rows = int(source.loc[source["year"] == prior_year, "row_count"].sum())
    status = _comparison_status(source_current_rows, source_prior_rows, prior)
    return {
        "actual_million": round(current, 6),
        "actual_yuan": round(current * 1_000_000),
        "prior_million": round(prior, 6),
        "prior_yuan": round(prior * 1_000_000),
        "evol": safe_evol(current, prior) if status == "ok" else None,
        "comparison_status": status,
        "current_rows": current_rows,
        "prior_rows": prior_rows,
    }


def _ait_rows(
    df: pd.DataFrame,
    current_year: int,
    prior_year: int,
) -> list[dict]:
    total_current = float(df.loc[df["year"] == current_year, "spend_million"].sum())
    total_prior = float(df.loc[df["year"] == prior_year, "spend_million"].sum())
    total_current_rows = int(df.loc[df["year"] == current_year, "row_count"].sum())
    total_prior_rows = int(df.loc[df["year"] == prior_year, "row_count"].sum())
    weight_status = _comparison_status(total_current_rows, total_prior_rows, total_prior)
    rows = []
    for label in _AIT_ORDER:
        subset = df[df["ait_roe"].str.casefold() == label.casefold()]
        summary = _spend_summary(subset, df, current_year, prior_year)
        weight = safe_div(summary["actual_million"], total_current)
        weight_prior = safe_div(summary["prior_million"], total_prior)
        rows.append({
            "label": label,
            **summary,
            "weight": weight,
            "weight_prior": weight_prior,
            "weight_change": (
                round(weight - weight_prior, 6)
                if (
                    weight_status == "ok"
                    and weight is not None
                    and weight_prior is not None
                )
                else None
            ),
            "weight_comparison_status": weight_status,
        })
    return rows


def _channel_summary(
    transaction: pd.DataFrame,
    source: pd.DataFrame,
    channel: str,
    current_year: int,
    prior_year: int,
) -> dict:
    rule = _CHANNEL_RULES[channel]
    mask = pd.Series(
        [
            rule(str(media or "").strip().casefold(), str(submedia or "").strip().casefold())
            for media, submedia in zip(transaction["media"], transaction["submedia"])
        ],
        index=transaction.index,
        dtype=bool,
    )
    subset = transaction.loc[mask]
    summary = _spend_summary(subset, source, current_year, prior_year)
    transaction_current = float(
        transaction.loc[transaction["year"] == current_year, "spend_million"].sum()
    )
    transaction_prior = float(
        transaction.loc[transaction["year"] == prior_year, "spend_million"].sum()
    )
    weight = safe_div(summary["actual_million"], transaction_current)
    weight_prior = safe_div(summary["prior_million"], transaction_prior)
    source_current_rows = int(source.loc[source["year"] == current_year, "row_count"].sum())
    source_prior_rows = int(source.loc[source["year"] == prior_year, "row_count"].sum())
    weight_status = _comparison_status(
        source_current_rows, source_prior_rows, transaction_prior
    )
    return {
        **summary,
        "weight": weight,
        "weight_prior": weight_prior,
        "weight_change": (
            round(weight - weight_prior, 6)
            if (
                weight_status == "ok"
                and weight is not None
                and weight_prior is not None
            )
            else None
        ),
        "weight_comparison_status": weight_status,
    }


@tool
def query_media_investment(
    brand: str,
    focus_start: str,
    focus_end: str,
    prior_start: str,
    prior_end: str,
) -> dict:
    """查询TTL、AIT、交易平台花费及Overall/RED/Douyin BKFS结构。"""
    try:
        current_year = int(str(focus_start)[:4])
        prior_year = int(str(prior_start)[:4])
        df = fetch_df(
            """
            SELECT
                year,
                period_month,
                media,
                submedia,
                ait_roe,
                bkfs_overall,
                bkfs_xiaohongshu,
                bkfs_douyin,
                SUM(spend_million) AS spend_million,
                COUNT(*) AS row_count
            FROM ai_bot_media_topline_investment
            WHERE brand_r = :brand
              AND (
                period_month BETWEEN :focus_start AND :focus_end
                OR period_month BETWEEN :prior_start AND :prior_end
              )
            GROUP BY
                year, period_month, media, submedia, ait_roe,
                bkfs_overall, bkfs_xiaohongshu, bkfs_douyin
            """,
            {
                "brand": brand,
                "focus_start": focus_start,
                "focus_end": focus_end,
                "prior_start": prior_start,
                "prior_end": prior_end,
            },
        )
        if df.empty:
            return {
                "error": "no_data",
                "message": f"Topline中没有找到品牌“{brand}”在指定期间的数据。",
                "brand": brand,
            }
        df["year"] = pd.to_numeric(df["year"], errors="coerce")
        df["spend_million"] = pd.to_numeric(df["spend_million"], errors="coerce").fillna(0.0)
        df["row_count"] = pd.to_numeric(df["row_count"], errors="coerce").fillna(0).astype(int)
        df["period_month"] = pd.to_datetime(df["period_month"])
        for column in ("media", "submedia", "ait_roe"):
            if column not in df:
                df[column] = ""
            df[column] = df[column].fillna("").astype(str).str.strip()
        current = float(df.loc[df["year"] == current_year, "spend_million"].sum())
        prior = float(df.loc[df["year"] == prior_year, "spend_million"].sum())
        current_rows = int(df.loc[df["year"] == current_year, "row_count"].sum())
        prior_rows = int(df.loc[df["year"] == prior_year, "row_count"].sum())
        status = _comparison_status(current_rows, prior_rows, prior)
        ait_rows = _ait_rows(df, current_year, prior_year)
        transaction = df[df["ait_roe"].str.casefold() == "transaction"].copy()
        transaction_channels = {
            channel: _channel_summary(
                transaction, df, channel, current_year, prior_year
            )
            for channel in ("tmall", "douyin", "jd")
        }
        return {
            "brand": brand,
            "matched_brand": brand,
            "date_range": {
                "current": [focus_start, focus_end],
                "prior": [prior_start, prior_end],
            },
            "ttl": {
                "actual_million": round(current, 6),
                "actual_yuan": round(current * 1_000_000),
                "prior_million": round(prior, 6),
                "prior_yuan": round(prior * 1_000_000),
                "evol": safe_evol(current, prior) if status == "ok" else None,
                "comparison_status": status,
            },
            "ait": ait_rows,
            "transaction_platforms": [
                {"label": label, **transaction_channels[key]}
                for key, label in (("tmall", "TMALL"), ("douyin", "Douyin"), ("jd", "JD"))
            ],
            # Backward-compatible key; values now follow the Transaction-only rule.
            "channels": transaction_channels,
            "mix": {
                "overall": _mix_rows(df, "bkfs_overall", "overall", current_year, prior_year),
                "red": _mix_rows(df, "bkfs_xiaohongshu", "red", current_year, prior_year),
                "douyin": _mix_rows(df, "bkfs_douyin", "douyin", current_year, prior_year),
            },
            "coverage": {
                "current_months": sorted(
                    df.loc[df["year"] == current_year, "period_month"].dt.strftime("%Y-%m").unique().tolist()
                ),
                "prior_months": sorted(
                    df.loc[df["year"] == prior_year, "period_month"].dt.strftime("%Y-%m").unique().tolist()
                ),
                "current_rows": current_rows,
                "prior_rows": prior_rows,
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
