# btc-race-24h-20261112-1439: deep-dip mean-reversion bot for cbBTC/USDC.
# v2: dip 70 bps below EMA(60), full size, STAGED exit: sell half at +60 bps,
# let the rest run to +80 bps, stop-loss -150 bps, max hold 120 bars.
# v2 verified by local replay: dev net PnL +$5.92, 4 filled orders, MDD -0.35%.
#
# Strategy: BTC on 1-minute bars moves slowly (1-min return SD ~5 bps) and a
# full-size round trip costs ~26 bps in fees, slippage and pool impact, so
# small wiggles are untradable. The edge on the dev window is in rare deep
# dips: when the close falls at least 70 bps below its 60-bar EMA the price
# bounces back within ~2 hours. The bot therefore waits for a deep dip, buys
# with all its cash, and exits in two stages. One position at a time, long
# only, no leverage, no shorts. Deterministic: pure float math on the bars
# seen, no clock, no network, no randomness.
#
# Why staged instead of a single take-profit: on dev the one good bounce
# peaked near +85 bps and took ~94 bars to reach +80, so a single TP at +80
# banks the most on that bounce but a bounce that stalls at +60..+80 banks
# nothing and exits at the hold limit instead. Selling half at +60 banks the
# proven move and leaves half as a free option on the bounce running further;
# across the TP1 50-60 / hold 120-240 plateau this scores +$5.7-5.9 on dev
# (v1 single TP +60 scored +$4.59), with lower drawdown. Shallower dips,
# trend filters, bounce confirmation, aggregated bars and drawdown-from-high
# entries were all tested on dev and lose money, so the entry is unchanged.
#
# Wire protocol: agent-fun/bars-replay/v1, via the vendored makefun_bars shim
# (a byte-identical copy of the kit's sdk/makefun_bars.py). The harness sends
# hello once, then one batch per minute bar; the bot answers each batch with
# exactly one reply. Orders decided on a batch fill at the next bar's open.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
# EMA span for the dip reference (bars).
EMA_SPAN = 60
# Enter when the close is this many bps below the EMA.
DIP_BPS = 70
# Stage 1: sell half the position at this many bps above the fill price.
TP1_BPS = 60
# Stage 2: sell the rest at this many bps above the fill price.
TP2_BPS = 80
# Exit everything at this many bps below the fill price.
SL_BPS = 150
# Exit everything after this many bars held, even without TP/SL.
MAX_HOLD_BARS = 120
# Reject a fill more than this far from the latest close (protects vs gaps).
MAX_SLIPPAGE_BPS = 100

_closes = []
_ema = None
_entry_price = None
_hold_bars = 0
_stage = 0


def _ema_step(close):
    global _ema
    if _ema is None:
        _ema = float(close)
    else:
        _ema += (2.0 / (EMA_SPAN + 1.0)) * (float(close) - _ema)
    return _ema


def _fill_price(state, held):
    """Recover the fill price (raw units) from the position's cost.

    cost is the quote spent, qty the coins bought; cost/qty is the fill price
    grossed up by the buy fee, so divide the fee back out. Uses the scales
    from hello, so it stays exact if the harness ever changes them.
    """
    sym = state.symbols[SYMBOL]
    quote_scale = int(state.hello["capital"]["quote_scale"])
    raw = held.cost * 10 ** (int(sym["qty_scale"]) + int(sym["price_scale"]))
    raw //= held.qty * 10 ** quote_scale
    fee_bps = int(state.hello["fees"]["fee_bps"])
    return raw * 10000 // (10000 + fee_bps)


def _sell(held, qty):
    return [{
        "side": "sell",
        "symbol": SYMBOL,
        "qty": qty,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


def on_batch(state, batch):
    global _entry_price, _hold_bars, _stage
    close = state.close(SYMBOL)
    if close is None:
        return []
    _closes.append(close)
    ema = _ema_step(close)
    held = state.positions.get(SYMBOL)

    if held is None:
        _entry_price = None
        _hold_bars = 0
        _stage = 0
        if state.warming_up or len(_closes) <= EMA_SPAN:
            return []
        dip_bps = (float(close) / ema - 1.0) * 10000.0
        if dip_bps <= -DIP_BPS:
            amount = state.cash
            min_notional = int(state.hello["fees"]["min_notional_quote_units"])
            if amount >= min_notional:
                return [{
                    "side": "buy",
                    "symbol": SYMBOL,
                    "notional_quote_units": amount,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
        return []

    if _entry_price is None:
        _entry_price = _fill_price(state, held)
        _hold_bars = 0
        _stage = 0
    _hold_bars += 1
    ret_bps = (float(close) / _entry_price - 1.0) * 10000.0
    if _stage == 0 and ret_bps >= TP1_BPS:
        _stage = 1
        return _sell(held, held.qty // 2)
    if _stage == 1 and (ret_bps >= TP2_BPS or ret_bps <= -SL_BPS
                        or _hold_bars >= MAX_HOLD_BARS):
        return _sell(held, held.qty)
    if _stage == 0 and (ret_bps <= -SL_BPS or _hold_bars >= MAX_HOLD_BARS):
        return _sell(held, held.qty)
    return []


makefun_bars.run(on_batch)
