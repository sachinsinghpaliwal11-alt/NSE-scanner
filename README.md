# NSE Scanner

Scans 49 Nifty stocks on a schedule and sends LONG / SHORT ideas to your Telegram.
It runs free on GitHub Actions. It never places trades and never touches your broker account.

## What it does

- **Intraday:** every 5 minutes on weekdays. Uses 15-minute candles, VWAP, the opening range and the 1-hour trend.
  Alerts are sent only between 9:30 AM and 2:45 PM IST. Market holidays are skipped.
- **MTF / positional:** once a day at 4:00 PM IST. Uses daily candles and the weekly trend. Buy signals only.
- Each alert shows entry, stop-loss, two targets and the reasons behind the signal.
- Every alert is saved to a journal and marked later as target hit, stop-loss hit, or closed.
  You get a Telegram message when a signal settles.

## Files

| File | What it is |
|---|---|
| `nse_scan.py` | The scanner |
| `.github/workflows/scan.yml` | The schedule that runs the scanner |
| `summary.md` | Created automatically. Win rate and latest signals |
| `journal.csv`, `journal.json` | Created automatically. Every signal and its result |

## Setup

1. Make a Telegram bot with @BotFather and copy its token. Send your bot any message.
2. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number after `"chat":{"id":`.
3. In this repository go to Settings, Secrets and variables, Actions, New repository secret. Add:
   - `TELEGRAM_BOT_TOKEN` with your token
   - `TELEGRAM_CHAT_ID` with your chat id
4. Open the Actions tab, click NSE scanner, click Run workflow, tick the test box and run it.
   A Telegram message saying the test worked means setup is complete.

## Optional settings

Add these in Settings, Secrets and variables, Actions, Variables tab, New repository variable.
Anything you leave out uses the default.

| Variable | Default | Meaning |
|---|---|---|
| `MIN_SCORE` | 5 | How strong a setup must be (score from about -12 to +12) |
| `ADX_MIN` | 20 | Skip sideways markets below this ADX. 0 turns it off |
| `TREND_ALIGN` | on | Only trade with the higher-timeframe trend. `off` turns it off |
| `MAX_STRETCH` | 3 | Skip moves more than this many ATR from the 20-period average. 0 turns it off |
| `SL_MULT` | 1.5 | Stop-loss distance in ATR |
| `COOLDOWN_HOURS` | 1 intraday, 20 MTF | Hours before the same stock can alert again |
| `DIRECTION` | both | `long` makes intraday buy-only |
| `WATCHLIST` | Nifty 50 list | Your own symbols, for example `RELIANCE,TCS,INFY` |
| `TOP_N` | 50 | How many stocks of the built-in list to scan |
| `NOTIFY_RESULTS` | on | `off` stops the RESULT messages |

## Reading the results

Open `summary.md`. R is one unit of risk, the distance from entry to stop-loss. A target 1 hit is +1.5R and a stop-loss hit is -1R.
If both are touched inside one candle it counts as a loss. Intraday signals close at the end of the session and MTF signals get 10 trading days.
Fewer than 50 closed signals is too few to judge the win rate.

## Limits

- Prices come from Yahoo Finance and can be up to 15 minutes late. Check the live price in your broker before acting.
- GitHub can start scheduled runs 5 to 15 minutes late. This suits swing and MTF ideas better than fast scalping.
- GitHub pauses scheduled runs on a repository with no activity for 60 days. If alerts stop, open the Actions tab and re-enable the workflow.
- MTF means margin trading, which charges daily interest and can force a sale if the price falls.
- Signals are setups built from standard indicators. They have no proven win rate and many will lose.

This is not investment advice. Always use a stop-loss and risk 1% or less of your capital per trade.
