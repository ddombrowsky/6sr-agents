#!/usr/bin/env python3
"""How the live strategy's real fills compare to the paper book it was promoted on.

A strategy earns the live flag on a *paper* track record: it books every trade at its
config's trade_amount_usd against a 1000 USD simulated balance. The real order that
follows is clamped by stellar_trader's caps, the remaining daily budget and claudio's
actual on-chain balance, and until 2026-08-03 the result of that submission was printed
to stdout and discarded. So the live P&L could neither confirm nor refute the paper P&L
that justified the promotion, and the two books held structurally different positions the
whole time a strategy was live.

trade_logger now records what actually happened on each attempt (the `live` object in
each log line). This reads that back and answers the question the promotion needs:
*against a correctly-sized baseline, what did the real fills do?*

Read-only. Deliberately lives in /opt/master_agent rather than /opt/tools: tools is the
money boundary monitor.check_boundary_integrity() watches, and a reporting script there
would cost a halt and a re-baseline every time its output format changed.

  python3 /opt/live_report.py                 # the current live strategy, one summary
  python3 /opt/live_report.py <name> --json   # the full machine-readable report

Two caveats it will not let you forget, and states in its own output:

* `live_sized_return_pct` is a *simulation of the real fills* -- the same decisions, sized
  by what actually filled. It is not claudio's account P&L: that account is shared across
  leader changes and predates this system, so no per-strategy on-chain return is derivable
  from these logs.
* Lines written before this feature existed carry no `live` object at all. They are
  counted and reported separately as `unrecorded` rather than being folded into either
  side, because "not recorded" and "attempted and refused" are very different findings.
"""
import json
import statistics
import sys
import time
from collections import Counter, deque
from pathlib import Path

TRADES_DIR = Path('/opt/trades')
STRATEGY_STATE = Path('/opt/strategy_state.json')
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json')

# A live strategy ticks every 30s, so a month of trading is ~90k lines. Bound the read so
# a runaway log can never stall the monitor cycle that prints the summary line.
MAX_LINES = 200_000


def _trade_log_path(name):
    """Where `name`'s trades actually got logged.

    Same fallback monitor.trade_log_path does -- trade_logger names the file after
    config.json's "name", which a revision may rewrite. Duplicated rather than imported:
    monitor imports this module, and reporting should not drag in the scoring stack.
    """
    log_path = TRADES_DIR / f'{name}.log'
    if log_path.exists():
        return log_path
    try:
        entry = json.load(STRATEGY_STATE.open())[name]
        cfg = json.load(open(Path(entry['path']) / 'config.json'))
        alt = TRADES_DIR / f"{cfg['name']}.log"
        if alt.exists():
            return alt
    except Exception:
        pass
    return log_path


def _load_live_strategy():
    try:
        return json.load(LIVE_STRATEGY_FILE.open())
    except Exception:
        return None


def _read_lines(path, since):
    """Parsed log entries at or after `since`, newest MAX_LINES only."""
    entries = []
    try:
        with path.open() as f:
            for line in deque(f, maxlen=MAX_LINES):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if since and float(e.get('timestamp') or 0) < since:
                    continue
                entries.append(e)
    except FileNotFoundError:
        return []
    return entries


def _is_xlm(entry):
    """True for an XLM-leg line, including v1 lines that predate the asset fields."""
    return (entry.get('asset_spec') or entry.get('asset') or 'XLM') == 'XLM'


# Which pubnet.log lines are executed trades, and which are offer lifecycle.
#
# stellar_trader._log_pubnet_trade is called from both kinds of execution path. On the
# TAKER path (domain_sdex) every line is a fill: submit_trade writes `buy`/`sell` and
# wind_down writes `wind_down_sell`. On the MAKER path (domain_sdex_maker) not one line
# is a fill -- place_offer and cancel_offer write `offer_bid`, `offer_ask` and
# `offer_cancel`, which are the placement, re-price and cancellation of a RESTING offer.
# A maker's fills never reach this file at all: quote_executor._live_sync detects them by
# reconciling against Horizon and records them through trade_logger instead.
#
# So `offer_*` lines cannot be counted against trade_logger's fill count. Several of them
# describe one offer (a re-price reuses the offer id) and an `offer_cancel` carries no
# notional at all -- which is how this check came to report "6 trades / $15.00" against 13
# real fills and print MISMATCH on every sdex_maker cycle while nothing was wrong.
#
# Classified by exclusion on purpose. A new lifecycle action mistaken for a fill costs a
# spurious MISMATCH, which is loud; a new FILL action mistaken for lifecycle would quietly
# drop real money out of the audit. Only the known-inert names are excluded.
_PUBNET_LIFECYCLE_PREFIX = 'offer_'
# monitor liquidating a leader change, not a strategy decision.
_PUBNET_IGNORED_ACTIONS = frozenset({'wind_down_sell'})


