#!/usr/bin/env python3
"""Replay a maker over a GRID of config values in one pass. The knob-tuning instrument.

## Why this exists

Read any recent revision log. A revision that wants to tune `min_spread_bp` writes
config.json, calls `backtest_maker_strategy`, reads the net edge, writes config.json
again, calls it again -- fifteen to thirty times. The 2026-09-04 11:29 cycle did exactly
that for `inventory_band_usd` and `inventory_skew_bp` and finished the turn at ~177k
tokens of context, most of it near-identical JSON.

Three things are wrong with that loop, and only the first is cost:

  1. Every step re-reads and re-buckets the same 400k+ trades. `maker_backtest.replay()`
     already takes `rows`/`tape`/`profile` as a fast path so the null can be replayed
     against identical data; this reuses it for a whole grid, so N configs cost roughly
     one load plus N replays instead of N loads plus N replays.
  2. Each step costs a model round trip and ~4k tokens of context. Thirty of them is most
     of a revision's budget spent on arithmetic the machine can do unattended.
  3. It searches ONE knob at a time and keeps the winner, which is hill-climbing on a
     surface whose knobs interact -- `quote_size_usd` against `inventory_band_usd` is the
     obvious pair, since the band is denominated in the size that fills it. A one-knob-at-
     a-time walk cannot see that and reliably stops on a ridge.

## Use

    import maker_sweep
    maker_sweep.sweep('/opt/strategies/clone_x',
                      grid={'min_spread_bp': [3, 5, 7, 9],
                            'quote_size_usd': [2, 4]},
                      days=7)

The grid is the FULL CROSS PRODUCT of the lists given, each combination applied on top of
the strategy's own config.json, replayed with the strategy's own `quote()`. So the call
above is 8 replays and reports all 8 ranked by net edge, with the strategy's unmodified
config included as the `baseline` row to compare against.

Nothing is written. The caller picks a row and writes that config itself -- a sweep that
edited config.json would make "what did the last call leave behind" part of the state a
revision has to track, and the whole point is to remove steps, not add invisible ones.

## Reading the result

`by_net_edge` is sorted best-first, but the top row is the best of N draws from a noisy
estimator and is biased upward by exactly the amount of searching that produced it. The
columns that survive that are `net_bp_per_fill` (scales with sample size rather than with
how long the replay ran) and `trades`. A row with 40 trades sitting on top of a grid of
rows with 3000 is a small-sample artifact, not a discovery -- MIN_TRADES_TRUSTED flags it
rather than dropping it, since "this knob kills fill flow" is itself worth seeing.

`split_halves=True` re-runs the top `split_top_n` rows on each half of the window
separately and adds `net_first_half` / `net_second_half`. A knob setting that only works
in one half of the tape is the thing this whole file exists to catch: the maker tape's two
weeks to 2026-09-04 contained one losing regime and one winning one, and a grid searched
across both picks settings that are really just bets on which regime repeats.
"""
import itertools
import os
import sys

sys.path = sys.path + ['/opt/tools']

import maker_backtest

# Below this many fills, a row's net edge is one or two trades and its ranking is noise.
# Flagged, never dropped: a knob that stops the strategy filling at all is a real finding.
MIN_TRADES_TRUSTED = 100

# A grid big enough to be slow is nearly always a mistake -- 6 knobs of 4 values each is
# 4096 replays, hours of CPU, and a result overfitted past any hope of meaning. Refused
# rather than truncated, because silently sweeping a subset of what was asked for and
# reporting it as the answer is worse than an error message.
MAX_COMBINATIONS = 64


