from __future__ import annotations

from nse_alert.login_ui import _success_body


def test_success_body_includes_token_and_copy_control() -> None:
    body = _success_body("abc_token_123")
    assert 'id="access-token"' in body
    assert 'value="abc_token_123"' in body
    assert "copyAccessToken" in body
    assert "Copy" in body
    assert "set-token" in body


def test_success_body_escapes_html() -> None:
    body = _success_body('<script>alert("x")</script>')
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
