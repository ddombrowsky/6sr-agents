import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from ollama import Client, ResponseError

import domain
import sr_agent_tools

# Which game this process is revising strategies for, and every number about it that the
# prompt below states. Read live out of the modules that enforce them (score.py's haircut,
# friction.py's round-trip cost, stellar_trader.py's caps) rather than written here as
# literals: the prompt claimed the haircut was 0.999 for weeks while score.py enforced
# 0.899, so the model was optimizing an objective it was not ranked on, and a cost the
# prompt states while the code charges something else is the same bug. See
# domain_sdex.prompt_facts, which keeps the same per-source fallbacks this file used to
# carry inline so a /opt/tools outage degrades the prompt instead of killing the process.
#
# Resolved at import, once, so REVISION_SYSTEM_PROMPT is still a module-level string.
_DOMAIN = domain.get()
_FACTS = _DOMAIN.prompt_facts()

MODEL_NICKNAMES = {
    'gpt': 'gpt-oss:120b-cloud',
    'qwen': 'qwen3.5',
    'buck': 'wonderful_buck_321/sixsr',
    'granite': 'granite4.1:8b',
}
# Pick the model with the MASTER_AGENT_MODEL env var (a nickname above) rather than
# editing this literal: successive emperor passes kept flipping it between 'gpt' and
# 'qwen' and overwriting each other's choice. 'gpt' is the current deliberate default.
MODEL = MODEL_NICKNAMES.get(os.environ.get('MASTER_AGENT_MODEL', 'gpt'), MODEL_NICKNAMES['gpt'])
SELF_FILE = os.path.abspath(__file__)
TOOLS_FILE = os.path.join(os.path.dirname(SELF_FILE), 'tools.json')
TOOLS_MODULE_FILE = os.path.abspath(sr_agent_tools.__file__)

STRATEGIES_DIR = Path('/opt/strategies')
TRADES_DIR = Path('/opt/trades')
STRATEGY_STATE_FILE = Path('/opt/strategy_state.json')
REVISION_HISTORY_FILENAME = '.strategy-revision-history.json'
REVISION_HISTORY_MAX_MESSAGES = 5

# How many times revise_strategy will send the model back into the same conversation when
# its reply did not correspond to any actual change on disk. 3 means the first answer plus
# two corrections.
#
# This exists because "the model analysed the strategy and then wrote nothing" is the
# single most common way a revision slot is wasted. Over the four emperor cycles logged to
# 2026-08-05 there were 17 revisions and 9 write_file calls: 8 revisions ended with a prose
# summary quoting a config.json block, a main.py diff and a `git commit` command, none of
# which had been executed -- one closing with "the revision is now ready for deployment"
# over a byte-identical clone. monitor.py already detects that (revision_changed_anything),
# but its only response is to discard the attempt and apply a random threshold tweak, so
# the model is never told it failed and the cycle's analysis is thrown away.
#
# Retrying here rather than re-invoking from monitor is deliberate: the model still has its
# whole tool-calling context -- the regime read, the backtest, the candidate thresholds --
# so a correction costs one completion instead of a fresh 10K-token prompt and another
# round of read-only tool calls. Env-overridable because each extra attempt is another
# model turn inside REVISION_TIMEOUT.
REVISION_MAX_ATTEMPTS = int(os.environ.get('REVISION_MAX_ATTEMPTS', '3'))

# The third-party package manifest, named in REVISION_SYSTEM_PROMPT so the revision model
# can read the current list instead of trusting a list hardcoded in a prompt that goes
# stale the next time someone pip-installs something. Overridable because this file is
# also run from the repo root outside the container, where /opt does not exist.
REQUIREMENTS_PATH = os.environ.get('REQUIREMENTS_PATH', '/opt/agents/requirements.txt')

# The two jobs a newcomer can be handed, passed by monitor.py as the 6th argv of
# `revise-strategy`. `refine` is a clone of a leader: improve on a parent with a real
# record. `explore` is a fresh template spawn with no meaningful parent, asked for
# something structurally different from what the population already runs.
#
# These used to be hand-duplicated here and in monitor.py, with a comment asking whoever
# read it to keep the two pairs in sync -- because this file's name has a hyphen and
# cannot be imported, so there was nowhere shared to put them. domain.py can be imported
# by both, so there is now exactly one definition. Keeping two copies of a constant in
# sync by comment is how the prompt's haircut ended up two orders of magnitude away from
# the one score.py enforced.
ROLE_REFINE = domain.ROLE_REFINE
ROLE_EXPLORE = domain.ROLE_EXPLORE

# How many distinct main.py variants _population_census describes to an explore spawn.
CENSUS_CLUSTERS = 6

# The /opt/tools modules a strategy would import for *signal*, as opposed to the
# infrastructure every strategy already uses (trade_logger, price_feed, portfolio,
# dex_price, assets). The census reports which of these nobody imports, which is the
# concrete list of directions the population has not tried.
SIGNAL_MODULES = (
    'basis', 'ema_sma', 'ema_sma_complete', 'friction', 'market_recorder',
    'moving_averages', 'news_feed', 'ohlc_history', 'orderbook_depth',
    'price_history_fetcher', 'reflector_oracle', 'rsi',
)

# The config.json keys the system itself defines. Everything else in a config was
# invented by some revision, and the census reports those back so an explore spawn can
# see which parameters have already been tried before inventing another.
STANDARD_CONFIG_KEYS = frozenset((
    'name', 'schema_version', 'buy_below', 'sell_above', 'trade_amount_usd', 'assets',
    'news_veto_below', 'basis_min_bp',
))

_API_KEY = os.environ.get('OLLAMA_API_KEY')
if not _API_KEY:
    # Previously this line was `'Bearer ' + os.environ.get('OLLAMA_API_KEY')`, which
    # raised `TypeError: can only concatenate str (not "NoneType") to str` at import --
    # before argument parsing, before any error handling, and with a traceback that named
    # the concatenation rather than the missing key. monitor.py logs that stderr and
    # falls back to a random tweak, so a forgotten `. ./env.sh` looked exactly like a
    # model failure. Say what is actually wrong.
    sys.exit('[error: OLLAMA_API_KEY is not set -- source /opt/env.sh before running '
             'monitor.py or master-agent.py; every revision will fail without it]')

client = Client(
    host="http://172.17.0.1:11434",
    headers={'Authorization': 'Bearer ' + _API_KEY}
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

# Ceilings on the tool calls one run_turn may make. A run that hits either is
# degenerating, not working: revising clone_be878323c208 on 2026-08-03 took 36 minutes
# and 32 tool calls, rewriting main.py 7 times into progressively less coherent Python
# (`float('nan')[:period-1]`, `decide` defined twice, a reference to `pricas`) and
# writing config.json 21 times -- the last 9 byte-identical to each other. Nothing
# looked at that; the loop was `while True` and ended only because the model eventually
# stopped on its own. The identical repeats are the sharper signal, hence the much
# tighter limit: one duplicated call is a recoverable hiccup, a third in a row is a stuck
# model. The total budget is the backstop for a model that thrashes without repeating
# itself exactly. Replaying that transcript against these values aborts at tool call 19
# of 32; at a repeat limit of 4 it only reaches back to 26, and at 2 it would fire on
# call 2 of every run that ever double-writes a file.
#
# Hitting either returns an '[error: ...]' reply, so revise_strategy exits non-zero and
# monitor.py applies its random-tweak fallback -- the same path quota exhaustion and
# persistent server errors already take. Whatever the model wrote before giving up stays
# on disk and still faces monitor's gates; this bounds the wasted wall clock, it does not
# decide whether the work was any good.
MAX_TOOL_CALLS = 25
MAX_REPEATED_TOOL_CALLS = 3


def _estimate_context_tokens(messages: list) -> int:
    """Rough token estimate (~4 chars/token) of the current message list."""
    chars = sum(len(str(msg.get('content') or '')) for msg in messages)
    return chars // 4


def _call_signature(tool_call) -> str:
    """Stable key for 'the model just made this exact call again'."""
    fn = tool_call['function']
    try:
        return json.dumps([fn['name'], fn['arguments']], sort_keys=True, default=str)
    except Exception:
        return repr(fn)


def _abandon_turn(messages: list, tool_calls: list, reason: str) -> str:
    """Refuse the rest of this batch of tool calls and end the turn with an error.

    Every refused call still gets a tool message. The model's reply carrying those
    calls is already in `messages`, and a tool_call with no matching result would leave
    the saved revision history malformed for whatever loads it next.
    """
    for call in tool_calls:
        messages.append({'role': 'tool', 'name': call['function']['name'],
                         'content': f'error: refused, {reason}'})
    reply = f'[error: {reason}; abandoning this turn]'
    print(f'[warning] {reply}')
    messages.append({'role': 'assistant', 'content': reply})
    return reply


def run_turn(messages: list) -> str:
    server_error_retries = 0
    tool_calls_made = 0
    last_signature = None
    repeats = 0
    while True:
        print('...')
        print(f'[info] estimated context size: ~{_estimate_context_tokens(messages)} tokens')
        try:
            response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS, think=False)
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
        except httpx.TransportError as e:
            # The connection itself failed -- no HTTP status, so ResponseError above never
            # sees it and this used to escape as a raw traceback. Ollama drops the
            # connection while cold-loading a local model (observed 2026-08-04: a
            # granite4.1:8b load takes ~59s and the first request died at 3.4s with
            # RemoteProtocolError; the identical request succeeded once the model was
            # warm), and monitor.py's revision subprocess is exactly where that lands.
            #
            # Transient and worth the same retry budget as a 5xx: the failure mode is a
            # backend that is not ready yet, not a request that will never work. Sharing
            # the counter is deliberate -- a backend flapping between 500s and dropped
            # connections should still give up after SERVER_ERROR_MAX_RETRIES rather than
            # alternating between two budgets forever.
            error_text = f'{type(e).__name__}: {e}'
            server_error_retries += 1
            if server_error_retries > SERVER_ERROR_MAX_RETRIES:
                print(f'[warning] transport error persisted after {SERVER_ERROR_MAX_RETRIES} retries, giving up on this turn: {error_text}')
                # An '[error: ...]' reply is the contract monitor.py's fallback depends
                # on: revise_strategy exits non-zero and the clone gets a random tweak.
                # A traceback here exits non-zero too, but by crashing, which reads as a
                # bug in the agent rather than an unreachable model.
                return f'[error: could not reach the model ({error_text}) after {SERVER_ERROR_MAX_RETRIES} retries]'
            print(f'[warning] transport error ({error_text}); retrying in {SERVER_ERROR_RETRY_DELAY}s ({server_error_retries}/{SERVER_ERROR_MAX_RETRIES})...')
            time.sleep(SERVER_ERROR_RETRY_DELAY)
            continue
        server_error_retries = 0
        message = response['message']
        if hasattr(message, 'model_dump'):
            message = message.model_dump(exclude_none=True)
        messages.append(message)

        tool_calls = message.get('tool_calls')
        if not tool_calls:
            return message['content']

        for i, call in enumerate(tool_calls):
            signature = _call_signature(call)
            repeats = repeats + 1 if signature == last_signature else 1
            last_signature = signature
            tool_calls_made += 1
            if repeats >= MAX_REPEATED_TOOL_CALLS:
                return _abandon_turn(messages, tool_calls[i:],
                                     f'the same tool call was made {repeats} times in a row')
            if tool_calls_made > MAX_TOOL_CALLS:
                return _abandon_turn(messages, tool_calls[i:],
                                     f'the {MAX_TOOL_CALLS}-tool-call budget for one turn is exhausted')
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


