"""Pytest fixtures for dash_engine tests."""
import json
import pytest


@pytest.fixture
def minimal_config():
    """Minimal engine config: one metric, one derived, one header view, layout."""
    return {
        "datasources": {
            "prometheus": {
                "base_url": "http://localhost:9090",
                "timeout_s": 2.0,
            }
        },
        "globals": {
            "refresh": {"fast_s": 0.2, "bulk_s": 5.0},
            "vars": {"node_id": "node/test"},
            "defaults": {"missing_value": "---"},
        },
        "metrics": [
            {"id": "host_cpu_decimal", "query": 'pve_cpu_usage_ratio{id="${node_id}"}', "query_type": "instant"},
            {"id": "guest_info", "query": "pve_guest_info", "query_type": "instant", "expose_labels": ["id", "name", "type"]},
        ],
        "derived": [
            {"id": "host_cpu_pct", "expr": "100 * host_cpu_decimal"},
        ],
        "views": [
            {
                "id": "host_header",
                "type": "header",
                "title": "Host",
                "template": "CPU: ${host_cpu_pct|percent:1}",
                "computed_values": {},
            },
            {
                "id": "guest_table",
                "type": "table",
                "title": "Guests",
                "source": {
                    "rows_from": {"anchor_metric": "guest_info", "join_on_label": "id"},
                    "preferred_labels": {"name": "name"},
                    "sort": {},
                    "filter": {},
                },
                "columns": [
                    {"id": "name", "title": "Name", "value": "${name}", "format": "number"},
                ],
            },
        ],
        "layout": [{"view": "host_header"}, {"view": "guest_table"}],
    }


@pytest.fixture
def sample_prometheus_result():
    """Minimal Prometheus API response with _m labels (as returned after bulk OR query)."""
    return {
        "data": {
            "result": [
                {"metric": {"_m": "host_cpu_decimal", "id": "node/test"}, "value": [1700000000, "0.25"]},
                {"metric": {"_m": "guest_info", "id": "vm/100", "name": "vm1", "type": "qemu"}, "value": [1700000000, "1"]},
            ]
        }
    }
