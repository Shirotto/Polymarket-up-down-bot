# polymarket-updown-bot

A trading bot for Polymarket's 5-minute crypto "Up or Down" markets.

Every five minutes Polymarket opens a market per asset (BTC, ETH, SOL, XRP,
DOGE) asking whether the price at the end of the round will be above or below
the price at the start. This bot watches those rounds, buys one side when its
rules say so, manages the position until the round settles, and keeps the
books.

It works, in the sense that it runs unattended, places and tracks orders,
survives restarts and cashes in its winners. It does not make money. I ran
it live with small stakes and it didn't, and the section
[Why it doesn't make money](#why-it-doesnt-make-money) explains why I think
that is structural and not a matter of tuning. I'm publishing it as a worked
example of what a complete bot for these markets involves, not as something
to point at your savings.

This repository is a cleaned-up rewrite of the bot I actually ran. The
strategy and the order and redemption code are ported from it, but since the
rewrite it has only been run in dry-run mode. Treat the live paths as
untested until you have watched them work with a stake you don't care about.

## What it does

**Two ways into a round.**

- *Late entry.* In the last seconds before the close, buy whichever side the
  order book favours, provided its ask is inside a band (0.85 to 0.95 by
  default). The idea is that by then the outcome is nearly decided and the
  favourite is "almost certain".
- *Early entry.* One to four minutes before the close, compare the live
  Binance price with the round's opening price. If it has moved far enough
  in one direction and that side is still cheap enough (0.60 to 0.85), buy
  it.

**Adding to a winner.** Up to three entries per round, only on the side
already held and only at a higher price than the previous entry. It never
averages down.

**Exits.** A stop-loss sells the position if the bid falls a fixed amount
below the entry. A take-profit exists but is off by default. Everything else
is held until the round settles.

**Loss limits.** After a daily loss limit or a run of losses inside a time
window, the bot goes into a one-hour cooldown: half stake, no early entries,
and late entries only in the final 15 seconds.

**Order handling.** Buys are fill-or-kill market orders. If the full size
doesn't fill, it tries half; if the ask moves, it follows it for a few
rounds up to a price ceiling and a deadline.

**Bookkeeping.** After each buy it checks how many shares the wallet really
holds instead of trusting the order response. Every entry and every result is
appended to a JSON Lines file. When nothing is open it compares the wallet's
USDC balance with what the tracked P&L says it should be and flags a gap.

**Redemption.** Winning shares have to be redeemed on-chain to become USDC
again. For proxy wallets (the kind Polymarket creates for an email login)
the bot builds the Gnosis Safe transaction, signs it, simulates it, and
sends it.

**A terminal dashboard** with live prices, open positions, the countdown and
the log, which falls back to plain log lines when there is no terminal.

**Dry-run mode**, on by default. It reads real markets and real order books
and simulates the orders. It needs no account and no keys.

## What it doesn't do

- It has no backtester. Nothing here was validated on historical data.
- It only takes liquidity. There are no limit orders and no market making.
- It polls REST endpoints a few times a second. It does not use Polymarket's
  websocket order book, so it is slow compared to anyone who does.
- Dry-run fills are optimistic: the whole stake at the best ask, no slippage.
- Redemption only supports proxy wallets (signature type 2). With a plain
  EOA you redeem by hand.
- The round's opening and closing prices come from the endpoint the
  polymarket.com front end uses. It is not a documented API and can change.
- It does nothing about access. Polymarket is not available everywhere; if
  you can't trade there from where you are, this won't change that.

## Why it doesn't make money

**The late entry has no information in it.** Buying a favourite at 0.92
risks 0.92 to win 0.08, so it needs to be right more than 92% of the time
just to break even. The price is already the market's estimate of that
probability, set by people with faster data than a polling loop. Being right
nine times in ten feels good and loses money: one reversal in the last
seconds takes back about eleven wins.

**The early signal is public.** "Spot has moved 0.05% above the open" is
visible to everyone, and by the time this bot sees it the Up side is already
priced accordingly. Whatever is left is the part of the move that tends to
reverse.

**The signal and the settlement use different prices.** The bot looks at
Binance. The round settles on Polymarket's own price source (Chainlink at
the time of writing). Close to the strike, which is exactly where the
decision matters, the two can disagree.

**Costs eat a thin edge.** Every trade crosses the spread, and Polymarket
charges taker fees on these markets. In practice the wallet was credited one
to two percent fewer shares than the order response reported. On a bet whose
best case is +8%, that is a large part of the upside.

**The stop-loss is a blunt tool here.** These prices swing hard in both
directions inside a round. Selling into a thin book after a 0.10 drop locks
in a loss on a position that may well have recovered, and pays the spread a
second time to do it.

**Books get thin near the close.** The moments the late entry cares about
are the ones with the least size on offer, so even small orders move the
price or don't fill.

None of this is a surprise in hindsight. A strategy built on public prices,
taker orders and a slow loop is competing in the one place where those three
things matter most.

