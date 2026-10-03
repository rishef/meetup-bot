"""Discord bot: members register their address privately, then get the
geographic midpoint for whoever wants to meet. Individual addresses are never
shown to anyone but their owner."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from db import Database, Meetup
from geo import GeocodeError, Geocoder, map_links, midpoint

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "meetup.db")
USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "discord-meetup-midpoint-bot/1.0")
COUNTRY_CODES = os.getenv("NOMINATIM_COUNTRY_CODES") or None
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")

MENTION_RE = re.compile(r"<@!?(\d+)>")
EMBED_COLOR = 0x5865F2
log = logging.getLogger("meetup-bot")


class MeetupBot(discord.Client):
    def __init__(self):
        # Slash commands + buttons only: no privileged intents needed.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = Database(DB_PATH)
        self.web: aiohttp.ClientSession | None = None
        self.geocoder: Geocoder | None = None

    async def setup_hook(self) -> None:
        self.web = aiohttp.ClientSession()
        self.geocoder = Geocoder(self.web, USER_AGENT, COUNTRY_CODES)
        # Re-attach button handlers to meetup messages posted before a restart.
        self.add_view(MeetupView())
        if DEV_GUILD_ID:
            guild = discord.Object(int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await super().close()
        if self.web:
            await self.web.close()
        self.db.close()


bot = MeetupBot()


# --- midpoint helpers -----------------------------------------------------


class MidpointError(Exception):
    pass


@dataclass
class MidpointResult:
    lat: float
    lon: float
    label: str
    included: list[int]
    missing: list[int]


async def find_midpoint(guild_id: int, user_ids: list[int]) -> MidpointResult:
    coords = bot.db.coords_for(guild_id, user_ids)
    included = [u for u in user_ids if u in coords]
    missing = [u for u in user_ids if u not in coords]
    if len(included) < 2:
        msg = "Need at least 2 people with a saved address to find a midpoint."
        if missing:
            msg += f"\nNo address yet: {mentions(missing)} — ask them to run `/register`."
        raise MidpointError(msg)

    lat, lon = midpoint([coords[u] for u in included])
    try:
        label = await bot.geocoder.reverse(lat, lon)
    except GeocodeError:
        log.warning("Reverse geocoding failed for %s,%s", lat, lon, exc_info=True)
        label = None
    return MidpointResult(lat, lon, label or "Unnamed spot", included, missing)


def mentions(user_ids: list[int], limit: int = 40) -> str:
    shown = " ".join(f"<@{u}>" for u in user_ids[:limit])
    if len(user_ids) > limit:
        shown += f" + {len(user_ids) - limit} more"
    return shown


def add_midpoint_field(embed: discord.Embed, lat: float, lon: float, label: str) -> None:
    google, osm = map_links(lat, lon)
    embed.add_field(
        name="📍 Meeting point",
        value=f"**{label}**\n`{lat:.5f}, {lon:.5f}`\n[Google Maps]({google}) · [OpenStreetMap]({osm})",
        inline=False,
    )


# --- address commands -----------------------------------------------------


@bot.tree.command(name="register", description="Save your address for this server (only you can ever see it)")
@app_commands.guild_only()
@app_commands.describe(address="Street address, area or landmark, e.g. '221B Baker Street, London'")
async def register(interaction: discord.Interaction, address: app_commands.Range[str, 3, 300]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        loc = await bot.geocoder.geocode(address)
    except GeocodeError as e:
        await interaction.followup.send(f"⚠️ {e}. Please try again in a moment.")
        return
    if loc is None:
        await interaction.followup.send(
            "❌ I couldn't find that address. Try adding the city and country, or use a nearby landmark."
        )
        return

    bot.db.set_address(interaction.guild_id, interaction.user.id, address, loc.lat, loc.lon)
    await interaction.followup.send(
        f"✅ Address saved. I matched it to:\n> {loc.display_name}\n\n"
        "If that's not right, run `/register` again with more detail.\n"
        "🔒 Nobody else can see your address — only group midpoints are shared."
    )


@bot.tree.command(name="myaddress", description="Show the address you saved (only visible to you)")
@app_commands.guild_only()
async def myaddress(interaction: discord.Interaction):
    row = bot.db.get_address(interaction.guild_id, interaction.user.id)
    if row is None:
        await interaction.response.send_message("You haven't saved an address yet. Use `/register`.", ephemeral=True)
        return
    address, lat, lon = row
    await interaction.response.send_message(f"Your saved address:\n> {address}\n`{lat:.5f}, {lon:.5f}`", ephemeral=True)


@bot.tree.command(name="unregister", description="Delete your saved address from this server")
@app_commands.guild_only()
async def unregister(interaction: discord.Interaction):
    deleted = bot.db.delete_address(interaction.guild_id, interaction.user.id)
    msg = "🗑️ Your address has been deleted." if deleted else "You didn't have an address saved."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="registered", description="See who in this server has saved an address (names only)")
@app_commands.guild_only()
async def registered(interaction: discord.Interaction):
    users = bot.db.registered_users(interaction.guild_id)
    if not users:
        await interaction.response.send_message("Nobody has registered yet. Use `/register` to start.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"{len(users)} member(s) registered", description=mentions(users, limit=150), color=EMBED_COLOR
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- quick midpoint -------------------------------------------------------


@bot.tree.command(name="midpoint", description="Find the middle meeting point for the members you mention")
@app_commands.guild_only()
@app_commands.describe(
    members="Mention everyone who's meeting, e.g. @alice @bob @carol",
    include_me="Include yourself (default: yes)",
)
async def midpoint_cmd(interaction: discord.Interaction, members: str, include_me: bool = True):
    user_ids = list(dict.fromkeys(int(m) for m in MENTION_RE.findall(members)))
    if include_me and interaction.user.id not in user_ids:
        user_ids.insert(0, interaction.user.id)
    if len(user_ids) < 2:
        await interaction.response.send_message(
            "Mention at least one other member, e.g. `/midpoint members:@alice @bob`.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    try:
        result = await find_midpoint(interaction.guild_id, user_ids)
    except MidpointError as e:
        await interaction.followup.send(str(e), allowed_mentions=discord.AllowedMentions.none())
        return

    embed = discord.Embed(title="Meetup midpoint", color=EMBED_COLOR)
    add_midpoint_field(embed, result.lat, result.lon, result.label)
    embed.add_field(name=f"Based on {len(result.included)} people", value=mentions(result.included), inline=False)
    if result.missing:
        embed.add_field(name="Left out (no address saved)", value=mentions(result.missing), inline=False)
    await interaction.followup.send(embed=embed)


# --- interactive meetup (people opt in with a button) --------------------


def render_meetup(meetup: Meetup, participants: list[int]) -> discord.Embed:
    embed = discord.Embed(
        title=f"📅 {meetup.title}",
        description="Click **I'm in** to join. Addresses stay private — only the group's midpoint is shown.",
        color=EMBED_COLOR,
    )
    embed.add_field(name=f"Who's in ({len(participants)})", value=mentions(participants) or "Nobody yet", inline=False)
    if meetup.mid_lat is not None:
        add_midpoint_field(embed, meetup.mid_lat, meetup.mid_lon, meetup.mid_label)
    embed.set_footer(text="Organizer: press “Find midpoint” once everyone has joined.")
    return embed


class MeetupView(discord.ui.View):
    """Persistent view: state lives in the DB keyed by message id, so buttons keep working after restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _load(self, interaction: discord.Interaction) -> Meetup | None:
        meetup = bot.db.get_meetup(interaction.message.id)
        if meetup is None:
            await interaction.response.send_message("This meetup no longer exists.", ephemeral=True)
        return meetup

    async def _refresh(self, interaction: discord.Interaction, message_id: int) -> None:
        meetup = bot.db.get_meetup(message_id)
        await interaction.response.edit_message(embed=render_meetup(meetup, bot.db.participants(message_id)))

    @discord.ui.button(label="I'm in", emoji="✋", style=discord.ButtonStyle.success, custom_id="meetup:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        meetup = await self._load(interaction)
        if meetup is None:
            return
        if bot.db.get_address(meetup.guild_id, interaction.user.id) is None:
            await interaction.response.send_message(
                "📮 Save your address first with `/register`, then click **I'm in** again. "
                "Nobody else will see it.",
                ephemeral=True,
            )
            return
        if not bot.db.join_meetup(meetup.message_id, interaction.user.id):
            await interaction.response.send_message("You're already in!", ephemeral=True)
            return
        await self._refresh(interaction, meetup.message_id)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="meetup:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        meetup = await self._load(interaction)
        if meetup is None:
            return
        if not bot.db.leave_meetup(meetup.message_id, interaction.user.id):
            await interaction.response.send_message("You weren't in this meetup.", ephemeral=True)
            return
        await self._refresh(interaction, meetup.message_id)

    @discord.ui.button(label="Find midpoint", emoji="📍", style=discord.ButtonStyle.primary, custom_id="meetup:find")
    async def find(self, interaction: discord.Interaction, button: discord.ui.Button):
        meetup = await self._load(interaction)
        if meetup is None:
            return
        if interaction.user.id != meetup.organizer_id:
            await interaction.response.send_message(
                f"Only the organizer (<@{meetup.organizer_id}>) can calculate the midpoint.", ephemeral=True
            )
            return

        await interaction.response.defer()
        participants = bot.db.participants(meetup.message_id)
        try:
            result = await find_midpoint(meetup.guild_id, participants)
        except MidpointError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        bot.db.set_midpoint(meetup.message_id, result.lat, result.lon, result.label)
        meetup = bot.db.get_meetup(meetup.message_id)
        await interaction.edit_original_response(embed=render_meetup(meetup, participants))
        if result.missing:
            await interaction.followup.send(
                f"Heads up: {mentions(result.missing)} left out — no address saved.", ephemeral=True
            )


@bot.tree.command(name="meetup", description="Start a meetup: people click to join, then you get their midpoint")
@app_commands.guild_only()
@app_commands.describe(title="What's the plan? e.g. 'Friday dinner'")
async def meetup_cmd(interaction: discord.Interaction, title: app_commands.Range[str, 1, 100] = "Meetup"):
    draft = Meetup(0, interaction.guild_id, interaction.user.id, title, None, None, None)
    await interaction.response.send_message(embed=render_meetup(draft, [interaction.user.id]), view=MeetupView())
    message = await interaction.original_response()
    bot.db.create_meetup(message.id, interaction.guild_id, interaction.user.id, title)

    if bot.db.get_address(interaction.guild_id, interaction.user.id) is None:
        await interaction.followup.send(
            "You're in this meetup but haven't saved an address — run `/register` so you're counted.",
            ephemeral=True,
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Command failed", exc_info=error)
    msg = "Something went wrong — please try again."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file (see .env.example).")
    bot.run(TOKEN)
