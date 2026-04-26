from scripts import vps_preflight


def _make_project_layout(tmp_path):
    (tmp_path / ".env").write_text(
        "BINANCE_API_KEY=test_key\nBINANCE_API_SECRET=test_secret\n",
        encoding="utf-8",
    )
    (tmp_path / "bot_config.yaml").write_text("monitoring: {}\n", encoding="utf-8")
    (tmp_path / "crypto_bot.db").write_text("", encoding="utf-8")


def test_run_preflight_fails_for_auth_degraded_bot_without_override(tmp_path, monkeypatch):
    _make_project_layout(tmp_path)

    responses = {
        "http://127.0.0.1:8080/health": {
            "healthy": True,
            "status": "degraded",
            "auth_degraded": {"active": True, "reason": "IP not allowed"},
        },
    }

    monkeypatch.setattr(vps_preflight, "_http_json", lambda url, timeout: responses[url])

    result = vps_preflight.run_preflight(
        project_root=tmp_path,
        bot_health_url="http://127.0.0.1:8080/health",
        timeout=5.0,
        allow_auth_degraded=False,
        skip_http=False,
    )

    assert result["status"] == "fail"
    bot_health_check = next(item for item in result["checks"] if item["name"] == "Bot health endpoint reachable")
    assert bot_health_check["ok"] is False
    assert "not allowed in strict preflight" in bot_health_check["detail"]


def test_run_preflight_allows_auth_degraded_bot_with_override(tmp_path, monkeypatch):
    _make_project_layout(tmp_path)

    responses = {
        "http://127.0.0.1:8080/health": {
            "healthy": True,
            "status": "degraded",
            "auth_degraded": {"active": True, "reason": "IP not allowed"},
        },
    }

    monkeypatch.setattr(vps_preflight, "_http_json", lambda url, timeout: responses[url])

    result = vps_preflight.run_preflight(
        project_root=tmp_path,
        bot_health_url="http://127.0.0.1:8080/health",
        timeout=5.0,
        allow_auth_degraded=True,
        skip_http=False,
    )

    assert result["status"] == "pass"
    bot_health_check = next(item for item in result["checks"] if item["name"] == "Bot health endpoint reachable")
    assert bot_health_check["ok"] is True
    assert "explicitly allowed" in bot_health_check["detail"]