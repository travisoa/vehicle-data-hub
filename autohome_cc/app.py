"""命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from autohome_cc.config import BROWSER_PROFILE_DIR, BROWSER_TIMEOUT_MS, DEFAULT_OUTPUT_PREFIX, LOG_DIR, OUTPUT_DIR
from autohome_cc.crawler.browser_client import BrowserAutohomeClient, BrowserUnavailableError
from autohome_cc.crawler.http_client import HttpClient, LoginRequiredError
from autohome_cc.crawler.search import AutohomeSearcher, SearchCandidate
from autohome_cc.exporter.excel_writer import export_to_excel
from autohome_cc.parser.compare_parser import (
    build_dataset_from_summary,
    load_cookie_mapping,
    merge_compare_datasets,
    parse_capture_file,
    parse_capture_text,
    parse_param_conf_api_data,
)
from autohome_cc.parser.spec_parser import parse_autohome_page, parse_series_config_page
from autohome_cc.utils.cleaners import (
    build_export_filename,
    ensure_unique_path,
    should_expand_all_trims,
    split_model_names,
)
from autohome_cc.utils.logger import RunLogger


class AutohomeScraperApp:
    """项目主流程。"""

    PARAM_CONF_API_URL = "https://www.autohome.com.cn/web-main/car/param/getParamConf"

    def __init__(
        self,
        *,
        cookie_string: str = "",
        cookie_file: str = "",
        browser_fallback: bool = False,
        browser_login: bool = False,
        browser_profile_dir: str = "",
        browser_channel: str = "",
    ) -> None:
        self.run_logger = RunLogger(LOG_DIR)
        cookies = load_cookie_mapping(cookie_string=cookie_string, cookie_file=cookie_file)
        self.http_client = HttpClient(self.run_logger, cookies=cookies)
        self.searcher = AutohomeSearcher(self.http_client, self.run_logger)
        self.browser_client: BrowserAutohomeClient | None = None
        # 最近一次 run() 中解析失败的输入，供 CLI 透出非零退出码
        self.last_failed_inputs: list[str] = []

        if browser_fallback or browser_login:
            profile_dir = Path(browser_profile_dir).expanduser() if browser_profile_dir else BROWSER_PROFILE_DIR
            self.browser_client = BrowserAutohomeClient(
                self.run_logger,
                user_data_dir=profile_dir,
                channel=browser_channel,
                timeout_ms=BROWSER_TIMEOUT_MS,
            )
            if browser_login:
                browser_cookies = self.browser_client.interactive_login()
                self.http_client.set_cookies(browser_cookies)

    def run(
        self,
        *,
        model_names: list[str],
        capture_files: list[str],
        output_dir: Path | None = None,
    ) -> Path:
        datasets = []
        failed_inputs: list[str] = []

        if capture_files:
            datasets, failed_inputs = self._run_capture_mode(capture_files, model_names)
        else:
            datasets, failed_inputs = self._run_online_mode(model_names)

        self.last_failed_inputs = failed_inputs
        if not datasets:
            raise SystemExit("没有成功解析到任何车型数据，请检查抓包文件或登录态。")

        merged_dataset = datasets[0] if len(datasets) == 1 else merge_compare_datasets(datasets)
        output_dir = output_dir or OUTPUT_DIR
        # 只用成功导出的车型命名，失败输入不进文件名
        exported_names = [name for name in model_names if name not in failed_inputs]
        output_path = ensure_unique_path(
            output_dir / build_export_filename(exported_names, prefix=DEFAULT_OUTPUT_PREFIX)
        )

        metadata = {
            "运行模式": "抓包文件回放" if capture_files else "在线抓取",
            "输入车型": ", ".join(model_names) if model_names else "(抓包文件内全部车型)",
            "抓包文件": ", ".join(capture_files) if capture_files else "-",
            "车型数量": len(merged_dataset.models),
            "参数行数": len(merged_dataset.scalar_rows),
            "颜色明细行数": len(merged_dataset.color_rows),
            "失败输入": ", ".join(failed_inputs) if failed_inputs else "-",
            "备注": " | ".join(merged_dataset.notes) if merged_dataset.notes else "-",
        }

        model_rows = [
            {
                "spec_id": model.spec_id,
                "车型": model.display_name or model.name,
                "厂商指导价": model.guide_price,
                "经销商报价": model.dealer_price,
                "来源": model.source,
            }
            for model in merged_dataset.models
        ]
        color_rows = [
            {
                "类别": row.get("category", ""),
                "spec_id": row.get("spec_id", ""),
                "车型": row.get("model", ""),
                "内容": row.get("content", ""),
            }
            for row in merged_dataset.color_rows
        ]
        source_urls = [
            str(row.get("autohome_url", "")) for row in merged_dataset.raw_rows if row.get("autohome_url")
        ]
        if source_urls:
            metadata["数据来源"] = " | ".join(dict.fromkeys(source_urls))

        export_to_excel(
            metadata=metadata,
            model_rows=model_rows,
            scalar_rows=merged_dataset.scalar_rows,
            color_rows=color_rows,
            output_path=output_path,
        )

        print(f"Successful models: {len(merged_dataset.models)}")
        print(f"Failed inputs: {len(failed_inputs)}")
        print(f"Output file path: {output_path}")
        if failed_inputs:
            print("Failed inputs: " + ", ".join(failed_inputs))
        if merged_dataset.notes:
            print("Notes: " + " | ".join(merged_dataset.notes))

        return output_path

    def _run_capture_mode(
        self,
        capture_files: list[str],
        model_names: list[str],
    ) -> tuple[list, list[str]]:
        datasets = []
        failed_inputs: list[str] = []
        requested_models = model_names or None
        for capture_file in capture_files:
            self.run_logger.info("开始解析抓包文件", model=capture_file, stage="capture")
            try:
                dataset = parse_capture_file(capture_file, requested_models=requested_models)
            except Exception as exc:  # noqa: BLE001
                failed_inputs.append(capture_file)
                self.run_logger.error(
                    "抓包文件解析失败",
                    model=capture_file,
                    stage="capture",
                    error=str(exc),
                )
                continue
            datasets.append(dataset)
            self.run_logger.info("抓包文件解析完成", model=capture_file, stage="capture_done")
        return datasets, failed_inputs

    def _run_online_mode(self, model_names: list[str]) -> tuple[list, list[str]]:
        datasets = []
        failed_inputs: list[str] = []
        for model_name in model_names:
            self.run_logger.info("开始处理车型", model=model_name, stage="start")
            try:
                dataset = self._process_single_model(model_name)
            except LoginRequiredError as exc:
                failed_inputs.append(model_name)
                self.run_logger.error(
                    "需要用户协助登录",
                    model=model_name,
                    stage="auth",
                    error=str(exc),
                )
                continue
            except Exception as exc:  # noqa: BLE001
                failed_inputs.append(model_name)
                self.run_logger.error(
                    "车型处理失败",
                    model=model_name,
                    stage="model",
                    error=str(exc),
                )
                continue

            datasets.append(dataset)
            self.run_logger.info("车型处理完成", model=model_name, stage="done")
        return datasets, failed_inputs

    def _process_single_model(self, model_name: str):
        candidates = self._search_candidates(model_name)
        if not candidates:
            raise ValueError("未找到候选搜索结果")

        last_error = None
        for candidate in candidates[:3]:
            try:
                return self._process_candidate(model_name, candidate)
            except LoginRequiredError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.run_logger.warning(
                    "候选页面处理失败，尝试下一个结果",
                    model=model_name,
                    stage="candidate",
                    url=candidate.url,
                    error=str(exc),
                )

        if last_error:
            raise last_error
        raise ValueError("未抓到有效结果")

    def _process_candidate(self, model_name: str, candidate: SearchCandidate):
        requested_models = None if should_expand_all_trims(model_name) else [model_name]

        api_dataset = self._try_param_conf_api(model_name, candidate)
        if api_dataset is not None:
            if api_dataset.raw_rows:
                api_dataset.raw_rows[0].update(
                    {
                        "input_name": model_name,
                        "matched_title": candidate.title,
                        "search_query": candidate.query,
                        "search_rank": candidate.rank,
                        "search_snippet": candidate.snippet,
                        "autohome_url": candidate.url,
                    }
                )
            return api_dataset

        fetch_result = self._fetch_candidate_page(candidate)
        target_url = fetch_result.final_url
        target_html = fetch_result.text

        try:
            dataset = parse_capture_text(
                target_html,
                source_label=target_url,
                requested_models=requested_models,
                source_type="online_series_page",
            )
        except Exception as capture_exc:  # noqa: BLE001
            self.run_logger.warning(
                "完整配置页解析失败，回退到 summary 解析",
                model=model_name,
                stage="compare_parse",
                url=target_url,
                error=str(capture_exc),
            )
            dataset = self._build_summary_fallback_dataset(model_name, candidate, target_url, target_html)

        if dataset.raw_rows:
            dataset.raw_rows[0].update(
                {
                    "input_name": model_name,
                    "matched_title": candidate.title,
                    "search_query": candidate.query,
                    "search_rank": candidate.rank,
                    "search_snippet": candidate.snippet,
                    "autohome_url": target_url,
                }
            )
        return dataset

    def _try_param_conf_api(self, model_name: str, candidate: SearchCandidate):
        series_id = candidate.series_id.strip()
        if not series_id:
            return None
        requested_models = None if should_expand_all_trims(model_name) else [model_name]

        try:
            response = self.http_client.get_response(
                self.PARAM_CONF_API_URL,
                params={"mode": "1", "site": "1", "seriesid": series_id},
                referer=candidate.url,
            )
            payload = response.json()
            return parse_param_conf_api_data(
                payload,
                source_label=str(response.url),
                requested_models=requested_models,
                source_type="online_param_conf_api",
            )
        except Exception as exc:  # noqa: BLE001
            self.run_logger.warning(
                "在线参数接口解析失败，回退到页面抓取",
                model=model_name,
                stage="param_conf_api",
                url=candidate.url,
                error=str(exc),
            )
            return None

    def _build_summary_fallback_dataset(
        self,
        model_name: str,
        candidate: SearchCandidate,
        target_url: str,
        target_html: str,
    ):
        try:
            summary, raw = parse_series_config_page(
                model_name,
                target_url,
                target_html,
                search_title=candidate.title,
                search_snippet=candidate.snippet,
                brand_name=candidate.brand,
            )
        except Exception as exc:  # noqa: BLE001
            self.run_logger.warning(
                "车系配置 JSON 解析失败，回退到页面文本解析",
                model=model_name,
                stage="series_parse",
                url=target_url,
                error=str(exc),
            )
            summary, raw = parse_autohome_page(
                model_name,
                target_url,
                target_html,
                search_title=candidate.title,
                search_snippet=candidate.snippet,
            )
        raw["search_query"] = candidate.query
        raw["search_rank"] = candidate.rank
        return build_dataset_from_summary(
            summary,
            raw,
            source_label=target_url,
            source_type="summary_fallback",
        )

    def _search_candidates(self, model_name: str) -> list[SearchCandidate]:
        try:
            return self.searcher.search(model_name)
        except Exception as exc:  # noqa: BLE001
            self.run_logger.warning(
                "Autohome 搜索接口失败",
                model=model_name,
                stage="search",
                error=str(exc),
            )
            if not self.browser_client:
                raise
            self.run_logger.info(
                "尝试使用浏览器渲染搜索结果页",
                model=model_name,
                stage="browser_search",
            )
            return self.browser_client.search(model_name)

    def _fetch_candidate_page(self, candidate: SearchCandidate):
        try:
            result = self.http_client.get_text(candidate.url)
            if self.browser_client and "hs_kw" in result.text:
                self.run_logger.info(
                    "检测到页面字段混淆，尝试浏览器还原后抓取",
                    model=candidate.model_name,
                    stage="browser_deobfuscate",
                    url=candidate.url,
                )
                browser_result = self.browser_client.fetch_text(candidate.url)
                self.http_client.set_cookies(browser_result.cookies)
                return browser_result
            return result
        except LoginRequiredError:
            if not self.browser_client:
                raise
            self.run_logger.info(
                "请求命中登录/验证页，尝试浏览器抓取",
                model=candidate.model_name,
                stage="browser_fetch_auth",
                url=candidate.url,
            )
            result = self.browser_client.fetch_text(candidate.url, allow_manual_login=True)
            self.http_client.set_cookies(result.cookies)
            return result
        except Exception as exc:  # noqa: BLE001
            if not self.browser_client:
                raise
            self.run_logger.warning(
                "请求抓取失败，尝试浏览器抓取",
                model=candidate.model_name,
                stage="browser_fetch",
                url=candidate.url,
                error=str(exc),
            )
            result = self.browser_client.fetch_text(candidate.url)
            self.http_client.set_cookies(result.cookies)
            return result


def build_argument_parser() -> argparse.ArgumentParser:
    """构建命令行参数。"""
    parser = argparse.ArgumentParser(description="Autohome Config Compare CLI")
    parser.add_argument("--models", help="逗号分隔的车型名称；在线模式必填，抓包模式可选做车型筛选")
    parser.add_argument("--capture-file", action="append", default=[], help="抓包原文件路径，可重复传入")
    parser.add_argument("--cookies", help="直接传入 Cookie header 字符串")
    parser.add_argument("--cookie-file", help="Cookie 文件路径，支持 header 风格或 Netscape 格式")
    parser.add_argument("--browser-fallback", action="store_true", help="requests/search API 失败时，允许调用浏览器渲染页面抓取")
    parser.add_argument("--browser-login", action="store_true", help="抓取前先打开浏览器进行扫码登录，并复用登录态")
    parser.add_argument("--browser-profile-dir", help="浏览器登录态目录，默认 .browser/autohome")
    parser.add_argument("--browser-channel", help="Playwright 浏览器 channel，例如 chrome 或 msedge")
    parser.add_argument("--output-dir", help="Excel 输出目录，默认 output/")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    raw_models = args.models
    if not raw_models and not args.capture_file:
        raw_models = input("请输入一个或多个车型名称（逗号分隔）:\n").strip()

    model_names = split_model_names(raw_models or "")
    if not model_names and not args.capture_file:
        raise SystemExit("未提供有效车型名称。")

    try:
        app = AutohomeScraperApp(
            cookie_string=args.cookies or "",
            cookie_file=args.cookie_file or "",
            browser_fallback=args.browser_fallback,
            browser_login=args.browser_login,
            browser_profile_dir=args.browser_profile_dir or "",
            browser_channel=args.browser_channel or "",
        )
    except BrowserUnavailableError as exc:
        raise SystemExit(str(exc)) from exc
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    app.run(
        model_names=model_names,
        capture_files=args.capture_file,
        output_dir=output_dir,
    )
    # 与 main.py fetch 保持一致：部分输入失败也要透出非零退出码，便于脚本化调用判断
    return 1 if app.last_failed_inputs else 0


if __name__ == "__main__":
    raise SystemExit(main())