def _pubnet_cross_check(name, since, log_submitted, log_usd):
    """Compare the paper log's recorded fills against stellar_trader's own ledger.

    The two records are written by different modules on different code paths
    (trade_logger vs stellar_trader._log_pubnet_trade, which attributes by reading
    live_strategy.json rather than trusting its caller), so agreement is real audit
    evidence and a mismatch is a finding worth surfacing rather than a rounding note.

    `match` is TRI-STATE. True and False mean what they say. None means the two records
    are not comparable at all, because this domain writes only offer lifecycle here (see
    _PUBNET_LIFECYCLE_PREFIX) and its fills are recorded elsewhere. Callers must test
    `is False`: a falsy None is "no evidence either way", and reporting that as a mismatch
    is exactly the bug this distinction removes.
    """
    path = TRADES_DIR / f'{name}.pubnet.log'
    if not path.exists():
        return {'available': False, 'match': log_submitted == 0,
                'note': f'no {path.name}; expected when nothing has ever filled',
                'log_submitted': log_submitted, 'log_usd': round(log_usd, 6),
                'pubnet_trades': 0, 'pubnet_usd': 0.0, 'offer_ops': {}}
    trades = usd = 0
    offer_ops = Counter()
    for e in _read_lines(path, since):
        action = e.get('action') or ''
        if action in _PUBNET_IGNORED_ACTIONS:
            continue
        if action.startswith(_PUBNET_LIFECYCLE_PREFIX):
            offer_ops[action] += 1
            continue
        trades += 1
        usd += float(e.get('amount_usd') or 0.0)

    # Any offer lifecycle at all means quote_executor is the execution path, and a maker's
    # fills bypass this file whether or not some other line here happens to be a fill. The
    # count is still reported rather than hidden, so a stray one is visible to --json.
    if offer_ops:
        note = (f'{path.name} records offer lifecycle, not fills; a maker detects its '
                f'fills by reconciliation, so there is nothing here to count against '
                f'{log_submitted} recorded fill(s)')
        if trades:
            note += f' ({trades} non-offer line(s) also present)'
        return {'available': True, 'match': None, 'note': note,
                'log_submitted': log_submitted, 'log_usd': round(log_usd, 6),
                'pubnet_trades': trades, 'pubnet_usd': round(usd, 6),
                'offer_ops': dict(offer_ops)}

    return {'available': True,
            'match': trades == log_submitted and abs(usd - log_usd) < 0.01,
            'note': None,
            'log_submitted': log_submitted, 'log_usd': round(log_usd, 6),
            'pubnet_trades': trades, 'pubnet_usd': round(usd, 6),
            'offer_ops': {}}


def _caps_now():
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
        return {'max_trade_usd': float(stellar_trader.MAX_TRADE_USD),
                'max_daily_usd': float(stellar_trader.MAX_DAILY_USD)}
    except Exception:
        return None