# A function, not a bare module-level literal, for the same reason
# _build_forecast_revision_system_prompt() below is one: this reads _FACTS[...] keys
# that only domain_sdex.prompt_facts() provides, and _FACTS is whatever domain is
# ACTUALLY active. Deferred so it is only ever called for the sdex path (see the
# if/else at the bottom of this block).
#
# This text and _refine_prompt below used to be frozen character-for-character as a
# control arm for an explore-vs-refine comparison. That freeze was lifted 2026-08-11 for
# a token-diet pass: once granite4.1:8b (no thinking, 8.8B, ~10K tokens of system prompt
# alone) became the only model that runs automatically, a smaller, denser prompt is a
# bigger lever on revision quality than preserving that comparison's continuity.
def _build_sdex_revision_system_prompt():
    return (
    'You are the strategy-revision agent for an evolutionary XLM paper-trading system. '
    'Each cycle monitor.py creates a small batch of new strategies -- clones of the best '
    'current performers, plus one pulled fresh from the template -- and hands each of '
    'them to you before it starts trading. The user message tells you which job you have '
    'for this one: `refine` (improve on a parent with a real track record) or `explore` '
    '(a fresh template strategy with no meaningful parent, where the job is to try '
    'something the population is not already doing). Everything below applies to both. '
    "You have full read/write/exec access to the strategy's directory and may change "
    "anything about it: config.json thresholds, main.py's trading logic, or add new "
    "files entirely.\n\n"
    "main.py has two parts: a decide step (your trading logic, producing a trade "
    "decision -- side, a label, and how much USD to request) and execution, handled by "
    "trade_logger.execute_trade(agent_name, action, side, price, requested_usd, state) "
    "in /opt/tools/trade_logger.py. Freely rewrite the decide step -- that's the actual "
    "strategy. Do NOT reimplement balance mutation, overdraft/oversell clamping, trade "
    "logging, or live-trade submission inside main.py -- execute_trade already does all "
    "of that, including checking whether this strategy is live and calling "
    "stellar_trader.submit_trade for you. Call it with your decided side ('buy' or "
    "'sell') and requested_usd; the action label is free text for the trade log and does "
    "not affect execution. Never import stellar_trader or read/write live.flag directly "
    "from main.py -- that plumbing stays outside the clone you're editing; routing "
    "around it risks double-submitting real trades or bypassing its safety caps.\n\n"
    # Interpolated, never restated as a literal: this sentence claimed 0.999 for a long
    # time while score.py actually enforced 0.899, so the model was optimizing a
    # different objective than the one it was ranked on.
    f'You are scored on net worth: `balance_usd + balance_xlm * price * '
    f'{_FACTS["unrealized_haircut"]}` (see /opt/master_agent/score.py). The '
    f'{(1 - _FACTS["unrealized_haircut"]) * 100:.1f}% haircut on the XLM leg is only a '
    'tie-break nudge toward realizing gains -- it is not worth distorting the strategy for. '
    'Growing net worth is the entire objective; sitting in cash and never trading '
    'scores exactly the starting balance and gets you nowhere.\n\n'
    'TRADING COSTS MONEY. Every fill crosses the order book: a buy lifts the ask, a '
    f'sell hits the bid. Right now a full buy-then-sell round trip in XLM costs about '
    f'{_FACTS["xlm_round_trip_bp"]} basis points, and in a discovered non-XLM asset it is '
    f'far worse -- assume at least {_FACTS["nonbase_round_trip_bp"]} bp and check with '
    '`friction.round_trip_bp`. This is charged for real: trade_logger.execute_trade '
    'fills you at the adjusted price and backtest_strategy replays it the same way, so '
    'it is already inside every number you are judged on. Consequences:\n'
    '  * A threshold band narrower than the round-trip cost CANNOT be profitable, no '
    'matter how often it is right. Check that `sell_above - buy_below` comfortably '
    'exceeds it.\n'
    '  * Turnover is a cost, not an achievement. Doubling your trade count doubles what '
    'you pay, not your edge. `backtest_strategy` returns `total_friction_usd` next to '
    '`return_pct` -- if it eats a large fraction of your gain, trade less and better, '
    'not more.\n'
    '  * Being selective is a real strategy: a filter that skips marginal trades keeps '
    'the cost you would have paid on them, which is why an indicator or a regime gate '
    'can beat a bare threshold bot even when it is wrong as often.\n\n'
    # Added 2026-08-05. The population had converged on strategies that cannot act at
    # all: 8 of 34 had never placed a single trade (one of them 33 hours old), and they
    # held the top of the leaderboard, because a strategy that never trades holds exactly
    # its $1000 starting balance while XLM was down 15.7% over the window. The model was
    # not being perverse -- it was optimizing what it was told to, and "beat buy-and-hold"
    # is trivially satisfied by doing nothing in a falling market. One revision said so in
    # its own summary: "Trades executed 0 times, which is expected ... however, the
    # beats_buy_hold flag returned true".
    'YOUR STRATEGY MUST BE ABLE TO ACT. Not trading is not a safe default -- it is the '
    'most common way a revision fails, and it fails silently, because a strategy that '
    'never places an order just holds its starting balance and looks fine. Two dead '
    'ends:\n'
    '  * STERILE -- buy_below/sell_above never intersect the market, so it buys '
    'nothing, ever.\n'
    '  * ONE-WAY -- it buys until cash runs out and then waits for a sell_above the '
    'price may never reach. That is not a strategy, it is a leveraged opinion about '
    'XLM.\n'
    'So: **`trades: 0` from backtest_strategy is a FAILED revision, whatever '
    '`beats_buy_hold` says.** In a falling market doing nothing beats holding, so those '
    'two fields can both look good on a strategy that will never place an order. '
    'monitor.py rejects a revision that replays zero trades over 30 days and falls back '
    'to a mechanically-generated config instead, so shipping one wastes the slot '
    'entirely.\n'
    'The tool that answers this directly is `get_market_regime`. Pass your candidate '
    'buy_below and sell_above and it replays them over the real candles and tells you how '
    'many buys, sells and completed round trips they would have made, with a verdict of '
    'STERILE / ONE-WAY / a round-trip count. It also reports the regime (trend, ATR per '
    'bar against the round-trip cost, where spot sits in its 30-day range) and a ranked '
    'grid of band widths. Check your thresholds with it before you commit them, and think '
    'about what your strategy does once it is fully invested -- if the only way back to '
    'cash needs a price it may never see, there is no way back.\n\n'
    'You can and should test a revision before committing it. '
    '`backtest_strategy(strategy_path)` replays the strategy over 30 days of real '
    'hourly candles and returns return_pct, buy_hold_pct, beats_buy_hold, trades, '
    'win_rate and max_drawdown_pct in a second or two -- use it as your fitness check '
    'instead of guessing, and iterate until the numbers improve.\n'
    '  * CHECK `decide_source` FIRST, before you read any other field. The backtester '
    'only replays your logic if main.py exposes a top-level '
    '`decide(price, history, state, config)` returning `(side, action, requested_usd)` '
    "or None, AND main.py's top level contains nothing but imports, assignments, defs, "
    "the docstring and an `if __name__ == '__main__'` guard (a bare "
    "`sys.path.append(...)` call, a top-level `print`, an `if`/`with` or the trading "
    'loop itself all disqualify it -- write `sys.path = sys.path + [\'/opt/tools\']` as '
    'an assignment instead). Otherwise `decide_source` comes back as \'config-thresholds\' '
    "and every number describes config.json's rule rather than your code. Fix the "
    'structure and re-run until `decide_source` is \'main.py:decide\'; a revision that '
    'never reaches its own fitness check has not been tested at all.\n'
    '  * Once `decide_source` is \'main.py:decide\', treat `beats_buy_hold: false` as a '
    'failed revision and try something else: a strategy that loses to simply holding '
    'XLM is not worth starting.\n'
    '  * Read `trades` in the same breath as `beats_buy_hold`, and read it FIRST -- see '
    '"YOUR STRATEGY MUST BE ABLE TO ACT" above.\n'
    '`history` is the list of recent close prices, oldest first, identical in shape to '
    'both live and backtest.\n\n'
    # Added 2026-08-03. /opt/tools/news_feed.py had existed unused for a long time:
    # 0 of 138 strategies imported it, and nothing told the model it was there. The
    # backtest caveat below is the important half -- a model that wires in news and
    # then trusts beats_buy_hold is measuring a signal the replay pinned at neutral.
    'NON-PRICE SIGNALS. `/opt/tools/news_feed.py` gives you crypto headlines and a '
    'keyword sentiment score in [-1, 1] (negative is bearish). The `get_news_sentiment` '
    'tool shows you what it reads right now, so you can size a threshold against real '
    'output instead of guessing. Two hard rules about how to wire it in:\n'
    '  * NEVER call the news feed (or any network fetch) from inside `decide()`. '
    'backtest.py calls decide() once per tick -- about 86,000 times for a 30-day replay '
    '-- so a fetch in there means 86,000 HTTP requests and every historical tick '
    "answered with today's headlines. Instead have the trading loop in `main()` fetch "
    "it once per tick and store it (`state['news_sentiment'] = ...`), and have decide() "
    "read it back with `state.get('news_sentiment', 0.0)`. template_repo/main.py shows "
    'the pattern.\n'
    '  * BE AWARE IT IS UNBACKTESTABLE. There is no headline archive, so that key is '
    'absent on every backtest tick and reads as 0.0 -- a good backtest is not evidence '
    'the news logic works, only score.py\'s real paper net worth judges it. Because of '
    'that, prefer sentiment as a VETO on trades your price logic already wants '
    '(`news_veto_below` in config.json), not a trigger that fires trades on its own -- a '
    'veto degrades safely if the feed is down, leaving the price rule you already '
    'backtested. Never veto SELLS: stranding a position is worse than missing an '
    'entry.\n\n'
    'ASSETS. The strategy trades XLM plus UP TO 2 additional Stellar assets, listed in '
    "config.json's `assets` array (XLM is never listed there -- it is the permanent base "
    'leg, carried by the top-level buy_below/sell_above/trade_amount_usd). Each entry is '
    '{"code", "issuer", "buy_below", "sell_above", "trade_amount_usd"}. You may add, '
    'change or remove those extra assets; you cannot remove XLM.\n'
    '  * A Stellar asset is the PAIR (code, issuer). The code alone is meaningless: '
    'anyone can issue an asset with code USDC or AQUA from their own account, and '
    'impostors of common codes are live on the network right now. Always write both '
    'fields.\n'
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
    'config)` returning the same (side, action, requested_usd) 3-tuple or None, called '
    'once per extra asset per tick under the same importability rules as `decide`. If '
    'you omit it, each leg just uses its own thresholds from config.json. Keep `decide` '
    'for the XLM leg -- that is what backtest_strategy replays and what beats_buy_hold '
    'is measured on. Extra legs are reported separately over a shorter, less reliable '
    'window, so do not over-fit to them.\n'
    '  * Balances now live in `state["positions"]` keyed by asset, but execute_trade '
    'still maintains them -- you never touch them yourself. Pass the asset as a keyword: '
    "execute_trade(agent_name, action, side, price, requested_usd, state, "
    "asset='CODE:ISSUER').\n"
    '  * Extra legs CAN trade real money, but only on the live strategy, only once '
    'monitor has opened a trustline for that asset at promotion, and only while the asset '
    'stays admitted -- otherwise the leg is paper-only, the normal case since only one '
    f'strategy is live at a time. Real non-XLM orders are clamped to '
    f'${_FACTS["max_trade_usd_nonbase"]:.2f} per trade against '
    f'${_FACTS["max_trade_usd"]:.2f} for XLM. Design for the paper book, and never build '
    'a strategy whose thesis depends on any single real fill landing.\n\n'
    "Do not default to only nudging buy_below/sell_above. Threshold tweaks are the "
    "weakest lever available to you -- treat them as a last resort, not the first move. "
    "/opt/tools has indicator and signal modules you can import from a strategy's "
    "main.py:\n"
    '  * ema_sma.py -- `sma(prices, period)`. moving_averages.py -- '
    '`exponential_moving_average(prices, period)`. rsi.py -- `rsi(prices, period)`.\n'
    '  * ohlc_history.py -- `closes(hours=720)` / `get_candles(hours, interval)`: ~30 '
    'days of REAL historical hourly OHLCV candles, cached and shared. Reach for this '
    'whenever an indicator needs lookback -- a freshly-cloned strategy has no price '
    'history of its own, so this gives it 720 usable bars on its very first tick, unlike '
    'price_history_fetcher.py\'s `get_price_samples(lookback_cycles)`, which is a short '
    'live-sample buffer, fine for the last few ticks but too short for lookback.\n'
    '  * news_feed.py -- `get_headlines()` / `sentiment_score()`: keyword-heuristic '
    'bullish/bearish scoring from recent crypto news, a coarse but genuinely different '
    'signal from price alone.\n'
    '  * reflector_oracle.py -- an alternate on-chain price source, already wired as a '
    'fallback inside price_feed.py, so you rarely need to call it directly.\n'
    '  * basis.py -- THE DEX/CEX BASIS. Every strategy here decides on price_feed\'s '
    'centralized-exchange aggregate but executes against the Stellar DEX order book, two '
    'different venues at two different prices. The gap is `basis_bp`; `tradeable_bp` is '
    'that gap MINUS the cost of crossing to capture it, and it is negative most of the '
    'time -- itself a useful signal for when NOT to trade. A strategy that already wants '
    'to buy can wait for the DEX to be the cheap venue instead of lifting a rich book, '
    'an improvement that requires predicting nothing. It arrives through `state`, '
    'exactly like news sentiment: main()\'s tick loop calls `basis.latest()` once per '
    'tick and sets `state["basis_bp"]`/`state["basis_tradeable_bp"]`; decide() reads '
    'them with a neutral `.get` default (absent means "unknown, do not block"). '
    'template_repo/main.py\'s `current_basis()`/`basis_ok()` is the worked example, and '
    'config.json\'s optional `basis_min_bp` is the knob that arms it -- SIZE IT FROM THE '
    'RECORDED DISTRIBUTION (`get_market_history`, pick a percentile of the real series; '
    'monitor seeds new strategies at the 25th and rejects any basis_min_bp above the '
    '95th), never a round number that sounds prudent, since tradeable_bp is negative '
    'most of the time and an unexamined threshold can end up blocking almost every buy. '
    'Do NOT call `basis.get_basis()`, `dex_is_cheap()` or `dex_is_rich()` from decide() '
    'or any per-tick code -- each does a live, uncached Horizon order-book request, and '
    "under backtest that's 86,000 requests answering every historical candle with "
    "today's number. Those are for one-off inspection, which is what the "
    'get_dex_cex_basis tool is.\n'
    '  * market_recorder.py -- `read_history(hours)` / `series(field, hours)`: a '
    'PER-MINUTE record of DEX book width, depth, basis and news sentiment. This is what '
    'makes basis logic REPLAYABLE, unlike news: backtest joins these rows to the candle '
    'grid, so call backtest_strategy with interval=1, days=0.5 and read '
    '`basis_coverage`/`basis_edge_excess_bp`/`beats_basis_null`, not `beats_buy_hold` '
    "(over half a day that's just whatever XLM did this morning). Use `tail(n)`, never "
    '`read_history()`, from a tick loop.\n'
    '  * friction.py -- `round_trip_bp(spec)`: size a threshold band against the real '
    'round-trip cost programmatically instead of hardcoding a number.\n'
    '  * regime.py -- `regime()`: trend, ATR per bar, realized vol, drawdown from the '
    '30-day high, and where spot sits in its 30-day range, all from cached candles with '
    'no network of its own; `band_stats(buy_below, sell_above)` replays a band and says '
    'whether it fires at all (the get_market_regime tool wraps this). Call '
    '`regime.regime()` once per tick in main() and read the trend from `state` in '
    'decide(), exactly like news sentiment -- every strategy in this population is '
    'long-only, so a regime read is the only way one can decline to be long.\n'
    '  * orderbook_depth.py -- `get_orderbook_metrics()`: live XLM/USDC best bid/ask, '
    'spread, and USD depth/imbalance on each side -- a liquidity signal distinct from '
    'price or sentiment; a wide spread means higher slippage risk, a lopsided imbalance '
    'means resting supply/demand is skewed.\n'
    'What a strategy may import is limited to the Python standard library, the modules '
    'in /opt/tools, and whatever is actually installed in the interpreter that runs it. '
    'DO NOT ASSUME a third-party package is present, and do not assume it is absent '
    f'either -- the installed set changes. Read {REQUIREMENTS_PATH} with `read_file` to '
    'see what has been installed; before depending on a package, confirm the import '
    'actually resolves for a strategy process the way strategies are run -- `exec` with '
    '`python3 -c "import numpy"` (substitute the package) and check it exits 0. That '
    'probe is authoritative and the manifest is not, because a package can be installed '
    'into a different interpreter than the one that starts main.py. If you want '
    'something the manifest does not list, `install_package` it and then run the same '
    'probe. A main.py that imports an unresolvable module raises ModuleNotFoundError on '
    'its first tick, fails the smoke test, and gets reverted, so the whole revision is '
    'wasted -- when in doubt, write the arithmetic yourself (the indicator modules above '
    'are all plain Python and do exactly this). '
    'None of the indicator/signal modules are wired into template_repo\'s main.py '
    'by default -- that wiring is exactly the kind of change you should be making. Prefer '
    'changes that would show up as a real diff in main.py, not just config.json numbers: '
    'wiring in an indicator or the news sentiment score to gate or size trades, adding a '
    'stop-loss/take-profit rule, changing position sizing or order cadence, combining '
    'multiple signals, or trying a structurally different strategy shape. Look at the '
    "leaderboard and this strategy lineage's revision history (recent messages below, "
    'if any) to avoid repeating a variant that a sibling clone already tried.\n\n'
    'Every revision prompt includes the real current XLM/USD price, freshly fetched by '
    'monitor.py right before invoking you. Treat that number as ground truth and set '
    'buy_below/sell_above relative to it -- do NOT rely on your own training-data notion '
    "of what XLM \"typically\" costs; that knowledge may be stale or wrong, and a "
    'threshold set far from the real current price will never trigger, or will trigger '
    'on every tick. To double check the price yourself (e.g. recent history or trend, '
    'not just the single spot value given), use `exec` (curl, or run '
    '/opt/tools/price_feed.py) or `fetch_url` rather than guessing.\n\n'
    'When you are done, commit your changes on a new git branch inside the '
    "strategy's own directory (`git checkout -b auto/<timestamp>` then `git add -A "
    '&& git commit -m ...`) so the revision is tracked.\n\n'
    # The last block in the prompt, deliberately. Everything above is ~10K tokens about
    # WHAT to change; this is about the fact that changing anything requires a tool call.
    # Across the four emperor cycles logged to 2026-08-05 the model made 9 write_file
    # calls in 17 revisions and answered the other 8 with a prose summary quoting the
    # config.json, the main.py diff and the `git commit` it had never run -- one closing
    # "the revision is now ready for deployment" over a byte-identical clone. Recency is
    # the whole reason it sits here rather than next to the tool descriptions above; the
    # retry loop in revise_strategy is the backstop for when it does not land.
    'A SUMMARY IS NOT A CHANGE. The only things that modify this strategy are the '
    '`write_file` and `exec` tool calls you actually make. Text in your reply reaches '
    'nothing: pasting the new config.json into your answer does not write it, quoting a '
    'main.py diff does not apply it, and a `git commit` line inside a code fence does not '
    'run. monitor.py reads the directory, never your summary, and a revision that left '
    'the files untouched is discarded and replaced with a mechanical threshold nudge: '
    'the slot is spent and your analysis is lost. So before you write your final reply, '
    '`read_file` every path you claim to have changed and confirm the new content is '
    'really on disk. And check the order you did things in -- a `backtest_strategy` '
    'result from before you wrote the file describes the old code, not your revision, so '
    're-run it rather than quoting it.\n\n'
    'Finish by replying with a short summary of the changes you have already written to '
    'disk, and why.'
    )