## Running it

Python 3.10 or newer.

```
git clone <this repository>
cd polymarket-updown-bot
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m updown_bot
```

That starts a dry run with default settings. `Ctrl+C` stops it. Options:

```
--no-tui        plain log lines instead of the dashboard
--interactive   confirm mode, stake and loss limit before starting
--env-file F    read settings from F instead of .env
```

With Docker:

```
cp .env.example .env
docker compose up --build
```

Logs, journals and state end up in `data/`:

| file | contents |
|---|---|
| `bot.log` | the log |
| `state.json` | open positions and session counters, reloaded on restart |
| `entries.jsonl` | one line per buy |
| `results.jsonl` | one line per closed position |
| `wallet_audit.jsonl` | wallet versus tracked P&L (live only) |
| `redeemer.json` | redemption queue (live only) |

### Going live

Set `DRY_RUN=false` and fill in the credentials at the bottom of
`.env.example`: the signer's private key, the CLOB API key, secret and
passphrase, the funder address, and a Polygon RPC URL. The signer needs a
little POL for gas if auto-redeem is on.

Start with `AUTO_REDEEM_DRY=true`, which builds and simulates redemptions
without sending them, and read the log before turning it off.

Keep the stake small. See above.

## Configuration

Everything is set through environment variables, or a `.env` file.
[`.env.example`](.env.example) lists all of them with their defaults. The
ones that shape the strategy:

| variable | default | meaning |
|---|---|---|
| `ASSETS` | `BTC,ETH,SOL,XRP,DOGE` | markets to watch |
| `STAKE_USDC` | `5.0` | USDC per entry |
| `MAX_OPEN_POSITIONS` | `1` | positions open at once, across all assets |
| `LATE_MIN_PRICE` / `LATE_MAX_PRICE` | `0.85` / `0.95` | ask band for late entries |
| `LATE_MIN_TTR_SEC` / `LATE_MAX_TTR_SEC` | `3` / `60` | seconds before the close in which late entries are allowed |
| `EARLY_ENABLED` | `true` | turn early entries on or off |
| `EARLY_MIN_PRICE` / `EARLY_MAX_PRICE` | `0.60` / `0.85` | ask band for early entries |
| `EARLY_MIN_TTR_SEC` / `EARLY_MAX_TTR_SEC` | `60` / `240` | time window for early entries |
| `EARLY_MIN_MOVE_PCT` | `0.0005` | spot move from the open needed for an early entry |
| `MAX_ENTRIES_PER_WINDOW` | `3` | entries per round |
| `SCALE_PRICE_STEP` | `0.03` | how much higher the ask must be to add |
| `HIGH_BAND_PRICE` / `HIGH_BAND_MAX_TTR_SEC` | `0.85` / `45` | at or above this price, only buy this close to the end |
| `MAX_CHASE_PRICE` | `0.95` | stop following the ask above this |
| `STOP_LOSS_DELTA` | `0.10` | sell if the bid falls this far below the entry |
| `MAX_DAILY_LOSS_USDC` | `10.0` | daily loss that triggers a cooldown |
| `LOSS_STREAK_COUNT` / `LOSS_STREAK_WINDOW_SEC` | `2` / `7200` | losses in a window that trigger a cooldown |

A note on fees: the bot measures them rather than modelling them, by reading
the real share balance after each buy. `TAKER_FEE_RATE` is there if you want
P&L to also subtract a modelled fee, and is 0 by default. Check Polymarket's
documentation for the current schedule.

## How it's put together

```
updown_bot/
  config.py      settings from the environment, validated at startup
  markets.py     finds each round's market, its opening and closing price
  feed.py        Binance websocket, latest spot price per asset
  strategy.py    entry rules and stake size (pure functions)
  positions.py   exit rules and P&L arithmetic (pure functions)
  risk.py        daily limit, loss streak, cooldown, wallet audit
  execution.py   order book quotes, buys and sells on the CLOB
  wallet.py      USDC balance over JSON-RPC
  redeem.py      on-chain redemption through the Safe
  state.py       positions and counters, saved to disk
  journal.py     JSON Lines output
  tui.py         terminal dashboard
  app.py         the loop that ties it together
```

One pass of the loop: settle or stop out open positions, look up the current
round for each asset, fetch the order books in one request, ask
`strategy.decide` what to do, place the order if it says buy, then audit the
wallet, redeem anything pending, update the cooldown and save state.

The decision logic takes plain values and returns plain values, with no
network and no clock inside it, so the tests cover it directly:

```
pip install -e ".[dev]"
pytest
```

What the tests don't cover is live order placement and broadcasting a
redemption, which need a funded account.

## Disclaimer

This is software for experimenting, shared as is, with no warranty. It can
lose money. Nothing here is financial advice. You are
responsible for the keys you give it, for what it does with them, and for
whether you're allowed to use Polymarket where you live.

## License

MIT