def report(name=None, since=None):
    """Live-vs-paper accounting for one strategy. Returns a dict; never raises."""
    live = _load_live_strategy()
    if name is None:
        name = (live or {}).get('name')
    if not name:
        return {'error': 'no live strategy'}
    if since is None and live and live.get('name') == name:
        since = live.get('since')

    path = _trade_log_path(name)
    entries = _read_lines(path, since)
    out = {'name': name, 'since': since, 'log': str(path),
           'since_iso': time.strftime('%Y-%m-%d %H:%M', time.localtime(since)) if since else None,
           'trades': len(entries)}
    if not entries:
        # The paper log is written only by this strategy's own execute_trade calls, i.e.
        # trades its decide() actually chose. It can legitimately stay empty since
        # promotion (thresholds just haven't fired) while pubnet.log still gains lines
        # under this name: stellar_trader.ensure_trading_cushion() buys real XLM once at
        # promotion via submit_trade directly, bypassing execute_trade entirely, and
        # _current_live_name() attributes it to whoever is live right now -- correctly,
        # by that function's own design, but invisibly to a report that only reads the
        # paper log. Without this check that real spend reads as "no trades" (seen on
        # clone_72b9b4cd5752, 2026-08-13: 5 cushion buys on pubnet.log, zero paper lines).
        pubnet = _pubnet_cross_check(name, since, 0, 0.0)
        if pubnet.get('pubnet_trades'):
            out['pubnet_cross_check'] = pubnet
            out['note'] = (f"no strategy-decided trades since promotion, but "
                           f"{pubnet['pubnet_trades']} real trade(s) totaling "
                           f"${pubnet['pubnet_usd']:.2f} on pubnet.log in this window "
                           f"(promotion cushion-funding, not a strategy decision)")
        else:
            out['error'] = f'no trades logged for {name} since promotion'
        return out

    attempts = submitted = refused = unrecorded = non_xlm_paper = 0
    paper_usd = live_usd = 0.0
    ratios = []
    reasons = Counter()

    for e in entries:
        paper_usd += float(e.get('amount_usd') or 0.0)
        lv = e.get('live')
        if lv is None:
            unrecorded += 1
            continue
        attempts += 1
        filled = float(lv.get('amount_usd') or 0.0)
        if lv.get('submitted'):
            submitted += 1
            live_usd += filled
            if lv.get('size_ratio') is not None:
                ratios.append(float(lv['size_ratio']))
        else:
            refused += 1
            reasons[lv.get('reason') or 'unknown'] += 1
            if not _is_xlm(e):
                non_xlm_paper += 1

    out.update({
        'attempts': attempts, 'submitted': submitted, 'refused': refused,
        'unrecorded': unrecorded, 'non_xlm_paper': non_xlm_paper,
        'fill_rate': round(submitted / attempts, 6) if attempts else None,
        'paper_usd': round(paper_usd, 6), 'live_usd': round(live_usd, 6),
        'realized_ratio': round(live_usd / paper_usd, 6) if paper_usd > 0 else None,
        'refusals': reasons.most_common(5),
    })
    # Over submitted lines only: averaging in the refusals would just restate fill_rate.
    if ratios:
        out['size_ratio'] = {'mean': round(statistics.fmean(ratios), 6),
                             'median': round(statistics.median(ratios), 6),
                             'min': round(min(ratios), 6), 'max': round(max(ratios), 6)}

    out.update(_returns(entries))
    out['borrow_cross_check'] = _borrow_cross_check(name, out.get('borrowed_xlm'))
    out['pubnet_cross_check'] = _pubnet_cross_check(name, since, submitted, live_usd)

    recorded = (live or {}).get('sizing') if live and live.get('name') == name else None
    caps = _caps_now()
    out['sizing'] = {'at_promotion': recorded, 'now': caps,
                     'caps_changed_since_promotion': bool(
                         recorded and caps
                         and any(recorded.get(k) is not None and recorded.get(k) != v
                                 for k, v in caps.items()))}
    return out


def _borrow_cross_check(name, borrowed_from_log):
    """Does the log's short liability agree with the strategy's own state.json?

    Same spirit as _pubnet_cross_check: a second, independently written source for a number
    this report now depends on. `borrowed_xlm` reaches the trade log only from a
    trade_logger new enough to record it (2026-08-12), and a strategy process holds its
    imports for its entire lifetime -- so one started before that date goes on writing lines
    without the field. Those read as flat and silently restore the exact overstatement the
    field was added to remove. state.json is written by that same process but straight from
    `state`, so a disagreement here means stale code rather than a stale number, and the fix
    is a restart, not an edit.

    Returns None when there is nothing to compare against.
    """
    try:
        entry = json.load(STRATEGY_STATE.open())[name]
        state = json.load(open(Path(entry['path']) / 'state.json'))
    except Exception:
        return None
    actual = float(state.get('borrowed_xlm') or 0.0)
    logged = float(borrowed_from_log or 0.0)
    # Drift of about one trade is expected and not worth reporting: state.json is rewritten
    # every tick, the log only when something actually traded.
    if abs(actual - logged) <= max(1.0, actual * 0.02):
        return {'match': True, 'state_json_xlm': round(actual, 4), 'log_xlm': round(logged, 4)}
    note = ('log shows no short but state.json does -- strategy is running a trade_logger '
            'too old to record borrowed_xlm; restart it') if logged == 0 and actual > 0 else \
           'log and state.json disagree on the outstanding short'
    return {'match': False, 'state_json_xlm': round(actual, 4),
            'log_xlm': round(logged, 4), 'note': note}


