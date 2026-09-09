"""变更扩展公示查询与最新有效公告解析测试。"""

from __future__ import annotations

import json
import pytest

from miit_gonggao import change_notice


ARTICLE_URL = "https://www.miit.gov.cn/datainfo/cpgg/art/2026/art_demo.html"
IFRAME_URL = "https://www.miit.gov.cn/datainfo/change404/index.html"


def _source() -> change_notice.ChangeNoticeSource:
    return change_notice.ChangeNoticeSource(
        notice_url=ARTICLE_URL,
        title="第404批《道路机动车辆生产企业及产品公告》变更扩展公示",
        published_at="2026-02-06 14:52",
        batch="404",
        iframe_url=IFRAME_URL,
        unit_url="https://www.miit.gov.cn/api/unit",
        unit_params={"pageId": "page-404", "webId": "web-1"},
    )


def _result_html(*, model_code: str, total: int = 1, page_size: int = 20) -> str:
    return f"""
    <div class="page-content"><table>
      <tr><th>标题</th><th>批次或底盘ID</th><th>企业名称</th>
          <th>产品商标</th><th>产品名称</th><th>产品型号</th></tr>
      <tr>
        <td style="display:none"><div title="多用途乘用车"><a href="art/demo.html">详情</a></div></td>
        <td style="display:none"><div title="404">404</div></td>
        <td><div title="示例汽车有限公司">示例汽车有限公司</div></td>
        <td><div title="示例牌">示例牌</div></td>
        <td><div title="多用途乘用车">多用途乘用车</div></td>
        <td><div title="{model_code}">{model_code}</div></td>
      </tr>
    </table></div>
    <div id="autUDB_pagination"
         queryData="{{'rows':'{page_size}','count':'{total}','pageNo':'1'}}"></div>
    """


def test_parse_change_notice_source():
    article_html = """
    <html><head>
      <title>备用标题</title>
      <meta name="ArticleTitle" content="第404批《道路机动车辆生产企业及产品公告》变更扩展公示">
      <meta name="PubDate" content="2026-02-06 14:52">
    </head><body><iframe src="/datainfo/change404/index.html"></iframe></body></html>
    """
    title, batch, published_at, iframe_url = change_notice.parse_change_notice_article(
        article_html, ARTICLE_URL
    )
    assert batch == "404"
    assert "变更扩展公示" in title
    assert published_at == "2026-02-06 14:52"
    assert iframe_url == IFRAME_URL

    iframe_html = """
    <script queryData="{'parseType':'buildstatic','pageId':'page-404'}"
            url="/api-gateway/jpaas-publish-server/front/page/build/unit"></script>
    """
    source = change_notice.parse_change_notice_unit(
        iframe_html,
        notice_url=ARTICLE_URL,
        title=title,
        published_at=published_at,
        batch=batch,
        iframe_url=iframe_url,
    )
    assert source.unit_params["pageId"] == "page-404"
    assert source.unit_url.startswith("https://www.miit.gov.cn/api-gateway/")


def test_parse_change_notice_rows():
    rows, total, page_size = change_notice.parse_change_notice_rows(
        _result_html(model_code="ABC6500EV", total=31, page_size=20), IFRAME_URL
    )
    assert total == 31
    assert page_size == 20
    assert rows == [
        {
            "notice_title": "多用途乘用车",
            "notice_batch": "",
            "raw_notice_batch": "",
            "batch_or_chassis_id": "404",
            "company": "示例汽车有限公司",
            "trademark": "示例牌",
            "product_name": "多用途乘用车",
            "model_code": "ABC6500EV",
            "detail_url": "https://www.miit.gov.cn/datainfo/change404/art/demo.html",
        }
    ]