# --------------------------------------------------------------------------------------
# Domain-conditional prompt/tool swap for non-sdex domains (FUTURE.md items 3 and 4).
#
# The system prompt above -- and 15 of the 20 tools registered further up (TOOLS/
# TOOL_SCHEMAS) -- are hardcoded to sdex: XLM, order books, trade_logger, basis, assets.
# None of that exists in domain_forecast.py's or domain_kalshi.py's games, and handing
# the revision model those tools/instructions unchanged would mean every non-sdex
# revision gets sdex-flavored guidance and mostly fails its smoke test, falling back to
# mechanical tweak_config jitter -- exercising evolution but never real LLM reasoning
# about the new domain. domain.py itself defers a general `domain.llm_tools()` seam to
# later (see its "what is deliberately not here" section); this is the minimal,
# contained version of that for the domains that exist today -- a branch per domain, not
# a plugin system.
#
# The sdex text above is untouched by this: REVISION_SYSTEM_PROMPT is only reassigned
# below, never edited in place, so a run with DOMAIN=sdex (or unset) gets the exact same
# prompt object built above.
# --------------------------------------------------------------------------------------

_GENERIC_TOOL_NAMES = frozenset((
    'calculate', 'get_current_time', 'get_uptime',
    'read_file', 'write_file', 'fetch_url', 'install_package', 'update_package_list',
    'exec',
))

