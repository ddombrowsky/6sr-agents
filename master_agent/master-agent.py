import json
import os
import re
import sys
import time
from pathlib import Path

from ollama import Client, ResponseError

import score
import sr_agent_tools

MODEL_NICKNAMES = {
    'gpt': 'gpt-oss:120b-cloud',
    'qwen': 'qwen3.5',
    'buck': 'wonderful_buck_321/sixsr',
}
# Pick the model with the MASTER_AGENT_MODEL env var (a nickname above) rather than
# editing this literal: successive emperor passes kept flipping it between 'gpt' and
# 'qwen' and overwriting each other's choice. 'qwen' is the current deliberate default.
MODEL = MODEL_NICKNAMES.get(os.environ.get('MASTER_AGENT_MODEL', 'qwen'), MODEL_NICKNAMES['qwen'])
SELF_FILE = os.path.abspath(__file__)
TOOLS_FILE = os.path.join(os.path.dirname(SELF_FILE), 'tools.json')
TOOLS_MODULE_FILE = os.path.abspath(sr_agent_tools.__file__)

STRATEGIES_DIR = Path('/opt/strategies')
TRADES_DIR = Path('/opt/trades')
REVISION_HISTORY_FILENAME = '.strategy-revision-history.json'
REVISION_HISTORY_MAX_MESSAGES = 5

