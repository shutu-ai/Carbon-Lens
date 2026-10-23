from __future__ import annotations

from carbon_lens.config import CarbonLensError
from carbon_lens.service import CarbonLensService


def test_health_check_runs_a_fresh_probe_and_returns_metadata(monkeypatch):
    service = CarbonLensService()

    def successful_query(connection, sql, limit, timeout_seconds):
        assert connection == "carbon-prod"
        assert sql == "SELECT 1 AS carbon_lens_health"
        assert limit == 1
        assert timeout_seconds == 60
        return {
            "transport": "ssh-beeline",
            "returned_row_count": 1,
            "truncated": False,
            "connection_attempts": 2,
        }

    monkeypatch.setattr(service, "query", successful_query)

    result = service.health_check("carbon-prod")

    assert result["ok"] is True
    assert result["phase"] == "result"
    assert result["transport"] == "ssh-beeline"
    assert result["connection_attempts"] == 2
    assert result["mcp_instance_id"].startswith("pid-")


def test_health_check_classifies_fresh_ssh_timeout(monkeypatch):
    service = CarbonLensService()

    def failed_query(connection, sql, limit, timeout_seconds):
        raise CarbonLensError("SSH connection failed: 10.0.0.1: timed out")

    monkeypatch.setattr(service, "query", failed_query)

    result = service.health_check("carbon-prod", timeout_seconds=30)

    assert result["ok"] is False
    assert result["phase"] == "ssh_connect"
    assert result["error_code"] == "ssh_connect_timeout"
    assert result["retryable"] is True
    assert result["timeout_seconds"] == 30