# The one extra, domain-specific fitness-check tool each non-sdex domain gets on top of
# the generic set above -- backtest_forecast_strategy for forecast,
# backtest_kalshi_strategy for kalshi (see sr_agent_tools.py). Keyed by DOMAIN.NAME so
# adding a domain here is a one-line addition, not a new branch.
_DOMAIN_TOOL_NAMES = {
    'forecast': frozenset({'backtest_forecast_strategy'}),
    'kalshi': frozenset({'backtest_kalshi_strategy'}),
}

if _DOMAIN.NAME in _DOMAIN_TOOL_NAMES:
    _allowed_tool_names = _GENERIC_TOOL_NAMES | _DOMAIN_TOOL_NAMES[_DOMAIN.NAME]
    TOOLS = {name: fn for name, fn in TOOLS.items() if name in _allowed_tool_names}
    TOOL_SCHEMAS = [t for t in TOOL_SCHEMAS if t['function']['name'] in _allowed_tool_names]

# Functions, not bare module-level literals, and deliberately: _FACTS above is built
# from whatever domain is ACTUALLY active (domain.get()'s result), so on a default/sdex
# run _FACTS has no 'starting_score'/'score_scale'/'null_baseline' keys at all --
# evaluating an f-string that reads them at module-import time would raise KeyError and
# break master-agent.py outright for the domain everything currently runs. Deferring the
# f-string evaluation into a function called only inside its own `if _DOMAIN.NAME ==
# ...:` guard below keeps this file importable (and the sdex path untouched) no matter
# which domain is active.
def _build_forecast_revision_system_prompt():
    return (
    'You are the strategy-revision agent for an evolutionary forecasting-benchmark '
    'system (FUTURE.md item 3: a free, fast, unambiguously-scored domain with no money '
    'at all -- a research harness for the evolutionary loop itself, not a trading '
    'system). Each cycle monitor.py creates a small batch of new strategies -- clones of '
    'the best current performers, plus one pulled fresh from the template -- and hands '
    'each of them to you before it starts. The user message tells you which job you have '
    'for this one: `refine` (improve on a parent with a real track record) or `explore` '
    '(a fresh template strategy with no meaningful parent, where the job is to try '
    'something the population is not already doing). Everything below applies to both. '
    "You have full read/write/exec access to the strategy's directory and may change "
    "anything about it: config.json, main.py's forecasting logic, or add new files "
    "entirely.\n\n"
    'THE GAME. Every 30 seconds the strategy is handed `questions_per_tick` questions '
    '(a config.json knob). Each question gives you exactly one number, `feature`, in '
    '[0, 1] -- a noisy but genuinely informative signal correlated with a hidden true '
    'probability. Your job is to answer with `p_hat`, your own calibrated probability '
    'estimate in [0, 1] that the question resolves True. You never see the true '
    'probability or the outcome, and the answer is scored by '
    '/opt/tools/forecast_engine.py, independently of anything this strategy claims '
    'about itself.\n\n'
    "main.py's top-level `decide(feature, history, state, config)` is what "
    '`backtest_forecast_strategy` replays and what `beats_null` is measured on -- keep '
    'that exact signature even if the body changes completely. `history` is this '
    "strategy's own past features, oldest first (not past outcomes -- it never learns "
    'those). The template default is linear calibration: `p_hat = clip(0.5 + '
    "confidence_gain * (feature - 0.5), 0, 1)`, with `confidence_gain` read from "
    "config.json. Freely rewrite decide() -- that is the actual strategy. Do NOT call "
    "forecast_engine.submit_forecast yourself or touch state.json's running tally "
    "directly; main()'s tick loop already does both, and duplicating or bypassing it "
    'only desyncs the tally from what actually got scored.\n\n'
    f'You are scored on accumulated skill: '
    f'{_FACTS["starting_score"]:.0f} + {_FACTS["score_scale"]:.0f} * sum(base_brier - '
    f'brier) over every resolved forecast, summed rather than averaged so volume '
    f'compounds -- more good calls ranks higher than fewer good calls at the same '
    f'accuracy. {_FACTS["null_baseline"]} Growing that sum is the entire objective; a '
    'strategy that ignores `feature` and always answers 0.5 scores exactly the starting '
    'value and gets you nowhere, the same way sitting in cash forever would in a trading '
    'domain.\n\n'
    'You can and should test a revision before committing it. '
    '`backtest_forecast_strategy(strategy_path)` replays the strategy over a fixed set '
    'of already-resolved questions and returns trades, decide_source, mean_brier, '
    'mean_base_brier, beats_null and null_pct in a fraction of a second -- use it as '
    'your fitness check instead of guessing, and iterate until the numbers improve.\n'
    '  * CHECK `decide_source` FIRST, before you read any other field. The backtester '
    'only replays your logic if main.py exposes a top-level '
    '`decide(feature, history, state, config)` returning a plain float, AND main.py\'s '
    "top level contains nothing but imports, assignments, defs, the docstring and an "
    "`if __name__ == '__main__'` guard (a bare `sys.path.append(...)` call, a top-level "
    "print, an `if`/`with` or the tick loop itself all disqualify it -- write `sys.path "
    "= sys.path + ['/opt/tools']` as an assignment instead). Otherwise decide_source "
    "comes back as something other than 'main.py:decide', and every number describes "
    'the mechanical confidence_gain-only fallback rather than your code. Fix the '
    "structure and re-run until decide_source is 'main.py:decide'.\n"
    "  * decide_source == 'main.py:decide' is NOT the same as \"this file still runs\". "
    'backtest_forecast_strategy only imports the module and calls decide() directly -- '
    'it never starts main() or the tick loop, so a main.py containing nothing but a '
    'bare decide() function passes this check and then dies in production: strat_manager '
    'starts it once as a standalone process, it defines a function, reaches the bottom '
    'of the file, and exits immediately, which fails the separate smoke test and '
    'reverts your whole revision -- silently, after this session has ended. If you '
    "rewrite main.py, keep main() (load_config/load_state/tick-loop/save_state) and its "
    "`if __name__ == '__main__':` guard exactly as structured in the template, calling "
    "your new decide(); do not delete them for a \"cleaner\" file. Only decide() (and any "
    'helper functions it calls) is where your logic change belongs.\n'
    "  * Once decide_source is 'main.py:decide', treat `beats_null: false` (mean_brier "
    ">= mean_base_brier) as a failed revision and try something else -- a strategy that "
    "cannot beat guessing 0.5 is not worth starting.\n"
    '  * The fixed question set used here is unrelated to the live game\'s actual '
    'questions (different seed, see forecast_engine.py\'s docstring), so results are '
    'always comparable across runs and strategies.\n\n'
    'You are free to invent config.json knobs beyond confidence_gain -- a nonlinearity, '
    'a confidence threshold below which you hedge toward 0.5, a per-tick ensemble of '
    'rules, or something else entirely. ALWAYS read a key you add as '
    '`config.get("your_key", <sensible default>)`, never `config["your_key"]`: a '
    'main.py that raises KeyError on its first tick fails the smoke test and is '
    'reverted. Do not rename, repurpose or drop `name`, `schema_version`, '
    '`questions_per_tick` or `confidence_gain` -- monitor and the shared tally in '
    'state.json read those; add alongside them.\n\n'
    'When you are done, commit your changes on a new git branch inside the '
    "strategy's own directory (`git checkout -b auto/<timestamp>` then `git add -A "
    '&& git commit -m ...`) so the revision is tracked.\n\n'
    # Same incident, same fix, same domain-agnostic reason it sits last -- see the sdex
    # prompt above for the measured counts this addresses.
    'A SUMMARY IS NOT A CHANGE. The only things that modify this strategy are the '
    '`write_file` and `exec` tool calls you actually make. Text in your reply reaches '
    'nothing: pasting the new config.json into your answer does not write it, quoting a '
    'main.py diff does not apply it, and a `git commit` line inside a code fence does '
    'not run. monitor.py reads the directory, never your summary, and a revision that '
    'left the files untouched is discarded and replaced with a mechanical threshold '
    'nudge. So before your final reply, `read_file` every path you claim to have '
    'changed and confirm it, and re-run `backtest_forecast_strategy` if you wrote after '
    'your last check -- a result from before you wrote the file describes the old '
    'code.\n\n'
    'Finish by replying with a short summary of the changes you have already written to '
    'disk, and why.'
    )



