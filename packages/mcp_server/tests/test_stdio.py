"""``sensei-mcp`` (stdio) takes its credential from the environment, and says what is missing."""

from __future__ import annotations

import pytest

from sensei_mcp import stdio


@pytest.mark.parametrize(
    ("url", "key"),
    [("", "rs_x_y"), ("https://sensei.example", ""), ("https://sensei.example", "not-a-key")],
)
def test_without_an_address_and_an_api_key_it_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str, key: str
) -> None:
    monkeypatch.setenv("SENSEI_URL", url)
    monkeypatch.setenv("SENSEI_API_KEY", key)
    with pytest.raises(SystemExit) as stopped:
        stdio.main()
    assert stopped.value.code == 2
    assert "SENSEI_API_KEY" in capsys.readouterr().err
