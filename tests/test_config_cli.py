from pathlib import Path

import pytest
from conftest import T0, perp, settlement_frame, spot

from funding_capture.cli import main
from funding_capture.config import load_settings
from funding_capture.data import HistoryStore
from funding_capture.models import Construction

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def test_example_config_loads():
    s = load_settings(EXAMPLE)
    assert [v.name for v in s.venues] == ["binance", "bybit", "okx", "hyperliquid"]
    assert s.venues[-1].quotes == ("USDC",)
    assert s.strategy.constructions == (Construction.SPOT_PERP, Construction.PERP_PERP)
    assert s.strategy.forecast.anchor_hourly == pytest.approx(0.1095 / 8760)
    assert s.costs.fees["binance"].perp_taker == 0.00045
    assert s.costs.fees["binance"].perp_maker == 0.0002  # untouched default kept
    assert s.portfolio.max_position_usd == 2000


def test_unknown_keys_rejected(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("strategy:\n  entry_aprr: 0.1\n")
    with pytest.raises(ValueError, match="entry_aprr"):
        load_settings(bad)
    bad.write_text("portfolo: {}\n")
    with pytest.raises(ValueError, match="portfolo"):
        load_settings(bad)


def test_store_roundtrip_and_backtest_cli(tmp_path, capsys):
    store = HistoryStore(tmp_path / "data")
    pa, pb = perp("a"), perp("b")
    store.save_instruments("a", [pa, spot("a")])
    store.save_instruments("b", [pb])
    rows_a = settlement_frame(T0, 30 * 24, 8, lambda i: 0.0004).itertuples(index=False)
    store.append_funding(pa, [(r.ts_ms, r.rate) for r in rows_a])
    rows_b = settlement_frame(T0, 30 * 24, 8, lambda i: 0.0001).itertuples(index=False)
    assert store.append_funding(pb, [(r.ts_ms, r.rate) for r in rows_b]) == 90
    assert store.append_funding(pb, [(r.ts_ms, r.rate) for r in rows_b]) == 0  # idempotent

    insts, series = store.load_all()
    assert {i.key for i in insts} == {pa.key, pb.key, spot("a").key}
    assert len(series[pa.key]) == 90

    trades = tmp_path / "trades.csv"
    main(["backtest", "--data", str(tmp_path / "data"), "--trades", str(trades)])
    out = capsys.readouterr().out
    assert "APR on capital" in out
    assert trades.exists()


def test_save_universe_keeps_spot_only_venue_markets(tmp_path):
    from funding_capture.data import save_universe

    store = HistoryStore(tmp_path)
    hl_perp = perp("hyperliquid", "WIF", quote="USDC")
    kr = [spot("kraken", "WIF", quote="USD"), spot("kraken", "DOGE", quote="USD")]
    save_universe(store, {"hyperliquid": [hl_perp], "kraken": kr}, {"hyperliquid": [hl_perp]})
    assert [i.asset for i in store.load_instruments("kraken")] == ["WIF"]  # DOGE has no perp
    assert store.load_instruments("hyperliquid") == [hl_perp]


def test_env_file_loader(tmp_path, monkeypatch):
    from funding_capture.cli import load_env_file

    env = tmp_path / ".env"
    env.write_text('# keys\nexport KRAKEN_API_KEY="abc"\nKRAKEN_SECRET=s3cr=t\nEXISTING=new\n')
    monkeypatch.setenv("EXISTING", "old")
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_SECRET", raising=False)
    assert load_env_file(str(env)) == 2
    import os

    assert os.environ["KRAKEN_API_KEY"] == "abc"
    assert os.environ["KRAKEN_SECRET"] == "s3cr=t"
    assert os.environ["EXISTING"] == "old"  # never overrides the real environment
    assert load_env_file(str(tmp_path / "missing")) == 0


def test_status_reads_paper_state_separately_from_live(tmp_path, capsys):
    from funding_capture.models import Candidate, PairPosition
    from funding_capture.state import position_to_dict, save_json

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"portfolio:\n  state_dir: {tmp_path / 'state'}\n")
    pos = PairPosition(
        "P1",
        Candidate(Construction.SPOT_PERP, "BTC", spot("k"), perp("h")),
        0.1,
        100.0,
        100.0,
        T0,
        funding_usd=1.5,
    )
    save_json(
        tmp_path / "state" / "paper" / "engine.json",
        {"positions": [position_to_dict(pos)], "closed": []},
    )
    main(["-c", str(cfg), "status"])
    out = capsys.readouterr().out
    assert "open pairs: 1" in out and "P1" in out and "funding=$1.50" in out
    with pytest.raises(SystemExit, match="fcap live"):
        main(["-c", str(cfg), "status", "--live"])


