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


def _pubnet_cross_check(name, since, log_submitted, log_usd):
    """Compare the paper log's recorded fills against stellar_trader's own ledger.

    The two records are written by different modules on different code paths
    (trade_logger vs stellar_trader._log_pubnet_trade, which attributes by reading
    live_strategy.json rather than trusting its caller), so agreement is real audit
    evidence and a mismatch is a finding worth surfacing rather than a rounding note.
    """
    path = TRADES_DIR / f'{name}.pubnet.log'
    if not path.exists():
        return {'available': False, 'match': log_submitted == 0,
                'note': f'no {path.name}; expected when nothing has ever filled',
                'log_submitted': log_submitted, 'log_usd': round(log_usd, 6),
                'pubnet_trades': 0, 'pubnet_usd': 0.0}
    trades = usd = 0
    for e in _read_lines(path, since):
        # wind_down_sell is monitor liquidating a leader change, not a strategy decision.
        if e.get('action') == 'wind_down_sell':
            continue
        trades += 1
        usd += float(e.get('amount_usd') or 0.0)
    return {'available': True,
            'match': trades == log_submitted and abs(usd - log_usd) < 0.01,
            'note': None,
            'log_submitted': log_submitted, 'log_usd': round(log_usd, 6),
            'pubnet_trades': trades, 'pubnet_usd': round(usd, 6)}


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
    out['pubnet_cross_check'] = _pubnet_cross_check(name, since, submitted, live_usd)

    recorded = (live or {}).get('sizing') if live and live.get('name') == name else None
    caps = _caps_now()
    out['sizing'] = {'at_promotion': recorded, 'now': caps,
                     'caps_changed_since_promotion': bool(
                         recorded and caps
                         and any(recorded.get(k) is not None and recorded.get(k) != v
                                 for k, v in caps.items()))}
    return out


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
    """
    recorded_from = next((i for i, e in enumerate(entries) if e.get('live') is not None), None)
    if recorded_from is None:
        return {'return_note': f'no live-recorded trades yet ({len(entries)} line(s) '
                               f'predate the recording); returns not computed'}
    xlm = [e for e in entries[recorded_from:] if _is_xlm(e)]
    if not xlm:
        return {'return_note': 'no XLM-leg trades in window; returns not computed'}

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
    start_net = usd + held * price0
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

    paper_net = float(last.get('balance_usd') or 0.0) + float(last.get('balance_xlm') or 0.0) * price_last
    live_net = live_usd_bal + live_xlm * price_last
    paper_pct = (paper_net - start_net) / start_net * 100
    live_pct = (live_net - start_net) / start_net * 100
    return {'start_net_worth': round(start_net, 6),
            'paper_net_worth': round(paper_net, 6),
            'live_sized_net_worth': round(live_net, 6),
            'last_price': price_last,
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
    if r.get('unrecorded') and r.get('paper_return_pct') is not None:
        parts.append(f"{r['unrecorded']} pre-recording line(s) excluded")
    if r.get('refusals'):
        reason, count = r['refusals'][0]
        parts.append(f'top refusal: {reason} ({count})')
    check = r.get('pubnet_cross_check') or {}
    if check.get('available') and not check.get('match'):
        parts.append(f"MISMATCH vs pubnet.log ({check['pubnet_trades']} trades / "
                     f"${check['pubnet_usd']:.2f})")
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
