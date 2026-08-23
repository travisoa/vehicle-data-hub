"""使用 Autohome 一方接口定位车型。

接口地址、请求参数与响应解析集中在本模块，benchmark 侧复用同一套解析
（各自保留自己的网络层），接口结构变化时只需改这里一处。
"""

from __future__ import annotations

from dataclasses import dataclass

from autohome_cc.utils.cleaners import normalize_whitespace

SEARCH_API_URL = "https://sou.autohome.com.cn/afu_search_proxy_api/qp_full"
SEARCH_REFERER = "https://sou.autohome.com.cn/"


@dataclass
class SeriesHit:
    """搜索响应里的一个候选条目。"""

    series_id: str
    name: str
    is_series: bool
    """接口标注为车系（CarSeries）时为 True；品牌等非车系条目为 False。

    搜品牌名（而非车系名）时接口返回的是品牌 ID，拿它当车系 ID 会查到空数据。
    """


@dataclass
class SearchCandidate:
    """搜索候选结果。"""

    model_name: str
    query: str
    title: str
    snippet: str
    url: str
    rank: int
    score: int
    series_id: str
    brand: str


def build_search_params(model_name: str) -> dict[str, str]:
    return {"qptype": "1", "q": model_name}


def parse_series_hits(data: dict) -> list[SeriesHit]:
    """从搜索响应解析候选条目，按接口原始顺序去重。

    三级取值，逐级兜底：
    1. simple_result.simple_intent.subjects：信息最全，一条里可能用逗号并列多个车系
    2. simple_result.queryintent
    3. qp_result.term：ruleType 4 为车系、3 为品牌
    """
    simple_result = data.get("simple_result") or {}
    simple_intent = simple_result.get("simple_intent") or {}
    hits: list[SeriesHit] = []

    for subject in simple_intent.get("subjects") or []:
        raw_ids = normalize_whitespace(str(subject.get("id", "")))
        raw_names = normalize_whitespace(str(subject.get("subject_s_name", "")))
        ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
        names = [item.strip() for item in raw_names.split(",") if item.strip()]
        is_series = str(subject.get("class_name", "")).strip() == "CarSeries"
        for index, series_id in enumerate(ids):
            hits.append(
                SeriesHit(
                    series_id=series_id,
                    name=names[index] if index < len(names) else "",
                    is_series=is_series,
                )
            )

    if not hits:
        for intent in simple_result.get("queryintent") or []:
            intent_id = str(intent.get("id", "")).strip()
            if not intent_id or intent_id == "-1":
                continue
            hits.append(
                SeriesHit(
                    series_id=intent_id,
                    name=normalize_whitespace(str(intent.get("entityName", ""))),
                    is_series=True,
                )
            )

    if not hits:
        for term in (data.get("qp_result") or {}).get("term") or []:
            term_id = str(term.get("id", "")).strip()
            entity_name = normalize_whitespace(str(term.get("entityName", "")))
            if not term_id or term_id == "-1" or not entity_name:
                continue
            hits.append(
                SeriesHit(series_id=term_id, name=entity_name, is_series=term.get("ruleType") == 4)
            )

    seen: set[str] = set()
    unique: list[SeriesHit] = []
    for hit in hits:
        if hit.series_id in seen:
            continue
        seen.add(hit.series_id)
        unique.append(hit)
    return unique


class AutohomeSearcher:
    """优先使用 Autohome 自己的搜索接口。"""

    def __init__(self, http_client, logger) -> None:
        self.http_client = http_client
        self.logger = logger

    def search(self, model_name: str) -> list[SearchCandidate]:
        """搜索车型并返回候选车系页。"""
        response = self.http_client.get_response(
            SEARCH_API_URL,
            params=build_search_params(model_name),
            referer=SEARCH_REFERER,
        )
        data = response.json()
        candidates = self._build_candidates(model_name, data)
        candidates.sort(key=lambda item: (-item.score, item.rank))
        return candidates

    def _build_candidates(self, model_name: str, data: dict) -> list[SearchCandidate]:
        simple_result = data.get("simple_result") or {}
        entity_results = simple_result.get("entity_result") or []
        brand_name = ""
        if entity_results:
            brand_name = normalize_whitespace(str(entity_results[0].get("belong_to_brand", "")))

        candidates: list[SearchCandidate] = []
        for rank, hit in enumerate(parse_series_hits(data), start=1):
            title = hit.name or model_name
            score = 100 - rank
            if normalize_whitespace(title).lower() == normalize_whitespace(model_name).lower():
                score += 20
            # 品牌等非车系条目的详情页取不到配置，排到全部车系候选之后再尝试
            if not hit.is_series:
                score -= 50

            candidates.append(
                SearchCandidate(
                    model_name=model_name,
                    query=model_name,
                    title=title,
                    snippet=simple_result.get("entityName", ""),
                    url=f"https://car.autohome.com.cn/config/series/{hit.series_id}.html",
                    rank=rank,
                    score=score,
                    series_id=hit.series_id,
                    brand=brand_name,
                )
            )

        if not candidates:
            self.logger.warning(
                "Autohome 搜索未返回车系候选",
                model=model_name,
                stage="search",
                details=f"response_code={data.get('code', '')}",
            )
        return candidates