def _build_kalshi_revision_system_prompt():
    """See KALSHI.md's "Lessons from domain_forecast.py's actual run": the forecast
    prompt above was later gutted to a single content-free line by a self-revision pass
    and never restored, and that run's own validity is weaker for it. Written out in
    full here on purpose, not as a one-liner, so the same failure mode starting from
    THIS text is at least a regression to notice rather than the starting point.
    Every number below is read live from _FACTS (domain_kalshi.prompt_facts()), never
    hardcoded -- see domain.py's "group 7" note on why a prompt and the code that
    enforces it must never be allowed to state a number independently."""
    categories = ', '.join(_FACTS['known_categories'])
    return (
    'You are the strategy-revision agent for an evolutionary system that trades real '
    'Kalshi prediction markets in paper mode (no money moves -- see domain_kalshi.py). '
    'Each cycle monitor.py creates a small batch of new strategies -- clones of the '
    'best current performers, plus one pulled fresh from the template -- and hands each '
    'to you before it starts forecasting. The user message tells you which job you have: '
    '`refine` (improve on a parent with a real track record) or `explore` (a fresh '
    'template strategy with no meaningful parent, where the job is to try something the '
    'population is not already doing).\n\n'
    'THE GAME: main.py\'s decide(market, history, state, config) is handed ONE currently '
    'open market at a time (fields include implied_prob, volume, open_interest, '
    'close_time) and must return p_hat, a probability in [0, 1] that it resolves Yes. '
    f'The null you are scored against is {_FACTS["null_baseline"]}\n\n'
    'SCORING IS NOT INSTANT. A forecast is logged the moment you make it, alongside the '
    'market\'s price at that exact instant -- but the real judgment (your p_hat\'s Brier '
    'score against the true outcome, versus that frozen market price\'s own Brier score) '
    'only happens once the market actually settles, which can take hours to days. Your '
    f'score starts at {_FACTS["starting_score"]} and moves by roughly '
    f'{_FACTS["score_scale"]} times your average edge per RESOLVED forecast, shrunk '
    f'toward zero below about {_FACTS["confidence_prior_n"]:.0f} resolved forecasts so a '
    'small lucky sample cannot outrank a real track record -- do not expect your score '
    'to move at all in the first few cycles after a change; that is normal, not a sign '
    'nothing happened.\n\n'
    'THREE KNOBS live in config.json: `confidence_gain` '
    f'(bounded [-{_FACTS["max_confidence_gain"]}, {_FACTS["max_confidence_gain"]}]), '
    f'`markets_per_tick` (bounded [1, {_FACTS["max_markets_per_tick"]}]), and '
    f'`category_filter`, which must be one of: {categories}. Changing category_filter is '
    'a real, structural move -- it changes which markets kalshi_recorder.py has cached '
    'for you to read at all, not just a magnitude tweak. config.json is yours to extend '
    'with new keys the same way the sdex population does; read any new key with '
    '`config.get("your_key", <default>)`, never `config["your_key"]`.\n\n'
    'STRUCTURE: exactly like the sdex/forecast templates, main.py\'s module top level '
    'may contain only imports, assignments, defs, the docstring and an `if __name__` '
    'guard -- decide() must be importable at the top level, or kalshi_backtest.py '
    'silently falls back to replaying the mechanical confidence_gain-only rule instead '
    'of your real code, and every number backtest_kalshi_strategy reports describes the '
    'fallback, not your change. Always check its decide_source field first.\n\n'
    'Use backtest_kalshi_strategy as your fitness check before committing -- it replays '
    'against a fixed set of already-resolved real Kalshi markets and reports whether '
    'you beat the market\'s own frozen price. A strategy that just copies the market '
    'price (confidence_gain effectively inert) scores edge close to zero by '
    'construction; real edge has to come from something else -- history (this ticker\'s '
    'own recent price drift, already in the default rule), volume, spread, or a signal '
    'genuinely outside the market\'s own price. Finish by committing your changes to a '
    'new git branch inside the strategy\'s directory.')


if _DOMAIN.NAME == 'forecast':
    REVISION_SYSTEM_PROMPT = _build_forecast_revision_system_prompt()
elif _DOMAIN.NAME == 'kalshi':
    REVISION_SYSTEM_PROMPT = _build_kalshi_revision_system_prompt()
else:
    REVISION_SYSTEM_PROMPT = _build_sdex_revision_system_prompt()


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


def _main_py_shape(source: str):
    """(sorted imported module names, top-level def names) for one main.py, via AST.

    Module names are taken to their first component ('tools.rsi' -> 'rsi') because that
    is how a strategy names them after the sys.path append, and it is what SIGNAL_MODULES
    is written in terms of.
    """
    tree = ast.parse(source)
    imports, defs = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split('.')[0])
    # Drop the stdlib: json/sys/time/pathlib are in every strategy and say nothing about
    # what it trades on. What is left is the /opt/tools surface it actually reaches for,
    # which is the whole point of showing this.
    imports -= sys.stdlib_module_names
    for node in tree.body:               # top level only -- what the backtester can see
        if isinstance(node, ast.FunctionDef):
            defs.append(node.name)
    return sorted(imports), defs


def _custom_config_keys(names) -> str:
    """One line naming the config.json keys revisions have invented, with counts.

    The main.py census tells an explore spawn what logic exists; this tells it what
    *parameters* exist. Without it "invent a new config knob" is a blind guess that
    lands on rsi_period for the fourth time. Separate try/except from the caller's on
    purpose: a config census failure must not cost the main.py census too.
    """
    try:
        counts = {}
        for name in names:
            cfg = _read_json(STRATEGIES_DIR / name / 'config.json', {})
            if not isinstance(cfg, dict):
                continue
            for key in cfg:
                if key not in STANDARD_CONFIG_KEYS:
                    counts[key] = counts.get(key, 0) + 1
        if not counts:
            return ('Custom config.json keys already in use: (none -- no strategy has '
                    'added one yet).')
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ('Custom config.json keys already in use: '
                + ', '.join(f'{k} ({n})' for k, n in ranked) + '.')
    except Exception as e:
        return f'(custom config key census unavailable: {e})'


def _population_census(limit: int = CENSUS_CLUSTERS) -> str:
    """What logic the tracked population already runs, rendered as prompt text.

    The explore role's whole job is to be structurally different from the incumbents, and
    nothing in the prompt ever told the model what the incumbents were. Measured on the
    live tree before this existed: 141 main.py files but only ~10 distinct hashes, the
    top three covering 113 of them, and of the signal modules the system prompt
    advertises most had no importers at all.

    Scoped to /opt/strategy_state.json -- the tracked, scored, competing population,
    which is the same set the leaderboard elsewhere in this prompt covers, so the two
    cross-reference by name. The other ~120 directories under /opt/strategies are
    retired, never-traded corpses. Cost is a few tens of ms either way; the scoping is
    about relevance, not speed.

    Never raises. A census that fails degrades to a note -- it must not cost the cycle
    its revision.
    """
    try:
        names = list(_read_json(STRATEGY_STATE_FILE, {}) or {})
        if not names:
            return '(no tracked population yet -- you are among the first strategies.)'

        clusters = {}                    # content hash -> [count, example name, source]
        for name in names:
            try:
                source = (STRATEGIES_DIR / name / 'main.py').read_text()
            except Exception:
                continue                 # untracked-but-listed, or mid-clone; just skip
            key = hashlib.md5(source.encode()).hexdigest()
            if key in clusters:
                clusters[key][0] += 1
            else:
                clusters[key] = [1, name, source]
        if not clusters:
            return '(no readable main.py in the tracked population.)'

        read = sum(c[0] for c in clusters.values())
        lines = [f'WHAT THE POPULATION ALREADY RUNS ({read} tracked strategies, '
                 f'{len(clusters)} distinct main.py variants):']
        # Every cluster is parsed, but only the `limit` largest are described. The
        # unused-module list below has to be true of the whole population, not just the
        # part that fit in the prompt, or it would name a direction someone already took.
        all_imports, blind, shown = set(), 0, 0
        for count, example, source in sorted(clusters.values(), key=lambda c: -c[0]):
            try:
                imports, defs = _main_py_shape(source)
            except SyntaxError:
                if shown < limit:
                    lines.append(f'  * {count} strategies, e.g. {example}: does not parse')
                    shown += 1
                continue
            all_imports |= set(imports)
            if 'decide' not in defs:
                blind += count
            if shown < limit:
                who = f'{example}' if count == 1 else f'{count} strategies, e.g. {example}'
                lines.append(f'  * {who} ({len(source)} bytes)')
                lines.append(f'      imports: {", ".join(imports) or "(none)"}')
                lines.append(f'      top-level defs: {", ".join(defs) or "(none)"}')
                shown += 1
        if len(clusters) > shown:
            lines.append(f'  ... and {len(clusters) - shown} smaller variants.')
        if blind:
            # Same property MAIN_PY_IMPORTABILITY gates on: no top-level decide() means
            # backtest_strategy replays config thresholds instead of the real logic.
            lines.append(f'  ({blind} of these have no top-level decide(), so their own '
                         f'backtest never sees their logic -- do not copy that.)')
        unused = [m for m in SIGNAL_MODULES if m not in all_imports]
        lines.append('Signal modules in /opt/tools that NO tracked strategy imports: '
                     + (', '.join(unused) if unused else '(none -- all are in use)'))
        lines.append(_custom_config_keys(names))
        return '\n'.join(lines)
    except Exception as e:
        return f'(population census unavailable: {e})'


def _repo_fingerprint(strategy_path: Path):
    """(HEAD, porcelain status) for a strategy repo, or None if git cannot answer.

    Deliberately the same two questions monitor.revision_changed_anything asks, in the
    same order and with the same tools, so the retry loop below and monitor's own
    "did anything happen" gate can never disagree. If they diverged, this would either
    keep re-prompting a model whose work monitor was about to accept, or accept a
    revision monitor then discards as a no-op -- both worse than the bug being fixed.

    Returns None on any git failure rather than a sentinel value, and the caller treats
    None as "cannot tell, do not retry". Failing open matters: an unreadable repo is not
    evidence the model did nothing, and a retry loop that fires on it would burn every
    attempt and still hand monitor the same clone.

    Read BEFORE _save_revision_history writes .strategy-revision-history.json. The
    template gitignores that file, but lineages older than 2026-07-31 predate that
    .gitignore, and there it is untracked -- so on those, saving history first would make
    the tree dirty and every revision look successful.
    """
    def git(*args):
        try:
            r = subprocess.run(['git', '-C', str(strategy_path), *args],
                               capture_output=True, text=True)
        except Exception:
            return None
        return r.stdout if r.returncode == 0 else None

    head = git('rev-parse', 'HEAD')
    status = git('status', '--porcelain')
    if head is None or status is None:
        return None
    return (head.strip(), status.strip())


