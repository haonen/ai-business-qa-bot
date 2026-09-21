from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


INVALID_PERIOD = "INVALID_PERIOD"
DATA_AFTER_LATEST = "DATA_AFTER_LATEST"
NO_DATA_IN_RANGE = "NO_DATA_IN_RANGE"
QUERY_TIMEOUT = "QUERY_TIMEOUT"
INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"


@dataclass(frozen=True)
class AnalysisFailure:
    failure_kind: str
    user_message: str
    requested_period: str | None = None
    latest_available_date: str | None = None
    retry_slot: str | None = None
    preserved_slots: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["preserved_slots"] = list(self.preserved_slots)
        return payload


class StructuredAnalysisError(ValueError):
    def __init__(self, failure: AnalysisFailure):
        super().__init__(failure.user_message)
        self.failure = failure


def failure_result(failure: AnalysisFailure, *, error: str = "analysis_failure") -> dict:
    return {
        "error": error,
        "message": failure.user_message,
        "failure": failure.to_dict(),
    }


def failure_meta(
    result: dict,
    *,
    brand: str | None = None,
    period: str | None = None,
    platform: str | None = None,
    domain: str | None = None,
) -> dict:
    failure = dict(result.get("failure") or {})
    return {
        "brand": brand,
        "period": period,
        "platform": platform,
        "domain": domain,
        "document_ready": False,
        "error_code": result.get("error"),
        "failure_kind": failure.get("failure_kind") or INFRASTRUCTURE_ERROR,
        "requested_period": failure.get("requested_period") or period,
        "latest_available_date": failure.get("latest_available_date"),
        "retry_slot": failure.get("retry_slot"),
        "preserved_slots": list(failure.get("preserved_slots") or []),
    }


def infrastructure_failure(exc: Exception, *, requested_period: str | None = None) -> AnalysisFailure:
    lowered = str(exc).casefold()
    timeout = "timeout" in lowered or "timed out" in lowered or "request timed out" in lowered
    return AnalysisFailure(
        failure_kind=QUERY_TIMEOUT if timeout else INFRASTRUCTURE_ERROR,
        user_message=(
            "数据查询超时，请稍后重试；本次任务的品牌、时间和平台范围已保留。"
            if timeout else
            "数据查询或连接暂时失败，请稍后重试；本次任务范围已保留。"
        ),
        requested_period=requested_period,
        preserved_slots=("brand", "period", "platform", "goals"),
    )


def data_coverage_failure(
    *,
    brand: str,
    platform_label: str,
    requested_period: str,
    latest_available_date: str | None,
    after_latest: bool = False,
) -> AnalysisFailure:
    latest_text = (
        f"当前可查询的最新日期是{latest_available_date}。"
        if latest_available_date else "目前还无法确认可查询的最新日期。"
    )
    return AnalysisFailure(
        failure_kind=DATA_AFTER_LATEST if after_latest else NO_DATA_IN_RANGE,
        user_message=(
            f"暂时无法完成{brand}{requested_period}的{platform_label}生意分析："
            "所需数据尚未齐全，不代表这段时间没有销售。"
            f"{latest_text}你可以换一个日期继续查询，我会保留品牌和平台范围。"
        ),
        requested_period=requested_period,
        latest_available_date=latest_available_date,
        retry_slot="period",
        preserved_slots=("brand", "platform", "goals"),
    )
