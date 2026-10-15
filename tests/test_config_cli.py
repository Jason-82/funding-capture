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