client = Client(
    host="http://172.17.0.1:11434",
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

TOOLS = sr_agent_tools.TOOLS

with open(TOOLS_FILE) as f:
    TOOL_SCHEMAS = json.load(f)


def _watched_mtimes() -> dict:
    """mtimes of files that define tool implementations/schemas.

    Compared against a startup snapshot so a tool that edits its own
    definitions (e.g. via write_file) can trigger a re-exec that picks up
    the change instead of running stale in-memory code.
    """
    paths = [TOOLS_FILE, TOOLS_MODULE_FILE]
    return {p: os.path.getmtime(p) for p in paths if os.path.exists(p)}


_STARTUP_MTIMES = _watched_mtimes()


def dispatch(tool_call) -> dict:
    name = tool_call['function']['name']
    args = tool_call['function']['arguments']
    print(f'  -> {name}({", ".join(f"{k}={v!r}" for k, v in args.items())})')
    fn = TOOLS.get(name)
    if not fn:
        result = f'error: unknown tool {name}'
    else:
        try:
            result = fn(**args)
        except Exception as e:
            result = f'error: {type(e).__name__}: {e}'
    result = str(result)
    shown = result if len(result) <= 200 else result[:200] + '...'
    print(f'  <- {shown}')
    return {'role': 'tool', 'name': name, 'content': result}


_OVERFLOW_RE = re.compile(r'exceeded max context length by (\d+) tokens')


def _truncate_messages(messages: list, error_text: str) -> bool:
    """Drop the oldest non-system messages to shrink the prompt.

    Keeps the system message (if any) and the most recent message (the one
    that triggered this turn) intact. Returns False if there's nothing left
    to drop.
    """
    keep_from = 1 if messages and messages[0].get('role') == 'system' else 0
    droppable = len(messages) - 1 - keep_from  # never drop the last message
    if droppable <= 0:
        return False

    match = _OVERFLOW_RE.search(error_text)
    if match:
        # ~4 chars/token, with a margin since this is a rough estimate.
        target_chars = int(match.group(1)) * 4 * 1.2
        removed_chars = 0
        removed = 0
        for msg in messages[keep_from:keep_from + droppable]:
            removed_chars += len(str(msg.get('content') or ''))
            removed += 1
            if removed_chars >= target_chars:
                break
    else:
        removed = max(1, droppable // 2)

    del messages[keep_from:keep_from + removed]
    print(f'[warning] prompt too long; dropped {removed} older message(s) and retrying')
    return True


SERVER_ERROR_MAX_RETRIES = 10
SERVER_ERROR_RETRY_DELAY = 10  # seconds


def _estimate_context_tokens(messages: list) -> int:
    """Rough token estimate (~4 chars/token) of the current message list."""
    chars = sum(len(str(msg.get('content') or '')) for msg in messages)
    return chars // 4


def run_turn(messages: list) -> str:
    server_error_retries = 0
    while True:
        print('...')
        print(f'[info] estimated context size: ~{_estimate_context_tokens(messages)} tokens')
        try:
            response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS)
        except ResponseError as e:
            error_text = str(e)
            if 'prompt too long' in error_text.lower() and _truncate_messages(messages, error_text):
                server_error_retries = 0
                continue
            if e.status_code == 429:
                # Ollama cloud's weekly quota. Nothing to retry against -- it resets on
                # its own schedule. monitor.py already falls back to a random tweak when
                # a revision fails, so return a one-line reason instead of letting a
                # 25-line traceback per clone per cycle fill the monitor log.
                print(f'[warning] model quota/rate limit hit: {error_text}')
                return f'[error: model quota exhausted ({error_text})]'
            if e.status_code >= 500:
                server_error_retries += 1
                if server_error_retries > SERVER_ERROR_MAX_RETRIES:
                    print(f'[warning] server error persisted after {SERVER_ERROR_MAX_RETRIES} retries, giving up on this turn: {error_text}')
                    return f'[error: server returned "{error_text}" after {SERVER_ERROR_MAX_RETRIES} retries try again]'
                print(f'[warning] server error ({error_text}); retrying in {SERVER_ERROR_RETRY_DELAY}s ({server_error_retries}/{SERVER_ERROR_MAX_RETRIES})...')
                time.sleep(SERVER_ERROR_RETRY_DELAY)
                continue
            raise
        server_error_retries = 0
        message = response['message']
        if hasattr(message, 'model_dump'):
            message = message.model_dump(exclude_none=True)
        messages.append(message)

        tool_calls = message.get('tool_calls')
        if not tool_calls:
            return message['content']

        for call in tool_calls:
            messages.append(dispatch(call))


def _handle_model_command(user_input: str) -> bool:
    """If user_input is a `/model <nickname>` command, apply it and return True."""
    global MODEL
    parts = user_input.strip().split()
    if not parts or parts[0] != '/model':
        return False
    if len(parts) != 2:
        print(f'usage: /model <nickname>  (available: {", ".join(MODEL_NICKNAMES)})')
        return True
    nickname = parts[1]
    model = MODEL_NICKNAMES.get(nickname)
    if model is None:
        print(f'[error] unknown model nickname {nickname!r} (available: {", ".join(MODEL_NICKNAMES)})')
        return True
    MODEL = model
    print(f'[info] switched model to {nickname!r} ({MODEL})')
    return True


REVISION_SYSTEM_PROMPT = (
    'You are the strategy-revision agent for an evolutionary XLM paper-trading system. '
    'Each cycle, monitor.py clones the best-performing strategies and hands you the fresh '
    "clone to improve before it starts trading. You have full read/write/exec access to "
    "the clone's directory and may change anything about it: config.json thresholds, "
    "main.py's trading logic, or add new files entirely. Use what you know about how this "
    'strategy and its ancestors have performed to decide what to change and why.\n\n'
    "main.py's trading logic has two parts: a decide step (price threshold checks, or "
    "whatever signal logic you wire in, producing a trade decision -- side, a label, "
    "and how much USD to request) and execution, handled by "
    "trade_logger.execute_trade(agent_name, action, side, price, requested_usd, state) "
    "in /opt/tools/trade_logger.py. Freely rewrite the decide step -- that's the actual "
    "strategy. Do NOT reimplement balance mutation, overdraft/oversell clamping, trade "
    "logging, or live-trade submission inside main.py -- execute_trade already does all "
    "of that, including checking whether this strategy is live and calling "
    "stellar_trader.submit_trade for you. Call it with your decided side ('buy' or "
    "'sell') and requested_usd; pass any action label you like (e.g. 'sell_stoploss') "
    "for the trade log without affecting execution. Do not import stellar_trader or "
    "read/write live.flag directly from main.py -- that plumbing is intentionally kept "
    "outside the clone you're editing; reimplementing or routing around it risks "
    "double-submitting real trades or bypassing stellar_trader.py's safety caps.\n\n"
    # Interpolated, never restated as a literal: this sentence claimed 0.999 for a long
    # time while score.py actually enforced 0.899, so the model was optimizing a
    # different objective than the one it was ranked on.
    f'You are scored on net worth: `balance_usd + balance_xlm * price * '
    f'{score.UNREALIZED_HAIRCUT}` (see /opt/master_agent/score.py). The '
    f'{(1 - score.UNREALIZED_HAIRCUT) * 100:.1f}% haircut on the XLM leg is only a '
    'tie-break nudge toward realizing gains -- it is not worth distorting the strategy for. '
    'Growing net worth is the entire objective; sitting in cash and never trading '
    'scores exactly the starting balance and gets you nowhere.\n\n'
    'You can and should test a revision before committing it. '
    '`backtest_strategy(strategy_path)` replays the strategy over 30 days of real '
    'hourly candles and returns return_pct, buy_hold_pct, beats_buy_hold, trades, '
    'win_rate and max_drawdown_pct in a second or two -- use it as your fitness check '
    'instead of guessing, and iterate until the numbers improve. Treat '
    '`beats_buy_hold: false` as a failed revision and try something else: a strategy '
    'that loses to simply holding XLM is not worth starting. The backtester picks up '
    "your logic automatically if main.py exposes a top-level "
    '`decide(price, history, state, config)` returning `(side, action, requested_usd)` '
    "or None; if it doesn't, it falls back to the plain buy_below/sell_above rule and "
    'will not see your changes at all. So structure main.py that way: put the decide '
    'step in that function and have the trading loop call it. `history` is the list of '
    'recent close prices, oldest first, so indicators work unchanged in both live and '
    'backtest paths.\n\n'
    'ASSETS. The strategy trades XLM plus UP TO 2 additional Stellar assets, listed in '
    "config.json's `assets` array (XLM is never listed there -- it is the permanent base "
    'leg, carried by the top-level buy_below/sell_above/trade_amount_usd). Each entry is '
    '{"code", "issuer", "buy_below", "sell_above", "trade_amount_usd"}. You may add, '
    'change or remove those extra assets; you cannot remove XLM.\n'
    '  * A Stellar asset is the PAIR (code, issuer). The code alone is meaningless: '
    'anyone can issue an asset with code USDC or AQUA from their own account, and '
    'impostors are live on the network right now -- there are three different issuers of '
    '"AQUA", with 191603, 96 and 47 holders. Always write both fields.\n'
    '  * NEVER write an issuer address from memory. Your recollection of an issuer is '
    'exactly what an attacker impersonates. Get it from `list_candidate_assets` or '
    '`verify_asset`, and copy the full 56-character G... address verbatim.\n'
    '  * Your asset choice is a PROPOSAL, not a decision. monitor.py re-verifies every '
    'asset before the clone starts and again on later cycles, and silently deletes any '
    'that fails -- including one that was fine when you picked it and has since lost its '
    'liquidity. A strategy whose entire thesis rests on one exotic asset will end up an '
    'XLM-only strategy. Call `verify_asset` before you commit, and prefer assets that '
    'pass it comfortably.\n'
    '  * Extra assets trade only on Stellar\'s own DEX: no centralized-exchange price, '
    'much thinner books, wider spreads, and short/gappy history. Size them well below '
    'your XLM leg. Scoring marks them at what the live bid side could actually absorb '
    'and applies an additional illiquidity haircut, so a large position in a thin asset '
    'is scored at what it could really be sold for, not at its quoted price.\n'
    '  * Put per-asset logic in an optional `decide_asset(asset, price, history, state, '
    'config)` returning the same (side, action, requested_usd) 3-tuple or None; it is '
    'called once per extra asset per tick with that asset\'s own price history. If you '
    'omit it, each leg just uses its own thresholds from config.json. Keep `decide` for '
    'the XLM leg -- that is what backtest_strategy replays and what beats_buy_hold is '
    'measured on. Extra legs are reported separately and over a shorter, less reliable '
    'window, so do not over-fit to them.\n'
    '  * Balances now live in `state["positions"]` keyed by asset, but execute_trade '
    'still maintains them -- you never touch them yourself. Pass the asset as a keyword: '
    "execute_trade(agent_name, action, side, price, requested_usd, state, "
    "asset='CODE:ISSUER').\n"
    '  * Real-money trading is currently XLM-only. A non-XLM leg is always paper, even '
    'on the live strategy, so do not build a strategy that depends on a real fill in a '
    'discovered asset.\n\n'
    "Do not default to only nudging buy_below/sell_above. Threshold tweaks are the "
    "weakest lever available to you -- treat them as a last resort, not the first move. "
    "/opt/tools has indicator and signal modules you can import from a strategy's "
    "main.py: ema_sma.py (SMA, `sma(prices, period)` -- note it was a truncated stub "
    "returning None for every sufficient-data case until recently, and its companion "
    "`exponential_moving_average` did not exist at all; both work now), "
    'moving_averages.py (EMA, `exponential_moving_average(prices, period)`), rsi.py '
    '(`rsi(prices, period)`), ohlc_history.py (`closes(hours=720)` / '
    '`get_candles(hours, interval)` -- ~30 days of REAL historical hourly OHLCV candles '
    'from Kraken/Coinbase, cached and shared. Reach for this whenever an indicator needs '
    'lookback: a freshly-cloned strategy has no price history of its own, so an EMA/RSI '
    'fed from the live sample buffer returns nan for hours after it starts, while this '
    'gives it 720 usable bars on its very first tick), '
    'price_history_fetcher.py (`get_price_samples(lookback_cycles)` '
    '-- a shared, cached buffer of recent *live* spot samples; fine for the last few '
    'ticks, but too short for indicator lookback -- use ohlc_history.py for that), '
    'news_feed.py (`get_headlines()` / '
    '`sentiment_score()` -- keyword-heuristic bullish/bearish scoring from recent crypto '
    'news, a coarse but genuinely different signal from price alone), '
    "reflector_oracle.py (an alternate on-chain price source -- now wired as a fallback "
    "inside price_feed.py itself, so you don't need to call it directly unless you "
    'specifically want to cross-check the DEX-oracle price against the CEX-aggregate '
    "one), and orderbook_depth.py (`get_orderbook_metrics()` -- live XLM/USDC order "
    'book from the Stellar DEX: best bid/ask, spread, and USD depth/imbalance on each '
    'side, a liquidity signal distinct from price or sentiment; a wide spread means '
    'higher slippage risk right now, a lopsided imbalance means resting supply/demand '
    'is skewed). None of the indicator/signal modules are wired into template_repo\'s main.py '
    'by default -- that wiring is exactly the kind of change you should be making. Prefer '
    'changes like: wiring in an indicator or the news sentiment score to gate or size '
    'trades, adding a stop-loss/take-profit rule, changing position sizing or order '
    'cadence, combining multiple signals, or trying a structurally different strategy '
    "shape -- something that would show up as a real diff in main.py, not just its "
    'config.json numbers. Look at the leaderboard and this strategy lineage\'s revision '
    'history (recent messages below, if any) to avoid repeating a variant that a sibling '
    'clone already tried.\n\n'
    'Every revision prompt includes the real current XLM/USD price, freshly fetched by '
    'monitor.py right before invoking you. Treat that number as ground truth and set '
    'buy_below/sell_above relative to it -- do NOT rely on your own training-data notion '
    "of what XLM \"typically\" costs; that knowledge may be stale or wrong, and thresholds "
    'set far away from the real current price will simply never trigger a trade (or will '
    "trigger on every single tick). If you ever need to double check the price yourself "
    '(e.g. to look at recent history or trend, not just the single spot value you were '
    "given), you have `exec` (curl, or run /opt/tools/price_feed.py) and `fetch_url` -- "
    'use them rather than guessing.\n\n'
    'When you are done, you MUST commit your changes on a new git branch inside the '
    "strategy's own directory (e.g. `git checkout -b auto/<timestamp>` then `git add -A "
    '&& git commit -m ...`) so the revision is tracked -- an unmodified or uncommitted '
    "clone will just keep trading with its parent's exact settings.\n\n"
    'Finish by replying with a short summary of what you changed and why.'
)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _read_text(path) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return '(could not read file)'


def _tail_lines(path, n=20) -> str:
    try:
        with open(path) as f:
            return ''.join(f.readlines()[-n:]) or '(empty trade log)'
    except Exception:
        return '(no trade log yet)'


def _load_revision_history(strategy_path: Path) -> list:
    history_file = strategy_path / REVISION_HISTORY_FILENAME
    if history_file.exists():
        with open(history_file) as f:
            return json.load(f)
    return [{'role': 'system', 'content': REVISION_SYSTEM_PROMPT}]


def _save_revision_history(strategy_path: Path, messages: list) -> None:
    """Persist history to the strategy's own directory, capped to the last
    REVISION_HISTORY_MAX_MESSAGES messages (plus the leading system message,
    if present) so it doesn't grow unbounded across revision cycles.
    """
    keep_from = 1 if messages and messages[0].get('role') == 'system' else 0
    trimmed = messages[:keep_from] + messages[keep_from:][-REVISION_HISTORY_MAX_MESSAGES:]
    history_file = strategy_path / REVISION_HISTORY_FILENAME
    with open(history_file, 'w') as f:
        json.dump(trimmed, f)


def revise_strategy(strategy_name: str, parent_name: str, parent_score: str = '',
                     leaderboard_json: str = '{}', current_price: str = '') -> None:
    """One-shot entry point invoked by monitor.py's tweak stage.

    Hands a freshly-cloned, not-yet-started strategy directory to the LLM with full
    tool access so it can rewrite config/code and commit the revision itself, instead
    of monitor.py applying a fixed random tweak.
    """
    strategy_path = STRATEGIES_DIR / strategy_name
    parent_path = STRATEGIES_DIR / parent_name
    parent_config = _read_json(parent_path / 'config.json', {})
    parent_state = _read_json(parent_path / 'state.json', {})
    trade_tail = _tail_lines(TRADES_DIR / f'{parent_name}.log')
    clone_main_py = _read_text(strategy_path / 'main.py')
    try:
        leaderboard = json.loads(leaderboard_json)
    except Exception:
        leaderboard = {}

    price_line = (
        f'Current XLM/USD price (fetched from CoinGecko by monitor.py moments ago, '
        f'this is ground truth -- not a historical or typical price): ${current_price}\n'
        if current_price else
        'Current XLM/USD price: NOT PROVIDED for this cycle -- fetch it yourself '
        '(exec curl, or /opt/tools/price_feed.py) before setting any thresholds.\n'
    )

    prompt = (
        f'A new clone `{strategy_name}` of `{parent_name}` was just created at '
        f'`{strategy_path}` (a git checkout of the strategy code). It has not started '
        f'trading yet.\n\n'
        f'{price_line}'
        f"Parent `{parent_name}`'s config.json: {json.dumps(parent_config)}\n"
        f"Parent `{parent_name}`'s current state.json: {json.dumps(parent_state)}\n"
        f"Parent `{parent_name}`'s score this cycle: {parent_score}\n"
        f"Parent `{parent_name}`'s most recent trades:\n{trade_tail}\n\n"
        f"The clone's main.py (identical to the parent's right now -- this is what you'd "
        f"edit to change trading logic, not just config.json):\n```python\n{clone_main_py}\n```\n\n"
        f'Current leaderboard (strategy name -> score, all strategies currently '
        f'running, including any you revised in previous cycles): {json.dumps(leaderboard)}\n\n'
        f'Revise the clone at `{strategy_path}` however you think will improve on its '
        f'parent, then commit your changes to a new git branch inside that directory. '
        f'Any buy_below/sell_above you set must be anchored to the current price above, '
        f'not to an assumed or remembered price level.'
    )

    messages = _load_revision_history(strategy_path)
    messages.append({'role': 'user', 'content': prompt})
    reply = run_turn(messages)
    _save_revision_history(strategy_path, messages)
    print(reply)
    if reply.startswith('[error:'):
        # run_turn gave up (quota exhausted, or server errors past the retry budget)
        # rather than actually revising anything. Exit non-zero so monitor.py's caller
        # applies its random-tweak fallback -- without this the clone would silently
        # start as a byte-identical copy of its parent.
        print(reply, file=sys.stderr)  # monitor.py logs stderr for a failed revision
        sys.exit(1)


def main():
    messages = [{
          'role': 'system',
          'content': (
              'You are a monitoring agent.  Your job is to monitor the trading bots '
              'that are successful, and update the strategies to optimize income. '
              'You can use any tool at your disposal, including fetching information '
              'from the internet to predict price movement.  You may update yourself '
              'as well.'
          )
    }]
    print("Agent ready. Type 'exit' to quit.")
    while True:
        user_input = input('> ')
        if not user_input.strip():
            continue
        if user_input.strip().lower() in ('exit', 'quit'):
            break
        if _handle_model_command(user_input):
            continue
        messages.append({'role': 'user', 'content': user_input})
        reply = run_turn(messages)
        print(reply)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'revise-strategy':
        revise_strategy(*sys.argv[2:])
    else:
        main()