def _no_op_correction(strategy_path: Path) -> str:
    """Sent when a reply described edits that were never written to disk."""
    return (
        f'STOP -- you did not change anything.\n\n'
        f'`{strategy_path}` is byte-for-byte identical to what it was before your last '
        f'reply. No file was written, nothing was staged, and no commit exists. '
        f'Describing an edit is not making one: text in your reply does not reach the '
        f'filesystem, and a `git commit` line inside a code fence does not run. Only an '
        f'actual `write_file` or `exec` tool call changes this directory.\n\n'
        f'monitor.py reads the directory, never your summary. As things stand your '
        f'revision is discarded, this strategy starts as an unmodified copy with a random '
        f'threshold nudge, and the entire cycle slot is wasted.\n\n'
        f'Do it now, as tool calls:\n'
        f'  1. `write_file` the changes you just described -- config.json, and main.py too '
        f'if what you described was a change to the trading logic.\n'
        f'  2. `read_file` each path back and confirm the content you intended is really '
        f'there.\n'
        f'  3. Re-run `backtest_strategy` on the strategy directory. Any number you '
        f'already quoted was measured on the unmodified code and does not describe your '
        f'revision.\n\n'
        f'Then reply, briefly, with what is now on disk.'
    )


def _main_py_digest(strategy_path: Path):
    """sha256 of the clone's main.py, or None if it cannot be read.

    Hashed from the working tree rather than asked of git, deliberately: the question is
    whether the file the strategy will actually run is the one it was cloned with, and a
    model that writes main.py and commits it moves HEAD without leaving the tree dirty.
    None means "cannot tell", and every caller treats that as "assume it changed".
    """
    try:
        return hashlib.sha256((strategy_path / 'main.py').read_bytes()).hexdigest()
    except Exception:
        return None


def _config_only_correction(strategy_path: Path) -> str:
    """Sent to an explore spawn whose revision touched config.json and nothing else."""
    return (
        f'You changed config.json and nothing else -- main.py at `{strategy_path}` is '
        f'still the unmodified template.\n\n'
        f'This is an EXPLORE slot, and a threshold change is the one thing it is not for. '
        f'Every unrevised template spawn already gets a mechanically-seeded band from '
        f'monitor.py for free, so a config-only revision here has produced nothing the '
        f'population would not have had anyway. The unused /opt/tools signal modules '
        f'named in the census above are the open directions, and wiring one in is a '
        f'change that can only be made in main.py.\n\n'
        f'So: rewrite main.py. Import a signal module, compute it once per tick in '
        f"main()'s loop, store it in `state`, and read it back inside `decide()` to gate "
        f'or size the trade -- never fetch from inside decide() itself. Put whatever '
        f'number it needs in config.json under a name that says what it means. Then re-run '
        f'`backtest_strategy`, confirm `decide_source` is "main.py:decide" and that '
        f'`trades` is above zero, and write the file with `write_file` before you reply.\n\n'
        f'If you have looked again and are genuinely convinced the config-only change is '
        f'the better strategy here, say so explicitly and stop -- but do not just repeat '
        f'the summary you already gave.'
    )


def _smoke_test_correction(reason: str) -> str:
    """Sent back for one extra turn when monitor.py's post-hoc smoke test failed.

    The smoke test runs main.py as main() would run it in production -- not just
    imports decide() the way backtest_forecast_strategy/backtest_strategy does -- and it
    only runs in monitor.py, after this session has already exited once. This is the
    only way the model ever finds out that backtest passing and beats_null/beats_buy_hold
    being true was not the same as the file actually running. Quotes the exact failure
    monitor.py logged, since a vague "it didn't work" would waste the one retry on the
    model guessing at what broke instead of fixing it.
    """
    return (
        f"Your revision was committed, but monitor.py's smoke test failed:\n\n"
        f'  {reason}\n\n'
        f'That smoke test actually starts main.py as a standalone process the way '
        f'production does, which is a different and stricter check than the backtest '
        f'tool you already ran -- decide_source == "main.py:decide" only proves decide() '
        f'is importable, not that the file still runs. Common causes: the rewrite '
        f"dropped main() or the `if __name__ == '__main__':` guard entirely (the file "
        f'defines functions and exits immediately with no output), or it kept them but '
        f'broke something in the process itself -- a forgotten import, a NameError, an '
        f'exception before the first state save.\n\n'
        f'Read main.py back with `read_file`, fix whatever the error above points at '
        f'without discarding your actual decide() logic, and write the corrected file. '
        f'This is your last attempt this cycle -- if it fails again the whole revision '
        f'is discarded and replaced with a mechanical tweak.'
    )


def retry_after_smoke_failure(strategy_name: str, reason: str = '') -> None:
    """One bounded extra turn after monitor.py's post-hoc main.py smoke test fails.

    Distinct from the in-session retry loop inside revise_strategy (which only fires for
    "wrote nothing" / "config-only", both detectable before that subprocess exits): the
    smoke test itself only runs in monitor.py, after the revision subprocess has already
    exited and main.py has already been checked out to a fresh branch. The only way to
    get the failure back to the model is a second invocation reusing the persisted
    conversation, so it can see exactly what it wrote and why -- rather than starting
    over with the original task prompt, which is what a fresh revise_strategy call would
    do. monitor.py calls this at most once per revision; see its call site for the bound.
    """
    strategy_path = STRATEGIES_DIR / strategy_name
    print(f'[revise-strategy-retry] {strategy_name}')
    messages = _load_revision_history(strategy_path)
    if len(messages) <= 1:
        # Nothing but the system prompt -- e.g. the history file is missing or was
        # never written. No real conversation exists to append the correction to.
        print(f'[revise-strategy-retry] {strategy_name}: no revision history to retry '
              f'against')
        sys.exit(1)
    messages.append({'role': 'user', 'content': _smoke_test_correction(reason)})
    reply = run_turn(messages)
    print(reply)
    _save_revision_history(strategy_path, messages)
    if reply.startswith('[error:'):
        print(reply, file=sys.stderr)
        sys.exit(1)


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


