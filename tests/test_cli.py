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