def sweep(strategy_dir, grid, days=7, spec='XLM', base_config=None,
          split_halves=False, split_top_n=3):
    """Replay `strategy_dir`'s quote() once per point of `grid`. See the module docstring.

    `grid` maps a config key to the list of values to try. `base_config` overrides what is
    read from the strategy's config.json, for callers that want to sweep around a config
    that is not on disk yet.
    """
    import json

    if not isinstance(grid, dict) or not grid:
        return {'error': 'grid must be a non-empty {config_key: [values, ...]} dict'}
    keys = sorted(grid)
    values = []
    for key in keys:
        item = grid[key]
        if not isinstance(item, (list, tuple)) or not item:
            return {'error': f'grid[{key!r}] must be a non-empty list of values'}
        values.append(list(item))
    combos = list(itertools.product(*values))
    if len(combos) > MAX_COMBINATIONS:
        return {'error': f'{len(combos)} combinations exceeds MAX_COMBINATIONS '
                         f'({MAX_COMBINATIONS}); sweep fewer knobs or fewer values, and '
                         f'sweep the interacting pair rather than every knob at once'}

    if base_config is None:
        base_config = {}
        path = os.path.join(strategy_dir or '', 'config.json')
        if strategy_dir and os.path.exists(path):
            try:
                with open(path) as f:
                    base_config = json.load(f)
            except Exception as e:
                return {'error': f'could not read {path}: {e}'}
    if not isinstance(base_config, dict):
        return {'error': 'base_config must be a dict'}

    # Loaded ONCE for the whole grid: this is the entire speed argument for the file.
    rows = maker_backtest._load_rows(days, spec)
    if len(rows) < 10:
        return {'error': 'not enough recorded book history to replay a maker'}
    import dex_trades
    tape = dex_trades.get_trades(spec=spec, start_ts=rows[0]['ts'],
                                 end_ts=rows[-1]['ts'] + maker_backtest.MAX_GAP_S,
                                 sides_only=True)
    if not tape:
        return {'error': 'no trade tape cached; run dex_trades.backfill() first'}
    profile = maker_backtest._depth_profile(rows)

    # The strategy's real quote(), resolved ONCE. Resolving it per combination would let
    # a strategy whose main.py stopped importing halfway through the sweep report some
    # rows for its own logic and some for the mechanical fallback, in one table, unlabelled.
    quote_fn, source = maker_backtest._load_quote(strategy_dir, base_config)

    def run(config, rows_, tape_, profile_):
        return maker_backtest.replay(days=days, spec=spec, config=config,
                                     quote_fn=quote_fn, source=source, rows=rows_,
                                     tape=tape_, profile=profile_, _null=False)

    def row_of(label, config, result):
        trades = result.get('trades') or 0
        net = result.get('net_edge_usd')
        out = {'label': label,
               'config': {k: config.get(k) for k in keys},
               'trades': trades,
               'net_edge_usd': net,
               'net_bp_per_fill': round(net / trades * 10000.0, 3)
                                  if (net is not None and trades) else None,
               'spread_captured_usd': result.get('spread_captured_usd'),
               'adverse_selection_usd': result.get('adverse_selection_usd'),
               'quote_uptime_pct': result.get('quote_uptime_pct'),
               'inventory_max_usd': result.get('inventory_max_usd')}
        if trades < MIN_TRADES_TRUSTED:
            out['WARNING'] = (f'only {trades} fills; this row\'s net edge is a small-sample '
                              f'result and its rank should not be trusted')
        return out

    results = [row_of('baseline', base_config, run(dict(base_config), rows, tape, profile))]
    for combo in combos:
        config = dict(base_config)
        config.update(dict(zip(keys, combo)))
        label = ' '.join(f'{k}={v}' for k, v in zip(keys, combo))
        results.append(row_of(label, config, run(config, rows, tape, profile)))

    ranked = sorted(results, key=lambda r: (r['net_edge_usd'] is not None,
                                            r['net_edge_usd'] or 0.0), reverse=True)

    out = {'strategy': os.path.basename(strategy_dir or ''), 'decide_source': source,
           'days': days, 'spec': spec, 'rows': len(rows), 'tape': len(tape),
           'combinations': len(combos), 'swept_keys': keys,
           'by_net_edge': ranked,
           'NOTE': ('the top row is the best of %d noisy draws and is biased upward by the '
                    'search that found it; prefer net_bp_per_fill, check trades, and treat '
                    'a win smaller than the spread between adjacent rows as a tie'
                    % (len(combos) + 1))}

    if split_halves and len(rows) >= 40:
        half = len(rows) // 2
        halves = []
        for name, seg in (('first', rows[:half]), ('second', rows[half:])):
            seg_tape = dex_trades.get_trades(
                spec=spec, start_ts=seg[0]['ts'],
                end_ts=seg[-1]['ts'] + maker_backtest.MAX_GAP_S, sides_only=True)
            halves.append((name, seg, seg_tape, maker_backtest._depth_profile(seg)))
        for entry in ranked[:max(1, int(split_top_n))]:
            config = dict(base_config)
            config.update({k: v for k, v in entry['config'].items() if v is not None})
            for name, seg, seg_tape, seg_profile in halves:
                res = run(config, seg, seg_tape, seg_profile)
                entry[f'net_{name}_half'] = res.get('net_edge_usd')
        out['SPLIT_NOTE'] = ('net_first_half / net_second_half are on the top rows only. A '
                             'setting that is positive in one half and negative in the '
                             'other is a bet on a regime, not an edge -- prefer a row that '
                             'is positive in both even if its total is lower')
    return out


def _main(argv):
    """CLI: maker_sweep.py <strategy_dir> <key>=<v1,v2,...> [<key>=...] [--days N] [--split]"""
    import json

    args = [a for a in argv if not a.startswith('--')]
    if len(args) < 2:
        print(_main.__doc__)
        return 2
    days = 7
    for a in argv:
        if a.startswith('--days'):
            days = float(a.split('=', 1)[1]) if '=' in a else days
    grid = {}
    for spec in args[1:]:
        if '=' not in spec:
            print(f'not a <key>=<v1,v2,...> spec: {spec}')
            return 2
        key, raw = spec.split('=', 1)
        try:
            grid[key] = [float(v) for v in raw.split(',') if v != '']
        except ValueError:
            print(f'non-numeric value in {spec}')
            return 2
    print(json.dumps(sweep(args[0], grid, days=days, split_halves='--split' in argv),
                     indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv[1:]))