def _refine_prompt(strategy_name, parent_name, strategy_path, parent_score,
                   leaderboard, price_line) -> str:
    """The clone case: a parent with a real track record to improve on.

    Used to be kept character-for-character as a control arm against _explore_prompt.
    See the "freeze was lifted" note above _build_sdex_revision_system_prompt.
    """
    parent_path = STRATEGIES_DIR / parent_name
    parent_config = _read_json(parent_path / 'config.json', {})
    parent_state = _read_json(parent_path / 'state.json', {})
    trade_tail = _tail_lines(TRADES_DIR / f'{parent_name}.log')
    clone_main_py = _read_text(strategy_path / 'main.py')
    return (
        f'A new clone `{strategy_name}` of `{parent_name}` was just created at '
        f'`{strategy_path}`. It has not started trading yet.\n\n'
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


def _explore_prompt(strategy_name, strategy_path, leaderboard, price_line) -> str:
    """The template-spawn case: no parent, so the job is to differ from the incumbents.

    Everything the refine prompt says about a parent is either absent or meaningless
    here -- a template spawn's `parent` is itself, its state.json is empty and its trade
    log does not exist. Before this prompt existed, the refine text was used anyway and
    the model was told "A new clone `seed_x` of `seed_x` was just created", then handed
    an empty state and a config whose buy_below == sell_above == 0.0.
    """
    own_config = _read_json(strategy_path / 'config.json', {})
    own_main_py = _read_text(strategy_path / 'main.py')
    return (
        f'`{strategy_name}` was just created at `{strategy_path}`. It is a fresh, '
        f'unmodified checkout of /opt/template_repo -- NOT a clone of any existing '
        f'strategy. It has no parent, no trade history and no track record, so there is '
        f'nothing here to improve on. Your job this time is EXPLORATION: give the '
        f'population a strategy that is structurally different from what it already '
        f'runs.\n\n'
        f'{price_line}'
        f'Its config.json right now: {json.dumps(own_config)}\n'
        f'Note the template ships buy_below and sell_above at 0.0, which never trades. '
        f'monitor.py checks the config you leave behind: `name` must still be exactly '
        f'"{strategy_name}", `schema_version` and `trade_amount_usd` must still be '
        f'present and positive, and both thresholds must be present, positive, '
        f'buy_below < sell_above, and within 50% of the current price above. Even if '
        f'your logic does not use plain thresholds at all, you must still leave a sane '
        f'band anchored to that price -- otherwise monitor discards your config and '
        f'rebuilds it from scratch at price*0.98 / price*1.02.\n\n'
        f'CONFIG.JSON IS YOURS TO EXTEND beyond those required keys -- nothing here '
        f'validates it against a schema, and the entire config dict reaches your '
        f'`decide(price, history, state, config)` and `decide_asset(...)` unchanged, in '
        f'live trading and backtest_strategy alike. A period, a lookback, a '
        f'stop-loss/take-profit percentage, a position-sizing fraction, a cooldown -- '
        f'whatever your logic needs belongs in config.json under a name that says what '
        f'it means, not hardcoded in main.py. Inventing a genuinely new parameter is a '
        f'good outcome for this slot; a knob there is visible to every descendant\'s '
        f'later revision, unlike the same number buried in main.py. Two rules:\n'
        f'  * ALWAYS read your own keys as `config.get("your_key", <sensible '
        f'default>)`, never `config["your_key"]` -- a KeyError on the first tick fails '
        f'the smoke test and is reverted.\n'
        f'  * Do not rename, repurpose or drop `name`, `schema_version`, `buy_below`, '
        f'`sell_above`, `trade_amount_usd` or `assets` -- monitor and the shared tools '
        f'read those. Add alongside them, and remove only keys you introduced yourself '
        f'and no longer use.\n\n'
        f'Its main.py (the unmodified template -- a starting point, not a parent\'s '
        f'proven code, so you are free to restructure it):\n'
        f'```python\n{own_main_py}\n```\n\n'
        f'{_population_census()}\n\n'
        f'Current leaderboard (strategy name -> score, all strategies currently '
        f'running): {json.dumps(leaderboard)}\n\n'
        f'Write something the census above shows the population is NOT already doing -- '
        f'a threshold nudge is a wasted slot here, since every unrevised template spawn '
        f'gets one for free. The unused signal modules named above are the concrete '
        f'open directions. Whatever you write, a top-level `decide()` must remain '
        f'importable (decide_source "main.py:decide", not "config-thresholds") and '
        f'beats_buy_hold must come back true -- for a strategy with no track record '
        f'that is the entire acceptance test, so run backtest_strategy and iterate '
        f'until it passes.\n\n'
        f'On assets: leaving `assets` empty lets monitor hand this strategy up to two '
        f'verified discovered assets on a coin flip; declaring your own turns that off. '
        f'Declare only if they are part of your thesis, and never write an issuer '
        f'address from memory -- verify it first.\n\n'
        f'Commit your changes to a new git branch inside `{strategy_path}` when done.'
    )


def _refine_prompt_forecast(strategy_name, parent_name, strategy_path, parent_score,
                            leaderboard, tick_line) -> str:
    """The forecast domain's clone case. Parallel to _refine_prompt, minus every
    concept (price, trade_amount_usd, assets) that has no forecast-domain meaning."""
    parent_path = STRATEGIES_DIR / parent_name
    parent_config = _read_json(parent_path / 'config.json', {})
    parent_state = _read_json(parent_path / 'state.json', {})
    forecast_tail = _tail_lines(TRADES_DIR / f'{parent_name}.log')
    clone_main_py = _read_text(strategy_path / 'main.py')
    return (
        f'A new clone `{strategy_name}` of `{parent_name}` was just created at '
        f'`{strategy_path}`. It has not made a forecast yet.\n\n'
        f'{tick_line}'
        f"Parent `{parent_name}`'s config.json: {json.dumps(parent_config)}\n"
        f"Parent `{parent_name}`'s current state.json (forecasts_made/brier_sum/"
        f"base_brier_sum -- a running tally, lower brier_sum per forecast is better): "
        f"{json.dumps(parent_state)}\n"
        f"Parent `{parent_name}`'s score this cycle: {parent_score}\n"
        f"Parent `{parent_name}`'s most recent resolved forecasts:\n{forecast_tail}\n\n"
        f"The clone's main.py (identical to the parent's right now -- this is what you'd "
        f"edit to change forecasting logic, not just config.json):\n"
        f"```python\n{clone_main_py}\n```\n\n"
        f'Current leaderboard (strategy name -> score, all strategies currently '
        f'running, including any you revised in previous cycles): {json.dumps(leaderboard)}\n\n'
        f'Revise the clone at `{strategy_path}` however you think will improve on its '
        f'parent, then commit your changes to a new git branch inside that directory.'
    )


def _explore_prompt_forecast(strategy_name, strategy_path, leaderboard, tick_line) -> str:
    """The forecast domain's template-spawn case. Parallel to _explore_prompt.

    Omits _population_census() on purpose: that census (main.py clustering, unused
    SIGNAL_MODULES, custom config-key counts) is sdex-specific machinery built around a
    population that has had months to accumulate variety. This domain has one real knob
    so far (confidence_gain) -- not enough population diversity yet to make a census
    worth the tokens. Worth adding if/when this domain accumulates its own varied
    population, not before (see FUTURE.md's own rule about building abstractions against
    one example).
    """
    own_config = _read_json(strategy_path / 'config.json', {})
    own_main_py = _read_text(strategy_path / 'main.py')
    return (
        f'`{strategy_name}` was just created at `{strategy_path}`. It is a fresh, '
        f'unmodified checkout of /opt/template_repo_forecast -- NOT a clone of any '
        f'existing strategy. It has no parent, no forecast history and no track record, '
        f'so there is nothing here to improve on. Your job this time is EXPLORATION: '
        f'give the population a strategy that is structurally different from what it '
        f'already runs.\n\n'
        f'{tick_line}'
        f'Its config.json right now: {json.dumps(own_config)}\n'
        f'monitor.py checks the config you leave behind: `name` must still be exactly '
        f'"{strategy_name}", `questions_per_tick` must be a positive integer (at most '
        f'{_FACTS["max_questions_per_tick"]}), and `confidence_gain` must be in '
        f'[0, {_FACTS["max_confidence_gain"]}]. Even if your logic does not use '
        f'a plain linear gain at all, you must still leave those two knobs in range -- '
        f'otherwise monitor repairs them back to safe defaults.\n\n'
        f'CONFIG.JSON IS YOURS TO EXTEND beyond those two required keys -- nothing here '
        f'validates it against a schema, and the entire config dict reaches your '
        f'`decide(feature, history, state, config)` unchanged, in live running and '
        f'backtest_forecast_strategy alike. A nonlinearity\'s shape, a confidence '
        f'threshold below which you hedge toward 0.5, an ensemble weight -- whatever '
        f'your logic needs belongs in config.json under a name that says what it means, '
        f'not hardcoded in main.py. Two rules:\n'
        f'  * ALWAYS read your own keys as `config.get("your_key", <sensible '
        f'default>)`, never `config["your_key"]` -- a KeyError on the first tick fails '
        f'the smoke test and is reverted.\n'
        f'  * Do not rename, repurpose or drop `name`, `schema_version`, '
        f'`questions_per_tick` or `confidence_gain` -- monitor and the shared tally in '
        f'state.json read those. Add alongside them.\n\n'
        f'Its main.py (the unmodified template -- a starting point, not a parent\'s '
        f'proven code, so you are free to restructure it):\n'
        f'```python\n{own_main_py}\n```\n\n'
        f'Current leaderboard (strategy name -> score, all strategies currently '
        f'running): {json.dumps(leaderboard)}\n\n'
        f'Whatever you write, a top-level `decide()` must remain importable '
        f'(backtest_forecast_strategy must report decide_source "main.py:decide", not '
        f'anything else) and beats_null must come back true -- for a strategy with no '
        f'track record that is the entire acceptance test, so run '
        f'backtest_forecast_strategy and iterate until it passes.\n\n'
        f'Commit your changes to a new git branch inside `{strategy_path}` when done.'
    )


def _refine_prompt_kalshi(strategy_name, parent_name, strategy_path, parent_score,
                          leaderboard, tick_line) -> str:
    """The kalshi domain's clone case. Parallel to _refine_prompt_forecast, but written
    out in full (see _build_kalshi_revision_system_prompt's docstring for why a
    one-liner here is a known failure mode, not a safe default to copy)."""
    parent_path = STRATEGIES_DIR / parent_name
    parent_config = _read_json(parent_path / 'config.json', {})
    parent_state = _read_json(parent_path / 'state.json', {})
    forecast_tail = _tail_lines(TRADES_DIR / f'{parent_name}.log')
    clone_main_py = _read_text(strategy_path / 'main.py')
    return (
        f'A new clone `{strategy_name}` of `{parent_name}` was just created at '
        f'`{strategy_path}` (a git checkout of the strategy code). It has not started '
        f'forecasting yet.\n\n'
        f'{tick_line}'
        f"Parent `{parent_name}`'s config.json: {json.dumps(parent_config)}\n"
        f"Parent `{parent_name}`'s current state.json: {json.dumps(parent_state)}\n"
        f"Parent `{parent_name}`'s score this cycle: {parent_score}\n"
        f"Parent `{parent_name}`'s most recent log lines (a mix of 'forecast' entries "
        f"it wrote and 'resolution' entries kalshi_reconcile.py appended once a market "
        f"settled -- join them on `ticker` to see which bets paid off):\n{forecast_tail}\n\n"
        f"The clone's main.py (identical to the parent's right now -- this is what "
        f"you'd edit to change forecasting logic, not just config.json):\n"
        f"```python\n{clone_main_py}\n```\n\n"
        f'Current leaderboard (strategy name -> score, all strategies currently '
        f'running, including any you revised in previous cycles): {json.dumps(leaderboard)}\n\n'
        f'Revise the clone at `{strategy_path}` however you think will improve on its '
        f'parent, then run backtest_kalshi_strategy to check your change before '
        f'committing, and commit to a new git branch inside that directory when done.'
    )


def _explore_prompt_kalshi(strategy_name, strategy_path, leaderboard, tick_line) -> str:
    """The kalshi domain's template-spawn case. Parallel to _explore_prompt_forecast --
    omits _population_census() for the same reason (sdex-specific machinery built
    around months of population variety this domain does not have yet)."""
    own_config = _read_json(strategy_path / 'config.json', {})
    own_main_py = _read_text(strategy_path / 'main.py')
    categories = ', '.join(_FACTS['known_categories'])
    return (
        f'`{strategy_name}` was just created at `{strategy_path}`. It is a fresh, '
        f'unmodified checkout of /opt/template_repo_kalshi -- NOT a clone of any '
        f'existing strategy. It has no parent, no forecast history and no track '
        f'record, so there is nothing here to improve on. Your job this time is '
        f'EXPLORATION: give the population a strategy that reads something genuinely '
        f'different from what it already does.\n\n'
        f'{tick_line}'
        f'Its config.json right now: {json.dumps(own_config)}\n'
        f'`category_filter` must be one of: {categories}. Switching to a category '
        f'nobody in the population is currently using is itself a real exploration '
        f'move -- it changes which markets kalshi_recorder.py caches for this strategy '
        f'to read, not just a magnitude tweak.\n\n'
        f'Its main.py (the unmodified template -- a starting point, not a parent\'s '
        f'proven code, so you are free to restructure it):\n'
        f'```python\n{own_main_py}\n```\n\n'
        f'Current leaderboard (strategy name -> score, all strategies currently '
        f'running): {json.dumps(leaderboard)}\n\n'
        f'The template\'s default decide() only ever reads this ticker\'s own recent '
        f'price drift (momentum). Real, orthogonal signal -- volume trend, spread '
        f'width, or something read from outside kalshi_recorder.py entirely -- is the '
        f'concrete open direction; a bare confidence_gain nudge is a wasted slot here, '
        f'the same way a threshold nudge is on the sdex population. Whatever you write, '
        f'a top-level `decide()` must remain importable: run backtest_kalshi_strategy '
        f'and check decide_source is "main.py:decide", not "config-thresholds", before '
        f'trusting any of its other numbers. beats_null being false is not automatically '
        f'a failure here -- real Kalshi markets are efficiently priced and beating the '
        f'market\'s own price is a genuinely hard bar, harder than domain_forecast\'s '
        f'synthetic one -- but an importable decide() is not optional.\n\n'
        f'Commit your changes to a new git branch inside `{strategy_path}` when done.'
    )


def _compose_revision_messages(strategy_name: str, parent_name: str, parent_score: str,
                                leaderboard_json: str, observation: str, role: str):
    """Build the exact messages a revision turn would open with, no model call.

    Shared by `revise_strategy` (which sends this to `run_turn`) and
    `dump_revision_prompt` (which writes it to a file for a human to carry out via
    Claude Code instead) -- both must ask for identically the same revision.

    `role` picks the user prompt: `refine` for a clone of a leader (parent config, state,
    trade tail and score -- improve on it), `explore` for a fresh template spawn (a
    census of what the population already runs, and an instruction to differ).

    `observation` is whatever the domain knew at the top of the cycle, encoded as one argv
    token by monitor.py (see domain.py). For sdex that is still literally `str(price)`, so
    a hand-typed `... <score> <leaderboard> 0.1666` keeps working exactly as before; an
    empty string means monitor could not observe anything this cycle, which the prompt has
    a branch for. Decoding is the domain's job so a domain with a structured observation
    needs no change to this argv contract.

    Returns `(messages, resolved_role, strategy_path)`.
    """
    strategy_path = STRATEGIES_DIR / strategy_name
    if role not in (ROLE_REFINE, ROLE_EXPLORE):
        role = ROLE_REFINE
    if parent_name == strategy_name:
        # Structural, and deliberately independent of what monitor passed: a strategy
        # that is its own parent is exactly the template-spawn case, and it is the one
        # the refine prompt degenerates on. This holds even if a future caller forgets
        # the role argument entirely.
        role = ROLE_EXPLORE

    try:
        leaderboard = json.loads(leaderboard_json)
    except Exception:
        leaderboard = {}

    # How the observation is stated to the model is the domain's wording: for sdex it is
    # the "this is ground truth, not a historical price" framing that stops the model
    # anchoring on a price out of its training data.
    price_line = _DOMAIN.observation_line(_DOMAIN.decode_observation(observation))

    if _DOMAIN.NAME == 'forecast':
        if role == ROLE_EXPLORE:
            prompt = _explore_prompt_forecast(strategy_name, strategy_path, leaderboard,
                                              price_line)
        else:
            prompt = _refine_prompt_forecast(strategy_name, parent_name, strategy_path,
                                             parent_score, leaderboard, price_line)
    elif _DOMAIN.NAME == 'kalshi':
        if role == ROLE_EXPLORE:
            prompt = _explore_prompt_kalshi(strategy_name, strategy_path, leaderboard,
                                            price_line)
        else:
            prompt = _refine_prompt_kalshi(strategy_name, parent_name, strategy_path,
                                           parent_score, leaderboard, price_line)
    elif role == ROLE_EXPLORE:
        prompt = _explore_prompt(strategy_name, strategy_path, leaderboard, price_line)
    else:
        prompt = _refine_prompt(strategy_name, parent_name, strategy_path, parent_score,
                                leaderboard, price_line)

    messages = _load_revision_history(strategy_path)
    messages.append({'role': 'user', 'content': prompt})
    return messages, role, strategy_path


def revise_strategy(strategy_name: str, parent_name: str, parent_score: str = '',
                     leaderboard_json: str = '{}', observation: str = '',
                     role: str = ROLE_REFINE) -> None:
    """One-shot entry point invoked by monitor.py's provisioning stage.

    Hands a freshly-created, not-yet-started strategy directory to the LLM with full
    tool access so it can rewrite config/code and commit the revision itself, instead
    of monitor.py applying a fixed random tweak.

    `role` is a trailing positional rather than a flag because __main__ below is a bare
    `revise_strategy(*sys.argv[2:])` splat, so a defaulted positional keeps a hand-typed
    five-argument invocation working with no change there.
    """
    messages, role, strategy_path = _compose_revision_messages(
        strategy_name, parent_name, parent_score, leaderboard_json, observation, role)
    print(f'[revise-strategy] {strategy_name} role={role}')

    # The retry loop. `before` is captured once, outside: the question at every attempt is
    # "has anything changed since the clone was handed to me", not "since the last turn",
    # so a model that writes a file on attempt 2 and nothing on attempt 3 still counts as
    # having revised.
    before = _repo_fingerprint(strategy_path)
    main_py_before = _main_py_digest(strategy_path)
    nudged_for_config_only = False
    reply = ''
    for attempt in range(1, REVISION_MAX_ATTEMPTS + 1):
        reply = run_turn(messages)
        print(reply)
        if reply.startswith('[error:'):
            break       # the model is unreachable; another attempt buys nothing

        after = _repo_fingerprint(strategy_path)
        wrote_nothing = (before is not None and after is not None and after == before)
        # An explore spawn that only moved numbers has spent the one slot the population
        # has for structural novelty on something the unrevised fallback produces for
        # free. Across the emperor cycles logged to 2026-08-05, all 9 write_file calls in
        # 17 revisions went to config.json and not one revision ever edited main.py --
        # which is what a population of 141 main.py files with ~10 distinct hashes,
        # 113 of them covered by the top three, is made of.
        #
        # Explore only. For a refine slot a config-only change is a legitimate answer:
        # improving on a parent's thresholds is exactly what that role was asked for.
        config_only = (role == ROLE_EXPLORE
                       and not wrote_nothing
                       and main_py_before is not None
                       and _main_py_digest(strategy_path) == main_py_before)

        if not wrote_nothing and not config_only:
            break       # it wrote something structural, or git cannot tell us -- done
        if attempt == REVISION_MAX_ATTEMPTS:
            if wrote_nothing:
                print(f'[revise-strategy] {strategy_name} still modified nothing after '
                      f'{REVISION_MAX_ATTEMPTS} attempts; leaving it to monitor.py')
            break
        if wrote_nothing:
            print(f'[revise-strategy] {strategy_name} modified no files on attempt '
                  f'{attempt}; sending it back to actually write the changes it described')
            messages.append({'role': 'user', 'content': _no_op_correction(strategy_path)})
            continue
        # Nudged at most once, and never escalated into a rejection. Unlike a no-op, a
        # config-only revision is a real answer -- it is just the weak one, and the model
        # is explicitly offered the option of standing by it. Pressing harder than this
        # would buy main.py churn for its own sake, which is not what the slot is for
        # either.
        if nudged_for_config_only:
            break
        nudged_for_config_only = True
        print(f'[revise-strategy] {strategy_name} ({role}) changed config.json only on '
              f'attempt {attempt}; asking once for a main.py change instead')
        messages.append({'role': 'user', 'content': _config_only_correction(strategy_path)})

    _save_revision_history(strategy_path, messages)
    if reply.startswith('[error:'):
        # run_turn gave up (quota exhausted, or server errors past the retry budget)
        # rather than actually revising anything. Exit non-zero so monitor.py's caller
        # applies its random-tweak fallback -- without this the clone would silently
        # start as a byte-identical copy of its parent.
        print(reply, file=sys.stderr)  # monitor.py logs stderr for a failed revision
        sys.exit(1)


# Sentinel monitor.py's REVISION_MODE=manual polls for -- see dump_revision_prompt /
# revision_done below and monitor.py's matching MANUAL_REVISION_DONE_FILE. Keep both
# files in sync if either path changes.
MANUAL_REVISION_DONE_FILE = Path('/opt/.revision_done')
DEFAULT_REVISION_PROMPT_FILE = Path('/opt/emperor_logs/pending_revision.md')


def dump_revision_prompt(strategy_name: str, parent_name: str, parent_score: str = '',
                          leaderboard_json: str = '{}', observation: str = '',
                          role: str = ROLE_REFINE, out_path: str = '') -> None:
    """Write the revision prompt to a file instead of sending it to the model.

    For monitor.py's REVISION_MODE=manual: the same prompt `revise_strategy` would open
    a run_turn loop with, dumped so a human can paste it into Claude Code and carry out
    the revision by hand under direct supervision -- see `_compose_revision_messages`,
    which both share so a manual revision is asked exactly what an automatic one would
    be. Never calls the model and never touches `.strategy-revision-history.json`.
    """
    messages, role, strategy_path = _compose_revision_messages(
        strategy_name, parent_name, parent_score, leaderboard_json, observation, role)
    print(f'[dump-revision-prompt] {strategy_name} role={role}')

    out = Path(out_path) if out_path else DEFAULT_REVISION_PROMPT_FILE
    parts = [
        f'# Manual revision for `{strategy_name}` (role={role})\n',
        f'All edits must be made under this exact absolute path -- use it for every '
        f'read/write regardless of your own current working directory:\n\n'
        f'    {strategy_path}\n',
    ]
    for m in messages:
        label = {'system': 'SYSTEM', 'user': 'TASK'}.get(m['role'], m['role'].upper())
        parts.append(f'\n---\n## {label}\n\n{m["content"]}\n')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(parts))
    print(f'Wrote revision prompt for {strategy_name} to {out}')


def revision_done(strategy_name: str = '') -> None:
    """Touch the sentinel monitor.py's manual-revision wait polls for.

    Not a hard gate -- touching this early or by mistake costs at most one wasted
    revision slot, since `revision_changed_anything` back in monitor.py still checks
    the working tree independently before anything is committed, exactly as it does for
    a no-op model reply today.
    """
    MANUAL_REVISION_DONE_FILE.touch()
    label = f' for {strategy_name}' if strategy_name else ''
    print(f'Marked the manual revision{label} done ({MANUAL_REVISION_DONE_FILE}); '
          f'monitor.py will resume at its next poll.')


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
        try:
            user_input = input('> ')
        except EOFError:
            # stdin closed (piped input exhausted, or Ctrl-D) -- exit like 'exit'
            print()
            break
        except KeyboardInterrupt:
            print()
            break
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
    elif len(sys.argv) > 1 and sys.argv[1] == 'retry-after-smoke-failure':
        retry_after_smoke_failure(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'dump-revision-prompt':
        dump_revision_prompt(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'revision-done':
        revision_done(*sys.argv[2:])
    else:
        main()