def test_query_change_notice_fetches_pages_and_honors_limit(monkeypatch):
    calls: list[int] = []

    def fake_fetch(source, *, search, page_num, page_size):
        assert source.batch == "404"
        assert json.loads(json.dumps(search))["CPXH"] == "ABC"
        calls.append(page_num)
        return _result_html(model_code=f"ABC{page_num}", total=3, page_size=1)

    monkeypatch.setattr(change_notice, "_fetch_unit_html", fake_fetch)
    rows, total = change_notice.query_change_notice(
        _source(), model_code="ABC", page_size=1, limit=2
    )
    assert total == 3
    assert [row["model_code"] for row in rows] == ["ABC1", "ABC2"]
    assert calls == [1, 2]


def test_latest_effective_rows_requires_exact_model_and_highest_batch(monkeypatch):
    monkeypatch.setattr(
        change_notice.core,
        "query_all_pages",
        lambda **kwargs: [
            {"clxh": "ABC6500EV", "gppc": "403"},
            {"clxh": "ABC6500EV", "gppc": "409"},
            {"clxh": "ABC6500EV-L", "gppc": "410"},
        ],
    )
    rows = change_notice.latest_effective_rows("abc6500ev")
    assert rows == [{"clxh": "ABC6500EV", "gppc": "409"}]


def test_new_product_notice_uses_headers_not_fixed_column_positions():
    html = '''<div class="page-content"><table>
      <tr><th>序号</th><th>企业名称</th><th>产品型号</th><th>产品名称</th><th>商标</th></tr>
      <tr><td>1</td><td>测试公司</td><td>ZZ7000BEV</td><td>纯电动轿车</td><td>测试牌</td></tr>
    </table></div>'''
    rows, total, _ = change_notice.parse_change_notice_rows(html, IFRAME_URL)
    assert total == 1
    assert rows[0]['model_code'] == 'ZZ7000BEV'
    assert rows[0]['product_name'] == '纯电动轿车'
    assert rows[0]['notice_batch'] == ''


def test_summary_row_does_not_discard_products():
    html = _result_html(model_code='ABC7000').replace(
        '</table>', '<tr><td colspan="6">合计：1 条</td></tr></table>')
    rows, total, _ = change_notice.parse_change_notice_rows(html, IFRAME_URL)
    assert total == len(rows) == 1
    assert rows[0]['model_code'] == 'ABC7000'


@pytest.mark.parametrize('html,message', [
    ('<div/>', '缺少结果表格'),
    ('<div class="page-content"><table></table></div>', '没有表头'),
    ('<div class="page-content"><table><tr><th>未知列</th></tr></table></div>', '表头第1行'),
    (_result_html(model_code='ABC7000').replace('<th>产品商标</th>', '<th>企业名称</th>'), '重复字段'),
    (_result_html(model_code=''), '第2行缺少精确型号'),
    (_result_html(model_code='ABC7000').replace('<td><div title="ABC7000">ABC7000</div></td>', ''),
     '第2行缺列'),
    (_result_html(model_code='ABC7000').replace('</table>',
     '<tr><td colspan="6">未知产品数据</td></tr></table>'), '第3行存在无法识别'),
])
def test_malformed_notice_fails_with_context(html, message):
    with pytest.raises(RuntimeError, match=message):
        change_notice.parse_change_notice_rows(html, IFRAME_URL)


def test_article_batch_preserves_raw_batch_and_chassis_column(monkeypatch):
    def fake_fetch(source, **kwargs):
        html = _result_html(model_code='ABC7000')
        html = html.replace('<div title="404">404</div>', '<div title="DP123">DP123</div>')
        return html.replace('<th>标题</th>', '<th>批次</th>').replace(
            '<div title="多用途乘用车"><a', '<div title="403"><a')
    monkeypatch.setattr(change_notice, '_fetch_unit_html', fake_fetch)
    rows, _ = change_notice.query_change_notice(_source())
    assert rows[0]['notice_batch'] == '404'
    assert rows[0]['raw_notice_batch'] == '403'
    assert rows[0]['batch_or_chassis_id'] == 'DP123'
