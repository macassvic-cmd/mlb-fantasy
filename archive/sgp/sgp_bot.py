"""
Discord slash commands for the Play Card (2026-09-16) - ONE bot process:

    /playcard              build the SGP card now (live pricing if a key is set) and post it
    /stack <game>          price one game's stacks at DraftKings (e.g. "DET@BUF" or "det buf")
    /dns                   top current Soccer DNS candidates (read-only, kickoff-gated)

Patterns reused from dfs-demon-bot's discord_bot.py: a discord.Client with
an app_commands.CommandTree synced in setup_hook, every command defers with
a "working..." reply first, and the slow work runs in asyncio.to_thread so
the event loop never blocks. Every reply goes through discord_health.redact.

WHERE IT RUNS: this is a long-running process holding a gateway websocket;
GitHub Actions cannot host it (jobs are capped at 6h and have no inbound
network). Options, cheapest first - pick one, it is not chosen here:
  1. This Windows PC via Task Scheduler (like run_dabble_dns_local.bat) -
     free, but the commands work only while the PC is on.
  2. A $4-6/month VPS (Fly.io / Railway / a small Linux box) running
     `python sgp_bot.py` under systemd - always on.
  3. Skip slash commands: the scheduled workflow posts the card via webhook
     (already implemented in play_card.py) and "on demand" = workflow_dispatch.

Env: DISCORD_BOT_TOKEN (required), ODDSBLAZE_API_KEY (optional - without it
/playcard and /stack answer from saved prices, marked STALE).
"""

import asyncio
import logging
import os
import re

import discord
from discord import app_commands
from dotenv import load_dotenv

import play_card
import stack_forge
from discord_health import redact

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sgp_bot")


def _key_available():
    return bool(os.environ.get("ODDSBLAZE_API_KEY"))


def build_playcard_blocking(leagues=("nfl", "ncaaf")):
    card, page = play_card.run(list(leagues), price=_key_available(), from_saved=not _key_available(), post=False)
    return card


def price_one_game_blocking(game_query):
    """Finds the upcoming game whose 'AWAY@HOME' matches the query (case-
    insensitive, '@'/space/'-' tolerant), prices its stacks at DraftKings,
    returns the ranked result or None."""
    q = re.sub(r"[^a-z0-9]", "", game_query.lower())
    from oddsblaze_client import OddsBlazeClient
    client = OddsBlazeClient(offline=not _key_available())
    for league in ("nfl", "ncaaf"):
        for e in stack_forge.upcoming_events(client, league):
            key = (e["teams"]["away"]["abbreviation"] + e["teams"]["home"]["abbreviation"]).lower()
            rev = (e["teams"]["home"]["abbreviation"] + e["teams"]["away"]["abbreviation"]).lower()
            if q in (key, rev):
                results = stack_forge.price_slate(client, league, event_ids=[e["id"]])
                return results[0] if results else None
    return None


def format_stack_result(r):
    if not r or not r.get("stacks"):
        return "No stacks could be built (DraftKings has no props for that game yet)."
    header = f"**{r['away']} @ {r['home']}** ({r['league'].upper()}, kickoff {play_card.pacific(r['kickoff'])}) - DraftKings main lines, lowest odds first"
    lines = [header]
    priced = [s for s in r["stacks"] if s.get("reference_book")]
    for i, s in enumerate(priced[:5]):
        legs = " + ".join(f"{l['player']} {stack_forge.MARKET_SHORT.get(l['market'], l['market'])} O" for l in s["legs"])
        corr = f", corr {s['correlation_ratio']:.2f}x" if s.get("correlation_ratio") else ""
        dkc = f", DK corr {s['dk_correlation']:.1f}" if s.get("dk_correlation") is not None else ""
        link = f" <{s['ref_link']}>" if s.get("ref_link") else ""
        prefix = "**Lowest odds:** " if i == 0 else ""
        lines.append(f"{prefix}**{s['name']}** {legs}")
        lines.append(f"  DK **{s['ref_american']:+d}** ({s['ref_decimal']:.2f}), implied {s['implied'] * 100:.1f}%{corr}{dkc}{link}")
    problems = [s for s in r["stacks"] if not s.get("reference_book")]
    if problems:
        lines.append("DraftKings could not price: " + "; ".join(f"{s['name']} ({s['dk_problem']})" for s in problems))
    for n in r.get("notes", []):
        lines.append(f"_{n}_")
    return redact(chr(10).join(lines))[:1900]


def format_dns(dns):
    if not dns["plays"]:
        return "No upcoming DNS candidates in the latest snapshot."
    lines = [f"DNS refresh {play_card.pacific(dns['last_refresh'])}"]
    for p in dns["plays"]:
        src = "; ".join(p["sources"]) if p["sources"] else "no corroborating source yet"
        lines.append(f"**{p['player']}** ({p['team']}, {p.get('matchup') or '?'}) kickoff {p['kickoff_pt']} - DNS **{p['dns']}** / conf {p['confidence']} - {src}" + (f" <{p['rotowire_url']}>" if p.get("rotowire_url") else ""))
    return redact(chr(10).join(lines))[:1900]


class PlayCardBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("slash commands synced")


client = PlayCardBot()


@client.tree.command(name="playcard", description="Build the SGP Play Card now (DraftKings stacks) and post it")
async def playcard_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("working... building the Play Card (this can take a few minutes while stacks price)", ephemeral=False)
    try:
        card = await asyncio.to_thread(build_playcard_blocking)
        embeds = [discord.Embed(title=e["title"], description=e["description"], color=e["color"]) for e in play_card.discord_embeds(card)]
        for batch in play_card.chunk_embeds([{"title": e.title, "description": e.description, "color": e.color.value} for e in embeds]):
            await interaction.followup.send(embeds=[discord.Embed(title=b["title"], description=b["description"], color=b["color"]) for b in batch])
    except Exception as e:
        logger.exception("playcard failed")
        await interaction.followup.send(redact(f"Play Card failed: {type(e).__name__}"))


@client.tree.command(name="stack", description="Price one game's 6-man SGP stacks at DraftKings, e.g. DET@BUF")
@app_commands.describe(game="AWAY@HOME abbreviations, e.g. DET@BUF")
async def stack_cmd(interaction: discord.Interaction, game: str):
    await interaction.response.send_message(f"working... pricing stacks for {redact(game)[:20]}")
    try:
        r = await asyncio.to_thread(price_one_game_blocking, game)
        await interaction.followup.send(format_stack_result(r) if r else f"No upcoming game matched {redact(game)[:20]}.")
    except Exception as e:
        logger.exception("stack failed")
        await interaction.followup.send(redact(f"Pricing failed: {type(e).__name__}"))


@client.tree.command(name="dns", description="Top current Soccer DNS candidates")
async def dns_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("working... reading the latest DNS snapshot")
    try:
        dns = await asyncio.to_thread(play_card.top_dns_plays)
        await interaction.followup.send(format_dns(dns))
    except Exception as e:
        logger.exception("dns failed")
        await interaction.followup.send(redact(f"DNS lookup failed: {type(e).__name__}"))


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
