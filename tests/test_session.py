import json
import os
import stat
from types import SimpleNamespace

import pytest

from pmscan import cli, env
from pmscan.trading import authorize_session_key

pytest.importorskip("eth_account")
pytest.importorskip("polymarket")


class FakeOwner:
    def __init__(self, wallet_type="DEPOSIT_WALLET"):
        self.wallet, self.wallet_type = "0xWALLET", wallet_type
        self.authorized = []
        self.waited = False

    def authorize_session_key(self, *, address, scopes):
        self.authorized.append((address, [str(s) for s in scopes]))
        owner = self
        tx = SimpleNamespace(wait=lambda: setattr(owner, "waited", True))
        return SimpleNamespace(session_key=SimpleNamespace(valid_until="2027-05-18T00:00:00Z"), transaction=tx)


def test_authorize_session_key_creates_a_clob_only_signer():
    owner = FakeOwner()
    out = authorize_session_key(owner, setup_approvals=False)
    [(address, scopes)] = owner.authorized
    assert address == out["session_address"]
    assert len(scopes) == 1 and "CLOB" in scopes[0]
    assert out["session_private_key"].startswith("0x") and len(out["session_private_key"]) == 66
    assert out["wallet"] == "0xWALLET" and out["valid_until"].startswith("2027") and owner.waited


def test_session_keys_need_a_deposit_wallet():
    with pytest.raises(RuntimeError, match="Deposit Wallet"):
        authorize_session_key(FakeOwner("POLY_PROXY"), setup_approvals=False)


def test_cli_saves_the_session_key_to_env_not_the_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PMSCAN_ODDS_API_KEY=abc\nPOLYMARKET_PRIVATE_KEY=old\n")
    monkeypatch.setattr(cli, "owner_client_from_env", lambda: FakeOwner())
    monkeypatch.setattr(cli, "authorize_session_key", lambda owner, setup_approvals: {
        "session_private_key": "0xSECRET", "session_address": "0xSESSION", "wallet": "0xWALLET",
        "wallet_type": "DEPOSIT_WALLET", "valid_until": "2027-05-18", "approvals": []})
    assert cli.main(["trade", "authorize-session"]) == 0
    text = (tmp_path / ".env").read_text()
    assert "POLYMARKET_PRIVATE_KEY=0xSECRET" in text and "PMSCAN_ODDS_API_KEY=abc" in text and "=old" not in text
    assert "POLYMARKET_WALLET_ADDRESS=0xWALLET" in text
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    assert "0xSECRET" not in capsys.readouterr().out


def test_update_dotenv_creates_file(tmp_path):
    env.update_dotenv({"A": "1"}, tmp_path / ".env")
    env.update_dotenv({"B": "2", "A": "3"}, tmp_path / ".env")
    assert (tmp_path / ".env").read_text() == "A=3\nB=2\n"


@pytest.mark.parametrize("wallet,key,needle", [
    ("0x123", "a" * 64, "wallet address"),
    ("0x" + "1" * 40, "0x" + "g" * 64, "private key"),
    ("0x" + "1" * 40, "abc", "private key"),
])
def test_credentials_are_checked_before_any_network_call(wallet, key, needle):
    from pmscan.trading import check_account
    with pytest.raises(ValueError, match=needle):
        check_account(wallet, key)


def test_json_mode_reports_errors_as_one_line(monkeypatch, capsys):
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", "0xnope")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "a" * 64)
    assert cli.main(["trade", "check-account", "--json"]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "wallet address" in json.loads(out[0])["error"]


def test_check_account_reports_wallet_type(monkeypatch):
    import polymarket

    from pmscan import trading

    class Client:
        wallet, wallet_type = "0x" + "1" * 40, "POLY_PROXY"

        def get_balance_allowance(self, asset_type):
            return SimpleNamespace(balance=12_340_000)

    monkeypatch.setattr(polymarket.SecureClient, "create", classmethod(lambda cls, **kw: Client()))
    out = trading.check_account("0x" + "1" * 40, "0x" + "ab" * 32)
    assert out["wallet_type_label"].startswith("Proxy Wallet") and not out["session_keys_supported"]
    assert out["collateral_usd"] == pytest.approx(12.34) and out["signer"].startswith("0x")
