# CNM Devigger Discord Bot

Devigs American odds using the Crazy Ninja Mike input format and four
switchable devig methods, then compares against a flat pick'em payout.

## What's verified vs. best-effort

- **`devig.py`** (4 devig methods) - 32 unit tests, including hand-computed
  exact values and cross-method invariants. Shin's method uses the
  standard Shin (1993) formula - not verified against CNM's exact
  implementation (no reference outputs available); every `/devig` output
  labels which method was used so you can sanity-check against the real
  CNM tool.
- **`parser.py`** (input grammar, parlay/OR/XOR, juice specification) -
  34 more unit tests. The comma/slash/^/|| grammar and the 2-value
  juice-bracket ("borrow a reference market's hold") are confirmed
  directly against the real CNM spec. The 3-value alt-line juice formula
  and the `%`-default historical hold are this project's own reasoned
  interpretation, not confirmed against CNM - every synthesized leg's
  `juice_source` field says exactly how its missing side was derived, so
  you can check it against CNM's actual number for the same input.
- **`bot.py`** - not live-tested against Discord's API (no bot token
  available in this environment). The embed-building logic was exercised
  directly against real `evaluate()` output for every input format
  variant and didn't crash, but the first real `/devig` and `/ev` in
  your server are this layer's actual test.

## Discord setup (one-time)

1. Go to **https://discord.com/developers/applications** and click
   **New Application**. Name it (e.g. "CNM Devigger") and create it.
2. In the left sidebar, click **Bot**. Click **Reset Token**, confirm,
   and copy the token immediately - Discord only shows it once. Paste it
   into `devigger/.env` (copy `.env.example` to `.env` first) as
   `DISCORD_BOT_TOKEN=...`.
3. Still on the **Bot** page: leave every **Privileged Gateway Intent**
   toggle OFF (Presence, Server Members, Message Content) - this bot
   only uses slash commands, it never reads message text or member
   lists, so none of those are needed.
4. In the left sidebar, click **OAuth2** -> **URL Generator**.
   - Under **Scopes**, check **`bot`** and **`applications.commands`**
     (both required - `bot` adds the bot user to your server,
     `applications.commands` is what makes slash commands work there).
   - Under **Bot Permissions**, check **Send Messages** and
     **Embed Links** (the only two this bot needs).
5. Copy the generated URL at the bottom of that page, open it in a
   browser, pick your server from the dropdown, and click **Authorize**.
   The bot now appears in your server's member list (shown offline until
   you actually run it).
6. (Optional, recommended for testing) Turn on Discord's **Developer
   Mode**: User Settings -> Advanced -> Developer Mode. Right-click your
   server's icon -> **Copy Server ID**. Put that in `.env` as
   `DISCORD_GUILD_ID=...` - this makes slash commands sync to your
   server in seconds instead of waiting up to an hour for a global sync
   to propagate. Remove it (or leave unset) once you're happy and want
   the commands available globally.

## Running it

```
cd devigger
pip install -r requirements.txt
cp .env.example .env   # then paste your real token in
python bot.py
```

You should see `Logged in as <bot name>` and a sync confirmation in the
console. In Discord, type `/devig` or `/ev` in any channel the bot can
see.

## Commands

- **`/devig <odds> [method]`** - fair value / hold / probability
  breakdown for any CNM-format odds string. `method` defaults to
  Multiplicative if omitted.
- **`/ev <odds> <payout_multiplier> [method]`** - devigs `odds`, then
  computes EV against a flat pick'em payout that returns
  `payout_multiplier`x total (e.g. `3.0` for a pick'em paying 3x your
  stake). Reports breakeven probability, edge, EV%, and whether it
  clears breakeven.

### Input format examples

| Input | Meaning |
|---|---|
| `+500/-700` | One 2-way market, betting the `+500` side |
| `+500/+250/+2000` | One native 3-way market |
| `+500/^+800/^+1000` | Three separate 2-way markets' "yes" sides, combined as mutually exclusive |
| `+200` | Fair-value shorthand - no vig assumed, mirrors to `-200` |
| `+150,-170` | 2-leg parlay (both must hit) |
| `-115/-110||-185/+140` | Two independent bets, either hitting wins |
| `+285/[-116:-106]` | Borrow juice/hold from a known similar market |
| `-270/[-135:-110:-110]` | Derive juice from an alt-line reference |
| `+600%` | Estimate juice via a default historical hold (player props) |

## Running the test suite

```
cd devigger
python -m unittest test_devig test_parser test_combine test_ev -v
```

66 tests, all passing as of this writing.