def _returns(entries):
    """Paper return vs the same decisions sized by what actually filled.

    Both books start from the *same* net worth -- the paper book's, reconstructed by
    undoing the first in-window trade -- so the two returns are directly comparable. The
    paper book then just follows the balances the log already carries; the live-sized book
    moves only on lines that really submitted, by the amount that really filled.

    XLM leg only, on both sides. Real money is XLM-only (trade_logger refuses non-XLM live
    trades), and marking extra legs is score.py's job against a real order book, not
    something to guess at from a trade log.

    The window starts at the first line that carries a `live` record, not at the first
    trade since promotion. Anything earlier predates the recording and cannot be replayed
    -- counting those as "did not fill" would report the live book flat while the paper
    book moved, which reads like a measured divergence and is really just missing data.
    With no recorded line at all, no return is reported.

    Both net worths are marked **net of the XLM short liability** (SHORTING_PLAN.md).
    `borrowed_xlm` is a debt whose sale proceeds are already sitting in `balance_usd`, so
    adding the cash without subtracting the debt books a short as pure profit. This
    function did exactly that until 2026-08-12: on seed_1124713bc960, carrying a 762 XLM
    short, it reported +20.9% against a true -1.6%. score.py, backtest.py and portfolio.py
    have always subtracted it -- they read state.json, where it is visible; this reads the
    trade log, where it was not recorded until the same date. A line without the field is
    read as flat, which is what every line predating shorting actually was.

    Only the paper book carries a liability. The live replay below clamps every sell to
    `live_xlm`, so it can never go short -- which is not a modelling shortcut but the real
    constraint: pubnet has no borrow facility, only stellar_trader's pre-funded
    SHORT_BUFFER_XLM. That asymmetry is the point of the comparison, not a flaw in it.
    """
    recorded_from = next((i for i, e in enumerate(entries) if e.get('live') is not None), None)
    if recorded_from is None:
        return {'return_note': f'no live-recorded trades yet ({len(entries)} line(s) '
                               f'predate the recording); returns not computed'}
    xlm_idx = [i for i in range(recorded_from, len(entries)) if _is_xlm(entries[i])]
    if not xlm_idx:
        return {'return_note': 'no XLM-leg trades in window; returns not computed'}

    xlm = [entries[i] for i in xlm_idx]
    first, last = xlm[0], xlm[-1]
    price0 = float(first.get('price') or 0)
    price_last = float(last.get('price') or 0)
    if price0 <= 0 or price_last <= 0:
        return {'return_note': 'unusable prices in window; returns not computed'}

    amount_usd = float(first.get('amount_usd') or 0.0)
    amount_xlm = float(first.get('amount_xlm') or 0.0)
    usd = float(first.get('balance_usd') or 0.0)
    held = float(first.get('balance_xlm') or 0.0)
    if (first.get('action') or '').startswith('buy'):
        usd, held = usd + amount_usd, held - amount_xlm
    else:
        usd, held = usd - amount_usd, held + amount_xlm

    # The debt as it stood *before* the first in-window trade. The preceding log line
    # carries it exactly; with no preceding line there is nothing to read it from, so fall
    # back to this line's own post-trade figure -- wrong by at most the borrow opened by
    # that single trade, and only when the window happens to open on a short-sell.
    borrowed_start = float((entries[xlm_idx[0] - 1] if xlm_idx[0] > 0 else first)
                           .get('borrowed_xlm') or 0.0)
    borrowed_last = float(last.get('borrowed_xlm') or 0.0)

    start_net = usd + held * price0 - borrowed_start * price0
    if start_net <= 0:
        return {'return_note': 'could not reconstruct a starting net worth'}

    live_usd_bal, live_xlm = usd, held
    for e in xlm:
        lv = e.get('live') or {}
        if not lv.get('submitted'):
            continue
        price = float(e.get('price') or 0)
        filled = float(lv.get('amount_usd') or 0.0)
        if price <= 0 or filled <= 0:
            continue
        if (e.get('action') or '').startswith('buy'):
            spend = min(filled, live_usd_bal)
            live_usd_bal -= spend
            live_xlm += spend / price
        else:
            sell = min(filled / price, live_xlm)
            live_xlm -= sell
            live_usd_bal += sell * price

    paper_net = (float(last.get('balance_usd') or 0.0)
                 + float(last.get('balance_xlm') or 0.0) * price_last
                 - borrowed_last * price_last)
    live_net = live_usd_bal + live_xlm * price_last
    paper_pct = (paper_net - start_net) / start_net * 100
    live_pct = (live_net - start_net) / start_net * 100
    return {'start_net_worth': round(start_net, 6),
            'paper_net_worth': round(paper_net, 6),
            'live_sized_net_worth': round(live_net, 6),
            'last_price': price_last,
            'borrowed_xlm': round(borrowed_last, 4),
            'short_liability_usd': round(borrowed_last * price_last, 6),
            'paper_return_pct': round(paper_pct, 4),
            'live_sized_return_pct': round(live_pct, 4),
            'return_gap_pct': round(paper_pct - live_pct, 4)}


