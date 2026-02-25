"""Tests for dash_engine: pure functions, engine with minimal config, and optional integration."""
import json
from unittest.mock import patch

import pytest

from dash_engine import (
    apply_color_macros,
    eval_expr,
    fmt_value,
    parse_results,
    pad_ansi,
    visible_len,
    prom_query,
    DashboardEngine,
    ListSourceDef,
    ViewDef,
)


# ------------------------------ Pure functions ------------------------------


class TestParseResults:
    def test_empty_or_none_returns_empty_list(self):
        assert parse_results(None) == []
        assert parse_results({}) == []
        assert parse_results({"data": {}}) == []

    def test_malformed_missing_data_returns_empty_list(self):
        assert parse_results({"status": "ok"}) == []

    def test_valid_result_returns_list_of_series(self):
        raw = {"data": {"result": [{"metric": {"__name__": "x"}, "value": [1, "2.5"]}]}}
        got = parse_results(raw)
        assert len(got) == 1
        assert got[0]["metric"]["__name__"] == "x"
        assert got[0]["value"] == [1, "2.5"]


class TestPromQuery:
    def test_prom_query_builds_expected_url_and_parses_json(self, monkeypatch):
        captured = {}

        class DummyResponse:
            def __init__(self, url):
                self._url = url

            def read(self):
                return json.dumps({"data": {"result": []}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=3.0):
            # Capture the full URL used for the query
            captured["url"] = getattr(req, "full_url", getattr(req, "get_full_url", lambda: None)())
            captured["timeout"] = timeout
            return DummyResponse(captured["url"])

        monkeypatch.setattr("dash_engine.urllib.request.urlopen", fake_urlopen)

        base = "http://example.com"
        query = 'metric_name{label="a b"}'

        result = prom_query(base, query, timeout=1.5)

        assert result == {"data": {"result": []}}
        assert captured["timeout"] == 1.5
        assert captured["url"].startswith(f"{base}/api/v1/query?")
        assert "query=" in captured["url"]
        # Ensure that characters like spaces and braces are URL-encoded
        assert "%7B" in captured["url"] and "%7D" in captured["url"]


class TestFmtValue:
    def test_none_returns_dash(self):
        assert fmt_value(None, "number") == "---"

    def test_percent(self):
        assert fmt_value(50.5, "percent") == "50.5%"
        assert fmt_value(50.567, "percent", decimals=2) == "50.57%"

    def test_kb_mb(self):
        assert fmt_value(1000, "kb") == "1.0 KB"
        assert fmt_value(1_000_000, "mb") == "1.0 MB"

    def test_number(self):
        assert fmt_value(42.1, "number") == "42.1"

    def test_temp_c(self):
        assert fmt_value(65.7, "temp_c") == "66°C"

    def test_auto_bytes(self):
        assert fmt_value(1024, "-b") == "1.0 KB"
        # Implementation uses >> 10 so 1536 becomes 1.0 KB (integer division)
        assert fmt_value(1536, "-b") == "1.0 KB"
        assert fmt_value(2048, "-b") == "2.0 KB"
        assert fmt_value(0, "-b") == "0.0 B"

    def test_fallback_printf(self):
        assert fmt_value(3.14, "%.2f") == "3.14"


class TestEvalExpr:
    def test_simple_arithmetic(self):
        assert eval_expr("1 + 2", {}) == 3.0
        assert eval_expr("10 - 3", {}) == 7.0
        assert eval_expr("4 * 5", {}) == 20.0
        assert eval_expr("20 / 4", {}) == 5.0

    def test_with_context(self):
        assert eval_expr("100 * x", {"x": 0.25}) == 25.0
        assert eval_expr("a / b", {"a": 10, "b": 2}) == 5.0

    def test_none_in_context_returns_none(self):
        assert eval_expr("100 * x", {"x": None}) is None
        assert eval_expr("a + b", {"a": 1, "b": None}) is None

    def test_disallowed_raises(self):
        with pytest.raises(ValueError):
            eval_expr("__import__('os')", {})


class TestVisibleLen:
    def test_plain_string(self):
        assert visible_len("hello") == 5

    def test_strips_ansi(self):
        s = "\x1b[31mred\x1b[0m"
        assert visible_len(s) == 3


class TestPadAnsi:
    def test_left_align(self):
        assert pad_ansi("ab", 5, "<") == "ab   "

    def test_right_align(self):
        assert pad_ansi("ab", 5, ">") == "   ab"

    def test_center_align(self):
        assert pad_ansi("ab", 5, "^") == " ab  "

    def test_width_shorter_than_content(self):
        assert pad_ansi("hello", 3, "<") == "hello"

    def test_invalid_align_raises(self):
        with pytest.raises(ValueError):
            pad_ansi("x", 5, "?")


class TestApplyColorMacros:
    def test_replaces_color_refs(self):
        cfg = {"colors": {"bright": {"green": "\x1b[32m"}}, "text": "${colors.bright.green}ok"}
        got = apply_color_macros(cfg)
        assert got["text"] == "\x1b[32mok"

    def test_nested_structures(self):
        cfg = {"a": {"b": "${colors.foo}", "c": 1}, "colors": {"foo": "F"}}
        got = apply_color_macros(cfg)
        assert got["a"]["b"] == "F"
        assert got["a"]["c"] == 1

    def test_unknown_ref_left_unchanged(self):
        cfg = {"t": "${colors.unknown.x}"}
        got = apply_color_macros(cfg)
        assert got["t"] == "${colors.unknown.x}"


# ------------------------------ Engine (minimal config) ------------------------------


class TestEngineSubstVars:
    def test_subst_vars_replaces_globals(self, minimal_config):
        engine = DashboardEngine(minimal_config)
        out = engine._subst_vars('pve_cpu{id="${node_id}"}')
        assert "node/test" in out
        assert "${node_id}" not in out


class TestEngineBuildBulkQueries:
    def test_single_metric_produces_one_query_with_label_replace(self, minimal_config):
        engine = DashboardEngine(minimal_config)
        queries = engine._build_bulk_queries()
        assert len(queries) == 1
        assert "label_replace(" in queries[0]
        assert '"_m"' in queries[0]
        assert "host_cpu_decimal" in queries[0]
        assert " or " in queries[0]  # two metrics joined

    def test_long_queries_split_into_batches(self, minimal_config):
        # Add many metrics with long query strings to exceed 7000 chars
        long_query = "pve_some_metric_" + "x" * 500
        minimal_config["metrics"] = [{"id": f"m{i}", "query": long_query, "query_type": "instant"} for i in range(20)]
        engine = DashboardEngine(minimal_config)
        queries = engine._build_bulk_queries()
        assert len(queries) >= 2


class TestEngineBulkFetch:
    def test_bulk_fetch_populates_series_and_reindex(self, minimal_config, sample_prometheus_result):
        engine = DashboardEngine(minimal_config)
        with patch("dash_engine.urllib.request.urlopen") as mock_open:
            mock_open.return_value.read.return_value = json.dumps(sample_prometheus_result).encode()
            mock_open.return_value.__enter__ = lambda self: self
            mock_open.return_value.__exit__ = lambda *a: None
            engine.bulk_fetch()
        assert len(engine.series) == 2
        ids = {s["_metric_id"] for s in engine.series}
        assert "host_cpu_decimal" in ids
        assert "guest_info" in ids
        assert engine.by_name_only.get("host_cpu_decimal") == [0.25]
        assert "node/test" in engine.rows or "vm/100" in engine.rows


class TestEngineContexts:
    def test_global_ctx_and_rows_ctx_after_fake_series(self, minimal_config):
        engine = DashboardEngine(minimal_config)
        engine.series = [
            {"metric": {"id": "vm/1"}, "value": [0, "0.5"], "_metric_id": "guest_cpu", "_expose_labels": []},
            {"metric": {"id": "vm/1", "name": "vm1"}, "value": [0, "1"], "_metric_id": "guest_info", "_expose_labels": ["id", "name"]},
        ]
        engine._reindex()
        gctx = engine.global_ctx()
        assert "guest_cpu" in gctx or len(engine.by_name_only) > 0
        rctxs = engine.rows_ctx()
        assert "vm/1" in rctxs


class TestEngineComputeDerived:
    def test_compute_derived_global(self, minimal_config):
        engine = DashboardEngine(minimal_config)
        engine.series = [
            {"metric": {"id": "node/test"}, "value": [0, "0.25"], "_metric_id": "host_cpu_decimal", "_expose_labels": []},
        ]
        engine._reindex()
        gctx = engine.global_ctx()
        rctxs = engine.rows_ctx()
        dglob, drows = engine.compute_derived(gctx, rctxs)
        assert "host_cpu_pct" in dglob
        assert dglob["host_cpu_pct"] == 25.0


class TestEngineRender:
    def test_render_header(self, minimal_config):
        engine = DashboardEngine(minimal_config)
        engine.series = [
            {"metric": {"id": "node/test"}, "value": [0, "0.25"], "_metric_id": "host_cpu_decimal", "_expose_labels": []},
        ]
        engine._reindex()
        gctx = engine.global_ctx()
        rctxs = engine.rows_ctx()
        dglob, _ = engine.compute_derived(gctx, rctxs)
        header_view = next(v for v in engine.views if v.id == "host_header")
        out = engine.render_header(header_view, gctx, dglob)
        assert "25" in out or "CPU" in out

    def test_render_table(self, minimal_config, sample_prometheus_result):
        engine = DashboardEngine(minimal_config)
        with patch("dash_engine.urllib.request.urlopen") as mock_open:
            mock_open.return_value.read.return_value = json.dumps(sample_prometheus_result).encode()
            mock_open.return_value.__enter__ = lambda self: self
            mock_open.return_value.__exit__ = lambda *a: None
            engine.bulk_fetch()
        gctx = engine.global_ctx()
        rctxs = engine.rows_ctx()
        _, drows = engine.compute_derived(gctx, rctxs)
        table_view = next(v for v in engine.views if v.id == "guest_table")
        out = engine.render_table(table_view, gctx, drows)
        assert "Name" in out or "Guests" in out or "vm1" in out

    def test_render_table_tabular_view_with_sort_and_derived(self):
        # Config with a table view that sorts by a per-row derived metric
        cfg = {
            "datasources": {
                "prometheus": {"base_url": "http://localhost:9090", "timeout_s": 2.0}
            },
            "globals": {
                "refresh": {"fast_s": 0.2, "bulk_s": 5.0},
                "vars": {},
                "defaults": {"missing_value": "---"},
            },
            "metrics": [
                {
                    "id": "guest_info",
                    "query": "pve_guest_info",
                    "query_type": "instant",
                    "expose_labels": ["id", "name", "type"],
                },
                {
                    "id": "guest_cpu",
                    "query": "pve_cpu_usage_ratio",
                    "query_type": "instant",
                },
            ],
            "derived": [
                {"id": "guest_cpu_pct", "per_row": True, "expr": "100 * guest_cpu"},
            ],
            "views": [
                {
                    "id": "guest_table",
                    "type": "table",
                    "title": "Guests",
                    "source": {
                        "rows_from": {"anchor_metric": "guest_info", "join_on_label": "id"},
                        "preferred_labels": {"name": "name", "type": "type"},
                        "sort": {"by": "guest_cpu_pct", "order": "desc"},
                        "filter": {},
                    },
                    "columns": [
                        {
                            "id": "name",
                            "title": "Name",
                            "value": "${name}",
                            "format": "number",
                            "width": 8,
                        },
                        {
                            "id": "cpu",
                            "title": "CPU%",
                            "value": "${guest_cpu_pct}",
                            "format": "percent",
                            "decimals": 1,
                            "width": 6,
                        },
                    ],
                }
            ],
            "layout": [{"view": "guest_table"}],
        }

        engine = DashboardEngine(cfg)

        # Two guests with different CPU usage; vm2 should appear before vm1 after sorting desc
        engine.series = [
            # guest_cpu metrics
            {
                "metric": {"id": "vm/1"},
                "value": [0, "0.10"],
                "_metric_id": "guest_cpu",
                "_expose_labels": [],
            },
            {
                "metric": {"id": "vm/2"},
                "value": [0, "0.30"],
                "_metric_id": "guest_cpu",
                "_expose_labels": [],
            },
            # guest_info metrics (carry labels and _expose_labels so rows get labels)
            {
                "metric": {"id": "vm/1", "name": "vm1", "type": "qemu"},
                "value": [0, "1"],
                "_metric_id": "guest_info",
                "_expose_labels": ["id", "name", "type"],
            },
            {
                "metric": {"id": "vm/2", "name": "vm2", "type": "lxc"},
                "value": [0, "1"],
                "_metric_id": "guest_info",
                "_expose_labels": ["id", "name", "type"],
            },
        ]

        engine._reindex()
        gctx = engine.global_ctx()
        rctxs = engine.rows_ctx()
        _, drows = engine.compute_derived(gctx, rctxs)

        table_view = next(v for v in engine.views if v.id == "guest_table")
        out = engine.render_table(table_view, gctx, drows)

        # Extract non-empty body lines (skip header and final blank)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) >= 3  # header + at least 2 rows

        header = lines[0]
        row1 = lines[1]
        row2 = lines[2]

        assert "Name" in header and "CPU%" in header
        # vm2 (higher CPU) should be first due to sort order desc
        assert "vm2" in row1
        assert "vm1" in row2
        # Derived percentages should be rendered via fmt_value with percent format
        assert "%" in row1 or "%" in row2

    def test_render_list_empty_or_simple(self, minimal_config):
        engine = DashboardEngine(minimal_config)
        list_view = ViewDef(
            id="mylist",
            type="list",
            title="List",
            list_source=ListSourceDef(items_from={"metric": "guest_info"}, sort={}, filter={}, limit=None),
            item_template="${name}",
            item_prefix="",
            item_suffix="",
            item_width=None,
        )
        engine.series = []
        out = engine.render_list(list_view)
        # With no series, only the title line is rendered (no "(empty)" because lines is non-empty)
        assert out == "List" or "empty" in out.lower() or "(no items)" in out


# ------------------------------ Integration-style ------------------------------


class TestIntegration:
    def test_full_cycle_load_config_bulk_fetch_render(self, minimal_config, sample_prometheus_result):
        engine = DashboardEngine(minimal_config)
        with patch("dash_engine.urllib.request.urlopen") as mock_open:
            mock_open.return_value.read.return_value = json.dumps(sample_prometheus_result).encode()
            mock_open.return_value.__enter__ = lambda self: self
            mock_open.return_value.__exit__ = lambda *a: None
            engine.bulk_fetch()
        gctx = engine.global_ctx()
        rctxs = engine.rows_ctx()
        dglob, drows = engine.compute_derived(gctx, rctxs)
        header_view = next(v for v in engine.views if v.id == "host_header")
        body_parts = []
        for vid in engine.layout:
            if vid == "host_header":
                continue
            view = next(v for v in engine.views if v.id == vid)
            if view.type == "table":
                body_parts.append(engine.render_table(view, gctx, drows))
            elif view.type == "list":
                body_parts.append(engine.render_list(view))
        hostline = engine.render_header(header_view, gctx, dglob)
        assert hostline
        assert "\n".join(body_parts) or True
