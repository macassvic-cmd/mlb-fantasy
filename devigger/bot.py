"""
Discord bot exposing /devig and /ev as slash commands. Webhooks can't
receive commands (they're send-only), so this needs a real bot
application with a token - see README.md in this directory for the
exact discord.com/developers setup steps.

Wraps parser.py / combine.py / ev.py, all already unit-tested
independently (see test_parser.py, test_combine.py, test_ev.py,
test_devig.py - 66 tests). This file itself is NOT live-tested against
Discord's actual API (no way to do that from here without a real bot
token and a server to join) - the command wiring and embed formatting
below follow discord.py's documented patterns, but treat first real use
as the live test of THIS layer specifically, not the math underneath it.
"""

import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

from combine import evaluate
from ev import pickem_ev

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("devigger_bot")

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# Optional: set to your Discord server's ID (right-click the server icon
# -> Copy Server ID, with Developer Mode on) for near-instant slash-
# command sync during development. A GLOBAL sync (the default, when this
# is unset) can take up to an hour to propagate to Discord's UI the
# first time a command is added or changed - see README.md.
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")

# User-facing method choices. "Additive/Shin" both resolve to devig.py's
# "shin" implementation - see devig.py's module docstring for why those
# two names share one slot (the user's own spec lists them together).
METHOD_CHOICES = [
    app_commands.Choice(name="Multiplicative (default)", value="multiplicative"),
    app_commands.Choice(name="Additive/Shin", value="shin"),
    app_commands.Choice(name="Power", value="power"),
    app_commands.Choice(name="Worst-case", value="worst_case"),
]

METHOD_DISPLAY = {
    "multiplicative": "Multiplicative",
    "shin": "Additive/Shin",
    "additive_linear": "Additive (linear)",
    "power": "Power",
    "worst_case": "Worst-case",
}


class DevigClient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Slash commands synced to guild {DISCORD_GUILD_ID} (near-instant).")
        else:
            # Global sync - can take up to an hour to propagate on
            # Discord's side the first time a command is added/changed.
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to an hour to appear).")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (id={self.user.id})")


client = DevigClient()


def _format_leg_block(leg_eval, index):
    market = leg_eval.leg.market
    r = leg_eval.devig_result
    lines = [
        f"**Leg {index}: `{leg_eval.leg.raw_text}`**",
        f"Fair odds: `{leg_eval.fair_american_odds:+.0f}`  |  Fair prob: `{leg_eval.fair_probability * 100:.2f}%`",
        f"Market hold: `{r.hold_pct:.2f}%`",
    ]
    if market.combined_from_separate_markets:
        lines.append("_XOR-combined from separate markets_")
    if market.juice_source:
        lines.append(f"_Juice: {market.juice_source}_")
    if r.warnings:
        for w in r.warnings:
            lines.append(f"WARNING: {w}")
    return "\n".join(lines)


def build_devig_embed(input_str, method, result):
    display_method = METHOD_DISPLAY.get(method, method)
    embed = discord.Embed(
        title="Devig Result",
        description=f"`{input_str}`\nMethod: **{display_method}**",
        color=0x3498DB,
    )

    leg_counter = 1
    for gi, group_eval in enumerate(result.group_evaluations):
        for leg_eval in group_eval.leg_evaluations:
            embed.add_field(name="​", value=_format_leg_block(leg_eval, leg_counter), inline=False)
            leg_counter += 1
        if len(group_eval.leg_evaluations) > 1:
            embed.add_field(
                name="​",
                value=(f"**Group {gi + 1} combined (parlay):** "
                       f"fair odds `{group_eval.combined_fair_american_odds:+.0f}`, "
                       f"fair prob `{group_eval.combined_fair_probability * 100:.2f}%`"),
                inline=False,
            )

    if len(result.group_evaluations) > 1:
        embed.add_field(
            name="Overall (OR-combined across groups)",
            value=(f"Fair odds: `{result.overall_fair_american_odds:+.0f}`\n"
                   f"Fair probability: `{result.overall_fair_probability * 100:.2f}%`"),
            inline=False,
        )
    elif len(result.group_evaluations[0].leg_evaluations) > 1:
        embed.add_field(
            name="Overall (parlay)",
            value=(f"Fair odds: `{result.overall_fair_american_odds:+.0f}`\n"
                   f"Fair probability: `{result.overall_fair_probability * 100:.2f}%`"),
            inline=False,
        )

    return embed


def build_ev_embed(input_str, method, result, ev_result):
    display_method = METHOD_DISPLAY.get(method, method)
    color = 0x2ECC71 if ev_result.clears_breakeven else 0xE74C3C
    embed = discord.Embed(
        title="EV vs. Pick'em Payout",
        description=f"`{input_str}` @ **{ev_result.payout_multiplier:.2f}x**\nMethod: **{display_method}**",
        color=color,
    )
    embed.add_field(name="Fair probability", value=f"`{ev_result.fair_probability * 100:.2f}%`", inline=True)
    embed.add_field(name="Fair odds", value=f"`{result.overall_fair_american_odds:+.0f}`", inline=True)
    embed.add_field(name="Breakeven probability", value=f"`{ev_result.breakeven_probability * 100:.2f}%`", inline=True)
    embed.add_field(name="Edge (probability points)",
                     value=f"`{ev_result.edge_probability_points * 100:+.2f}pp`", inline=True)
    embed.add_field(name="EV (per $1 staked)", value=f"`{ev_result.ev_fraction * 100:+.2f}%`", inline=True)
    embed.add_field(
        name="Clears breakeven?",
        value="**YES** ✅" if ev_result.clears_breakeven else "**NO** ❌",
        inline=True,
    )
    return embed


@client.tree.command(name="devig", description="Devig an odds string and show fair value / hold / probability.")
@app_commands.describe(
    odds="CNM-format odds string, e.g. +500/-700 or -115/-110||-185/+140",
    method="Devig method (default: Multiplicative)",
)
@app_commands.choices(method=METHOD_CHOICES)
async def devig_command(interaction: discord.Interaction, odds: str, method: app_commands.Choice[str] = None):
    method_value = method.value if method else "multiplicative"
    try:
        result = evaluate(odds, method=method_value)
    except ValueError as e:
        await interaction.response.send_message(f"Couldn't parse that input: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.exception(f"/devig failed for input {odds!r}")
        await interaction.response.send_message(f"Something went wrong: {e}", ephemeral=True)
        return

    embed = build_devig_embed(odds, method_value, result)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="ev", description="Compute EV of a devigged fair probability vs a flat pick'em payout.")
@app_commands.describe(
    odds="CNM-format odds string, e.g. +500/-700 or +150,+150 for a 2-leg parlay",
    payout_multiplier="Total return multiple if it hits (e.g. 3.0 for a pick'em that pays 3x)",
    method="Devig method (default: Multiplicative)",
)
@app_commands.choices(method=METHOD_CHOICES)
async def ev_command(interaction: discord.Interaction, odds: str, payout_multiplier: float,
                      method: app_commands.Choice[str] = None):
    method_value = method.value if method else "multiplicative"
    try:
        result = evaluate(odds, method=method_value)
        ev_result = pickem_ev(result.overall_fair_probability, payout_multiplier)
    except ValueError as e:
        await interaction.response.send_message(f"Couldn't compute that: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.exception(f"/ev failed for input {odds!r} @ {payout_multiplier}")
        await interaction.response.send_message(f"Something went wrong: {e}", ephemeral=True)
        return

    embed = build_ev_embed(odds, method_value, result, ev_result)
    await interaction.response.send_message(embed=embed)


def main():
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN is not set in .env - see README.md for setup steps.")
        return
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