def test_read_funding_csv_variants(tmp_path):
    from funding_capture.data import read_funding_csv

    a = tmp_path / "a.csv"
    a.write_text(
        "Funding Time,Funding Rate\n2026-10-01T01:00:00Z,0.0021%\n2026-10-01T00:00:00Z,-0.001%\n"
    )
    rows = read_funding_csv(a)
    assert rows == [
        (1_790_812_800_000, pytest.approx(-0.00001)),
        (1_790_816_400_000, pytest.approx(0.000021)),
    ]
    b = tmp_path / "b.csv"
    b.write_text("ts,rate\n1790812800,0.00002\n")
    assert read_funding_csv(b) == [(1_790_812_800_000, pytest.approx(0.00002))]


def test_us_config_loads():
    s = load_settings(EXAMPLE.parent / "config.us.yaml")
    assert [v.name for v in s.venues] == ["coinbase", "kraken"]
    assert s.venues[1].kinds == ("spot",)
    assert s.costs.fees["coinbase"].perp_min_fee_per_contract == 0.20
    assert s.costs.maker_venues == ("kraken", "coinbase")
    assert s.strategy.forecast.anchor_hourly == 0.0


def test_keys_command_coinbase_json_and_kraken_prompt(tmp_path, monkeypatch):
    import json
    import os
    import stat

    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nCOINBASE_API_KEY=old\n")
    key = tmp_path / "cdp_api_key.json"
    pem = "-----BEGIN EC PRIVATE KEY-----\nAAA\n-----END EC PRIVATE KEY-----\n"
    key.write_text(json.dumps({"name": "organizations/o/apiKeys/k", "privateKey": pem}))
    main(["--env-file", str(env), "keys", "coinbase", "--json", str(key)])
    text = env.read_text()
    assert "OTHER=1" in text and "COINBASE_API_KEY=organizations/o/apiKeys/k" in text
    assert "COINBASE_API_KEY=old" not in text
    saved = tmp_path / ".fcap" / "coinbase.pem"
    assert saved.read_text() == pem and f"COINBASE_SECRET_FILE={saved}" in text
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600

    monkeypatch.setattr("builtins.input", lambda prompt: "KRAKENKEY")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "c2VjcmV0==")
    main(["--env-file", str(env), "keys", "kraken"])
    text = env.read_text()
    assert "KRAKEN_API_KEY=KRAKENKEY" in text and "KRAKEN_SECRET=c2VjcmV0==" in text

    from funding_capture.cli import load_env_file
    from funding_capture.venues.ccxt_venue import credentials_from_env

    for k in ("COINBASE_API_KEY", "COINBASE_SECRET_FILE", "KRAKEN_API_KEY", "KRAKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    load_env_file(str(env))
    assert credentials_from_env("coinbase") == {
        "apiKey": "organizations/o/apiKeys/k",
        "secret": pem.strip(),
    }
    assert credentials_from_env("kraken")["secret"] == "c2VjcmV0=="


def test_status_shows_annualised_return(tmp_path, capsys):
    import time

    from funding_capture.models import Candidate, PairPosition
    from funding_capture.state import position_to_dict, save_json

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"portfolio:\n  state_dir: {tmp_path / 'state'}\n  capital_usd: 1000\n")
    closed = PairPosition(
        "P1",
        Candidate(Construction.SPOT_PERP, "BTC", spot("k"), perp("h")),
        0.1,
        100.0,
        100.0,
        T0,
        funding_usd=12.0,
        entry_fees_usd=2.0,
        closed_ms=T0 + 1,
    )
    ten_days_ago = int(time.time() * 1000) - 10 * 86_400_000
    save_json(
        tmp_path / "state" / "paper" / "engine.json",
        {
            "positions": [],
            "closed": [position_to_dict(closed)],
            "started_ms": ten_days_ago,
            "capital_usd": 1000,
        },
    )
    main(["-c", str(cfg), "status"])
    out = capsys.readouterr().out
    assert "running for 10.0 days" in out
    assert "annualised return on capital so far: +36.5%" in out  # $10 on $1k in 10 days
