"""Mapping venue tickers onto a canonical asset so legs on different venues can be paired.

Venues quote small-priced coins in multiples ("1000PEPE" on Binance, "kPEPE" on
Hyperliquid, "1MBABYDOGE") and bridged spot tokens under other names ("UBTC").
Getting this wrong pairs two different things, so every pairing is also checked
for price agreement before trading (see ``risk.price_divergence``).
"""

from __future__ import annotations

import re

# Bridged/wrapped tickers that represent the same underlying for hedging purposes.
DEFAULT_ALIASES: dict[str, str] = {
    "XBT": "BTC",
    "UBTC": "BTC",
    "UETH": "ETH",
    "USOL": "SOL",
}

_NUMERIC_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
_PREFIX_VALUE = {"1000000": 1e6, "100000": 1e5, "10000": 1e4, "1000": 1e3, "1M": 1e6}
# Hyperliquid writes thousands with a lowercase k: kPEPE, kBONK, kSHIB.
_K_PREFIX = re.compile(r"^k(?=[A-Z0-9]{2,}$)")


def canonical_asset(base: str, aliases: dict[str, str] | None = None) -> tuple[str, float]:
    """Return ``(asset, multiplier)`` where one venue base unit = ``multiplier`` asset units."""
    aliases = DEFAULT_ALIASES if aliases is None else aliases
    multiplier = 1.0
    name = base
    if _K_PREFIX.match(name):
        name, multiplier = name[1:], 1e3
    else:
        m = _NUMERIC_PREFIX.match(name.upper())
        if m:
            prefix = m.group(1)
            name, multiplier = name[len(prefix) :], _PREFIX_VALUE[prefix]
    name = name.upper()
    return aliases.get(name, name), multiplier
