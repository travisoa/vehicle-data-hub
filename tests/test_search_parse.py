"""汽车之家搜索响应解析测试（离线，autohome_cc 与 benchmark 共用同一套解析）。"""

from __future__ import annotations

import pytest

from autohome_cc.crawler.search import AutohomeSearcher, parse_series_hits
from benchmark import collector
from benchmark.collector import CollectError, search_series


def _subjects_response(subjects: list[dict]) -> dict:
    return {"simple_result": {"simple_intent": {"subjects": subjects}}}


def test_subjects_expands_comma_joined_ids_with_names():
    # 一条 subject 里逗号并列多个车系：一条 subject 里逗号并列多个车系
    data = _subjects_response(
        [
            {
                "id": "6606,7345,8612",
                "class_name": "CarSeries",
                "subject_s_name": "示例车系,示例车系C-DM,示例车系8",
            }
        ]
    )
    hits = parse_series_hits(data)
    assert [(hit.series_id, hit.name) for hit in hits] == [
        ("6606", "示例车系"),
        ("7345", "示例车系C-DM"),
        ("8612", "示例车系8"),
    ]
    assert all(hit.is_series for hit in hits)


def test_brand_class_name_is_not_a_series():
    # 实测品牌名：接口返回品牌 ID，不能当车系用
    data = _subjects_response([{"id": "609", "class_name": "Brand", "subject_s_name": "EXB 示例品牌"}])
    (hit,) = parse_series_hits(data)
    assert hit.series_id == "609"
    assert hit.is_series is False


def test_names_shorter_than_ids_leaves_name_empty():
    data = _subjects_response([{"id": "1,2", "class_name": "CarSeries", "subject_s_name": "只有一个"}])
    assert [(hit.series_id, hit.name) for hit in parse_series_hits(data)] == [
        ("1", "只有一个"),
        ("2", ""),
    ]


def test_falls_back_to_queryintent_then_term():
    intent_only = {"simple_result": {"queryintent": [{"id": "-1"}, {"id": "555", "entityName": "某车系"}]}}
    assert [(hit.series_id, hit.name) for hit in parse_series_hits(intent_only)] == [("555", "某车系")]

    term_only = {"qp_result": {"term": [{"id": 7927, "entityName": "示例车型S1", "ruleType": 4}]}}
    (hit,) = parse_series_hits(term_only)
    assert (hit.series_id, hit.name, hit.is_series) == ("7927", "示例车型S1", True)

    brand_term = {"qp_result": {"term": [{"id": 609, "entityName": "EXB 示例品牌", "ruleType": 3}]}}
    assert parse_series_hits(brand_term)[0].is_series is False


def test_duplicate_ids_are_collapsed_in_order():
    data = _subjects_response(
        [
            {"id": "10,11", "class_name": "CarSeries", "subject_s_name": "A,B"},
            {"id": "11,12", "class_name": "CarSeries", "subject_s_name": "B,C"},
        ]
    )
    assert [hit.series_id for hit in parse_series_hits(data)] == ["10", "11", "12"]


def test_empty_response_yields_no_hits():
    assert parse_series_hits({}) == []


def test_search_series_skips_brand_hit(monkeypatch):
    data = _subjects_response(
        [
            {"id": "609", "class_name": "Brand", "subject_s_name": "EXB 示例品牌"},
            {"id": "5678", "class_name": "CarSeries", "subject_s_name": "示例品牌M9"},
        ]
    )
    monkeypatch.setattr(collector, "_get_json", lambda *args, **kwargs: data)
    assert search_series("示例品牌") == ("5678", "示例品牌M9")


def test_search_series_rejects_brand_only_result(monkeypatch):
    data = _subjects_response([{"id": "609", "class_name": "Brand", "subject_s_name": "EXB 示例品牌"}])
    monkeypatch.setattr(collector, "_get_json", lambda *args, **kwargs: data)
    with pytest.raises(CollectError, match="品牌而非车系"):
        search_series("示例品牌")


def test_searcher_ranks_non_series_candidates_last():
    data = _subjects_response(
        [
            {"id": "609", "class_name": "Brand", "subject_s_name": "EXB 示例品牌"},
            {"id": "5678", "class_name": "CarSeries", "subject_s_name": "示例品牌M9"},
        ]
    )

    class _Response:
        @staticmethod
        def json() -> dict:
            return data

    class _HttpClient:
        @staticmethod
        def get_response(*args, **kwargs) -> _Response:
            return _Response()

    searcher = AutohomeSearcher(_HttpClient(), logger=None)
    candidates = searcher.search("示例品牌")
    assert [candidate.series_id for candidate in candidates] == ["5678", "609"]
