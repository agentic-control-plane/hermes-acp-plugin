"""Unit tests for the hermes-acp CLI."""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from acp_hermes import cli


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ACP_API_BASE", raising=False)
    monkeypatch.delenv("ACP_DASHBOARD_BASE", raising=False)
    return tmp_path


def _login_args(force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(cmd="login", force=force, func=cli.cmd_login)


def test_login_success_writes_credentials(tmp_path):
    provision_body = json.dumps(
        {"apiKey": "acp_live_key", "workspace": "ws-1", "isNew": True}
    ).encode()

    def _urlopen(req, timeout):  # noqa: ARG001
        if req.full_url.endswith("/plugin/provision"):
            return FakeResponse(provision_body)
        if req.full_url.endswith("/govern/health"):
            return FakeResponse(b"", status=200)
        raise AssertionError(f"unexpected url {req.full_url}")

    with patch("urllib.request.urlopen", _urlopen), patch(
        "webbrowser.open", return_value=True
    ), patch("builtins.input", return_value="paste_token"):
        rc = cli.cmd_login(_login_args())
    assert rc == 0
    creds = tmp_path / ".acp" / "credentials"
    assert creds.exists()
    assert creds.read_text().strip() == "acp_live_key"
    # Mode should be 0600
    assert oct(creds.stat().st_mode)[-3:] == "600"


def test_login_aborts_when_token_empty(tmp_path):
    with patch("webbrowser.open"), patch("builtins.input", return_value=""):
        rc = cli.cmd_login(_login_args())
    assert rc == 1
    assert not (tmp_path / ".acp" / "credentials").exists()


def test_login_existing_credentials_short_circuits(tmp_path):
    (tmp_path / ".acp").mkdir()
    (tmp_path / ".acp" / "credentials").write_text("existing")
    rc = cli.cmd_login(_login_args(force=False))
    assert rc == 0
    # Existing token preserved
    assert (tmp_path / ".acp" / "credentials").read_text() == "existing"


def test_login_force_overwrites(tmp_path):
    (tmp_path / ".acp").mkdir()
    (tmp_path / ".acp" / "credentials").write_text("stale")
    provision_body = json.dumps(
        {"apiKey": "fresh_key", "workspace": "ws", "isNew": False}
    ).encode()

    def _urlopen(req, timeout):  # noqa: ARG001
        return FakeResponse(provision_body if "provision" in req.full_url else b"")

    with patch("urllib.request.urlopen", _urlopen), patch(
        "webbrowser.open"
    ), patch("builtins.input", return_value="tok"):
        rc = cli.cmd_login(_login_args(force=True))
    assert rc == 0
    assert (tmp_path / ".acp" / "credentials").read_text().strip() == "fresh_key"


def test_login_provision_http_error_returns_1(tmp_path):
    def _urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with patch("urllib.request.urlopen", _urlopen), patch("webbrowser.open"), patch(
        "builtins.input", return_value="bad_tok"
    ):
        rc = cli.cmd_login(_login_args())
    assert rc == 1


def test_login_malformed_response_returns_1(tmp_path):
    def _urlopen(req, timeout):  # noqa: ARG001
        return FakeResponse(b'{"foo": "bar"}')  # missing apiKey/workspace

    with patch("urllib.request.urlopen", _urlopen), patch("webbrowser.open"), patch(
        "builtins.input", return_value="tok"
    ):
        rc = cli.cmd_login(_login_args())
    assert rc == 1


def test_logout_removes_credentials(tmp_path):
    (tmp_path / ".acp").mkdir()
    (tmp_path / ".acp" / "credentials").write_text("k")
    rc = cli.cmd_logout(argparse.Namespace())
    assert rc == 0
    assert not (tmp_path / ".acp" / "credentials").exists()


def test_logout_when_already_logged_out(tmp_path):
    rc = cli.cmd_logout(argparse.Namespace())
    assert rc == 0


def test_status_missing_credentials(tmp_path):
    rc = cli.cmd_status(argparse.Namespace())
    assert rc == 1


def test_status_credentials_present_and_healthy(tmp_path):
    (tmp_path / ".acp").mkdir()
    (tmp_path / ".acp" / "credentials").write_text("k")
    with patch("urllib.request.urlopen", lambda req, timeout: FakeResponse(b"", 200)):
        rc = cli.cmd_status(argparse.Namespace())
    assert rc == 0


def test_status_credentials_present_gateway_down(tmp_path):
    (tmp_path / ".acp").mkdir()
    (tmp_path / ".acp" / "credentials").write_text("k")

    def _down(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("down")

    with patch("urllib.request.urlopen", _down):
        rc = cli.cmd_status(argparse.Namespace())
    assert rc == 2


# ── coverage ─────────────────────────────────────────────────────────


def _cov_args(as_json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(cmd="coverage", json=as_json, func=cli.cmd_coverage)


def _write_key(tmp_path, key: str = "gsk_myws_abc123") -> None:
    (tmp_path / ".acp").mkdir(exist_ok=True)
    (tmp_path / ".acp" / "credentials").write_text(key + "\n")


COVERAGE_BODY = json.dumps(
    {
        "ok": True,
        "windowDays": 30,
        "clients": [
            {
                "client": "hermes-plugin",
                "family": "hook-harness",
                "interception": {"active": True, "calls": 42},
                "proxy": {"active": False, "calls": 0},
                "fix": {
                    "missing": "proxy",
                    "run": "Run `acp-hermes proxy-setup --verify` ...",
                    "docs": "https://agenticcontrolplane.com/docs/setup#i-run-a-hermes-agent",
                },
            }
        ],
        "workspace": {"interception": True, "proxy": False},
        "setupUrl": "https://agenticcontrolplane.com/docs/setup",
    }
).encode()


def test_coverage_missing_credentials(tmp_path):
    assert cli.cmd_coverage(_cov_args()) == 1


def test_coverage_non_gsk_key(tmp_path):
    _write_key(tmp_path, "not-a-workspace-key")
    assert cli.cmd_coverage(_cov_args()) == 1


def test_coverage_prints_planes_and_fix(tmp_path, capsys):
    _write_key(tmp_path)
    seen = {}

    def _urlopen(req, timeout):  # noqa: ARG001
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResponse(COVERAGE_BODY)

    with patch("urllib.request.urlopen", _urlopen):
        rc = cli.cmd_coverage(_cov_args())
    out = capsys.readouterr().out
    assert rc == 0
    # slug parsed from gsk_{slug}_..., key sent as bearer
    assert "/myws/admin/coverage" in seen["url"]
    assert seen["auth"] == "Bearer gsk_myws_abc123"
    assert "interception ✓" in out
    assert "proxy ✗" in out
    assert "proxy-setup --verify" in out
    assert "docs/setup" in out


def test_coverage_json_passthrough(tmp_path, capsys):
    _write_key(tmp_path)
    with patch("urllib.request.urlopen", lambda req, timeout: FakeResponse(COVERAGE_BODY)):
        rc = cli.cmd_coverage(_cov_args(as_json=True))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["workspace"] == {"interception": True, "proxy": False}


def test_coverage_http_error(tmp_path):
    _write_key(tmp_path)

    def _fail(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", None, io.BytesIO(b""))

    with patch("urllib.request.urlopen", _fail):
        assert cli.cmd_coverage(_cov_args()) == 1


def test_coverage_empty_workspace_points_at_setup(tmp_path, capsys):
    _write_key(tmp_path)
    body = json.dumps(
        {"ok": True, "windowDays": 30, "clients": [], "workspace": {"interception": False, "proxy": False},
         "setupUrl": "https://agenticcontrolplane.com/docs/setup"}
    ).encode()
    with patch("urllib.request.urlopen", lambda req, timeout: FakeResponse(body)):
        rc = cli.cmd_coverage(_cov_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "docs/setup" in out


# ── device login ─────────────────────────────────────────────────────


def _device_args(force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(cmd="login", force=force, device=True, func=cli.cmd_login)


def test_device_login_polls_until_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    grant = json.dumps({
        "device_code": "d" * 64, "user_code": "ACDE-FGHJ",
        "verification_uri_complete": "https://cloud.example/device?code=ACDE-FGHJ",
        "expires_in": 900, "interval": 5,
    }).encode()
    token_ok = json.dumps({"apiKey": "gsk_ws_key", "workspace": "ws", "isNew": True}).encode()
    calls = {"n": 0}

    def _urlopen(req, timeout):  # noqa: ARG001
        if req.full_url.endswith("/device/code"):
            return FakeResponse(grant)
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", None,
                io.BytesIO(json.dumps({"error": "authorization_pending"}).encode()),
            )
        return FakeResponse(token_ok)

    with patch("urllib.request.urlopen", _urlopen):
        rc = cli.cmd_login(_device_args())
    assert rc == 0
    assert (tmp_path / ".acp" / "credentials").read_text().strip() == "gsk_ws_key"
    assert calls["n"] == 3


def test_device_login_expired_code(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    grant = json.dumps({
        "device_code": "d" * 64, "user_code": "ACDE-FGHJ",
        "verification_uri": "https://cloud.example/device", "expires_in": 900, "interval": 5,
    }).encode()

    def _urlopen(req, timeout):  # noqa: ARG001
        if req.full_url.endswith("/device/code"):
            return FakeResponse(grant)
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", None,
            io.BytesIO(json.dumps({"error": "expired_token"}).encode()),
        )

    with patch("urllib.request.urlopen", _urlopen):
        rc = cli.cmd_login(_device_args())
    assert rc == 1
    assert not (tmp_path / ".acp" / "credentials").exists()


def test_device_login_gateway_unreachable(tmp_path):
    def _down(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("down")
    with patch("urllib.request.urlopen", _down):
        rc = cli.cmd_login(_device_args())
    assert rc == 1