def summary_line(name=None):
    """One line for monitor's per-cycle log, or None if there is nothing to say."""
    try:
        r = report(name)
    except Exception as e:
        return f'LIVE vs PAPER: report failed ({e})'
    if r.get('error'):
        return f"LIVE vs PAPER {r.get('name', '?')}: {r['error']}"
    if r.get('note'):
        return f"LIVE vs PAPER {r.get('name', '?')}: {r['note']}"

    fill = r.get('fill_rate')
    parts = [f"LIVE vs PAPER {r['name']} (since {r['since_iso'] or 'all time'}): "
             f"{r['attempts']} attempts / {r['submitted']} filled"
             f"{f' ({fill * 100:.1f}%)' if fill is not None else ''}"]
    ratio = r.get('realized_ratio')
    parts.append(f"paper ${r['paper_usd']:.2f} notional vs live ${r['live_usd']:.2f}"
                 f"{f' (ratio {ratio:.3f})' if ratio is not None else ''}")
    if r.get('paper_return_pct') is not None:
        parts.append(f"paper {r['paper_return_pct']:+.2f}% vs "
                     f"live-sized {r['live_sized_return_pct']:+.2f}%")
    elif r.get('return_note'):
        parts.append(r['return_note'])
    if r.get('borrowed_xlm'):
        parts.append(f"open short {r['borrowed_xlm']:.0f} XLM "
                     f"(${r['short_liability_usd']:.2f} liability, netted out of paper)")
    borrow = r.get('borrow_cross_check') or {}
    if borrow.get('match') is False:
        parts.append(f"SHORT LIABILITY UNVERIFIED: {borrow['note']} "
                     f"(state.json {borrow['state_json_xlm']:.0f} XLM "
                     f"vs log {borrow['log_xlm']:.0f})")
    if r.get('unrecorded') and r.get('paper_return_pct') is not None:
        parts.append(f"{r['unrecorded']} pre-recording line(s) excluded")
    if r.get('refusals'):
        reason, count = r['refusals'][0]
        parts.append(f'top refusal: {reason} ({count})')
    check = r.get('pubnet_cross_check') or {}
    # `is False`, not falsiness: None is "not comparable", reported on its own line below.
    if check.get('available') and check.get('match') is False:
        parts.append(f"MISMATCH vs pubnet.log ({check['pubnet_trades']} trades / "
                     f"${check['pubnet_usd']:.2f})")
    elif check.get('match') is None and check.get('offer_ops'):
        # Not an audit of the fills, but evidence the money path ran at all -- which is
        # the question someone reading this line about a fresh promotion actually has.
        ops = ', '.join(f'{n} {action}'
                        for action, n in sorted(check['offer_ops'].items()))
        parts.append(f'pubnet.log: {ops} (offer lifecycle; not comparable to fills)')
    if (r.get('sizing') or {}).get('caps_changed_since_promotion'):
        parts.append('caps changed since promotion')
    return '; '.join(parts)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    name = args[0] if args else None
    if '--json' in sys.argv:
        print(json.dumps(report(name), indent=2))
    else:
        line = summary_line(name)
        print(line if line else 'no live strategy')


if __name__ == '__main__':
    main()
