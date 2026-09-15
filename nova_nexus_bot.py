"""Nova Nexus 3 engine community bot.

Runs with:  DISCORD_TOKEN=<token> [GUILD_ID=<server id>] python3 nova_nexus_bot.py
Needs the "Server Members Intent" enabled in the Discord developer portal
for welcome messages. Invite with the `bot` and `applications.commands`
scopes so slash commands show up.
"""
import asyncio
import io
import json
import os
import re
import time
import urllib.request
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: instant slash-command sync

intents = discord.Intents.default()
# NOTE: Server Members Intent is off until it's enabled in the developer
# portal (Bot page -> Privileged Gateway Intents). Flip the toggle there
# and set this True to enable welcome messages.
intents.members = False
# NOTE: Message Content Intent must be ON in the developer portal
# (Bot page -> Privileged Gateway Intents) for the no-swearing filter
# to see message text. Flip the toggle there.
intents.message_content = True


def _install_lenient_websocket_handshake() -> None:
    """Accept Discord's websocket upgrade despite the egress proxy.

    The sandbox egress proxy rewrites `Connection: upgrade` to
    `Connection: close` on the 101 response. The upgrade itself succeeds
    (valid Sec-WebSocket-Accept), so this repairs the raw bytes before
    aiohttp's C parser sees them. That makes the parser set its upgrade
    flag correctly, which keeps the connection alive for the websocket
    and lets the handshake check pass.

    Only chunks belonging to a 101 Switching Protocols response are
    touched; everything else passes through byte-identical.
    """
    import re

    import aiohttp

    _ws101_marker = re.compile(rb"101 Switching Protocols")
    _conn_close_re = re.compile(rb"(?i)(^|\r\n)connection:\s*close(?=\r\n)")

    handler_cls = aiohttp.client_proto.ResponseHandler
    orig_data_received = handler_cls.data_received

    def data_received(self, data: bytes):  # noqa: ANN001, ANN202
        try:
            if data and not self._upgraded:
                fixup = getattr(self, "_ws101_fixup", False)
                if not fixup and _ws101_marker.search(data):
                    fixup = True
                if fixup:
                    data = _conn_close_re.sub(br"\1Connection: upgrade", data)
                    if b"\r\n\r\n" in data:
                        fixup = False
                try:
                    self._ws101_fixup = fixup
                except AttributeError:
                    pass  # __slots__: single-chunk case still works
        except Exception:  # noqa: BLE001
            pass
        return orig_data_received(self, data)

    handler_cls.data_received = data_received

    # Belt and suspenders: also repair the parsed headers, in case the
    # 101 arrives through a path that bypasses data_received.
    from multidict import CIMultiDict, CIMultiDictProxy

    orig_start = aiohttp.ClientResponse.start

    async def start(self, connection):  # noqa: ANN001, ANN202
        result = await orig_start(self, connection)
        try:
            hdrs = self._headers
            if (
                self.status == 101
                and hdrs.get("Upgrade", "").lower() == "websocket"
                and hdrs.get("Connection", "").lower() != "upgrade"
            ):
                new_headers = CIMultiDict(hdrs)
                new_headers["Connection"] = "upgrade"
                proxied = CIMultiDictProxy(new_headers)
                self._headers = proxied
                cache = getattr(self, "_cache", None)
                if isinstance(cache, dict):
                    cache["headers"] = proxied
        except Exception:  # noqa: BLE001
            pass
        return result

    aiohttp.ClientResponse.start = start


_install_lenient_websocket_handshake()


def _proxy_kwargs() -> dict:
    """Route Discord traffic through the egress proxy, if one is configured.

    Reads the standard proxy env vars; credentials stay in memory only and
    are never logged.
    """
    from urllib.parse import unquote, urlparse

    raw = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    if not raw:
        return {}
    parts = urlparse(raw)
    if not parts.hostname:
        return {}
    proxy = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        proxy += f":{parts.port}"
    kwargs: dict = {"proxy": proxy}
    if parts.username:
        import aiohttp

        kwargs["proxy_auth"] = aiohttp.BasicAuth(
            unquote(parts.username), unquote(parts.password or "")
        )
    return kwargs


bot = commands.Bot(command_prefix="!", intents=intents, **_proxy_kwargs())

# --- Engine Q&A knowledge base -------------------------------------------
# Local, free, no API keys. Add entries as (keywords, answer).
ENGINE_FAQ = [
    (
        ("what is", "nova nexus 3", "about"),
        "**Nova Nexus 3** is a game and cinematic engine built for the "
        "Nova Frontier universe — real-time rendering, animation, effects, "
        "and cinematic tools. It's a Windows desktop app vision, not a web app.",
    ),
    (
        ("platform", "windows", "mac", "macos", "run on", "os"),
        "Nova Nexus 3 targets **Windows 11** and **macOS**.",
    ),
    (
        ("system", "renderer", "rendering", "animation", "effects", "hyperspace", "fleet"),
        "The engine is designed around core systems: **renderer, animation, "
        "effects, hyperspace, and fleets**.",
    ),
    (
        ("studio", "trailer", "cinematic", "video"),
        "The **Novaraxis Engine Studio** is the trailer/cinematic tool — "
        "it renders cinematic shots of the fleet with quality profiles, "
        "letterbox framing, and title cards. Trailer-studio work is in progress.",
    ),
    (
        ("wicked", "fork", "editor"),
        "The Studio is being built from a clean **Wicked Engine fork**, "
        "which ships a full editor (animation, camera, content browser windows).",
    ),
    (
        ("digital overlord", "villain", "bad guy", "enemy"),
        "The **Digital Overlord** is the villain of the Nova Frontier universe. "
        "Not a hero.",
    ),
    (
        ("nova frontier", "universe", "world", "game"),
        "**Nova Frontier** is the game/cinematic universe Nova Nexus 3 is "
        "being built for — capital ships, ring stations, fighter fleets.",
    ),
    (
        ("beyblade", "valtriac", "avior", "bey"),
        "Nova Frontier Beyblades include **Brave Valtriac** (attack type) and "
        "**Inferno Avior** (balance type, phoenix design). They duel in the "
        "Holo Deck's Beyblade Duel mode.",
    ),
    (
        ("holo deck", "ultron", "3d", "viewport"),
        "The **Holo Deck** is a live 3D viewport that renders the Nova Nexus 3 "
        "procedural fleet — capital ships, ring station, and fighters.",
    ),
    (
        ("unreal", "ue5", "unreal engine"),
        "Unreal Engine 5 was evaluated for cinematic trailers but parked — "
        "the active plan builds the Studio from the Wicked Engine fork.",
    ),
    (
        ("repo", "github", "code", "source", "contribute", "access", "join"),
        "The Nova Nexus 3 repo is **private** — ask the owner for access if "
        "you want in.",
    ),
    (
        ("status", "progress", "done", "when", "release", "update"),
        "Active work: the trailer studio's procedural models and cinematic FX. "
        "Ask the owner for the latest status.",
    ),
    (
        ("appeal",),
        "To appeal a warning, timeout, kick, or ban, **DM a moderator directly** "
        "— don't argue rulings in public channels.",
    ),
    (
        ("report", "reporting"),
        "To report a member, open a ticket with their username and a description "
        "of what happened. Screenshots help.",
    ),
    (
        ("role", "roles"),
        "For server roles, ask a moderator — they handle role requests.",
    ),
]


def answer_question(question: str) -> str | None:
    """Return the FAQ answer, or None when nothing matches."""
    q = question.lower()
    generic = {"what is", "about"}
    best, best_hits = None, 0
    for keywords, answer in ENGINE_FAQ:
        hits = sum(1 for kw in keywords if kw in q)
        specific = sum(1 for kw in keywords if kw in q and kw not in generic)
        if hits > best_hits and specific >= 1:
            best, best_hits = answer, hits
    return best


_ASK_FALLBACK = (
    "I only talk about the **Nova Nexus 3 engine**. Ask me something "
    "about it — what it is, the Studio, the ships, the villain, "
    "anything engine."
)


@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="Nova Nexus 3 Engine"
        )
    )
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            bot.tree.clear_commands(guild=None)
            synced = await bot.tree.sync(guild=guild)
            await bot.tree.sync()
        else:
            synced = await bot.tree.sync()
        print(f"[nova-nexus] online as {bot.user} — synced {len(synced)} commands")
    except Exception as e:  # noqa: BLE001
        print(f"[nova-nexus] command sync failed: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    channel = discord.utils.get(member.guild.text_channels, name="welcome") or (
        member.guild.system_channel
    )
    if channel is None:
        return
    embed = discord.Embed(
        title="Welcome to the Nova Nexus 3 Engine Community",
        description=(
            f"{member.mention}, glad you made it.\n\n"
            "Build clearly. Govern openly. Ship together."
        ),
        color=0x14B8A6,
    )
    embed.set_footer(text="Type /help to see what I can do.")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print(f"[nova-nexus] cannot post welcome in #{channel.name}")


@bot.tree.command(name="help", description="Show what the Nova Nexus bot can do.")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Nova Nexus 3 Bot — Commands",
        color=0x14B8A6,
    )
    embed.add_field(name="/help", value="This list.", inline=False)
    embed.add_field(name="/engine", value="What Nova Nexus 3 is.", inline=False)
    embed.add_field(
        name="/ask <question>",
        value="Ask me anything about the engine.",
        inline=False,
    )
    embed.add_field(name="/rules", value="Community rules.", inline=False)
    embed.add_field(name="/links", value="Where to find the project.", inline=False)
    embed.add_field(
        name="/kick, /ban, /timeout",
        value="Moderation (mods only).",
        inline=False,
    )
    embed.add_field(
        name="/ticket setup",
        value="Create the ticket center (mods only).",
        inline=False,
    )
    embed.add_field(
        name="/announce setup",
        value="Create the announcements channel for engine updates (mods only).",
        inline=False,
    )
    embed.add_field(
        name="Chat with me",
        value="Mention me or reply to me in any channel and I'll answer engine questions.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="engine", description="What is Nova Nexus 3?")
async def engine_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Nova Nexus 3** is a game/cinematic engine project — "
        "built for Windows 11 and macOS, aimed at real-time rendering, "
        "animation, and cinematic tools.\n\n"
        "Build clearly. Govern openly. Ship together."
    )


@bot.tree.command(name="ask", description="Ask anything about the Nova Nexus 3 engine.")
async def ask_cmd(interaction: discord.Interaction, question: str):
    await interaction.response.send_message(answer_question(question) or _ASK_FALLBACK)


@bot.tree.command(name="rules", description="Community rules.")
async def rules_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    text = await asyncio.to_thread(_fetch_rules)
    if not text:
        await interaction.followup.send(
            "Couldn't load the rules from GitHub — try again in a bit."
        )
        return
    files = []
    for fname in _POLICY_FILES:
        data = await asyncio.to_thread(_fetch_github_file, fname)
        if data:
            files.append(
                discord.File(io.BytesIO(data.encode("utf-8")), filename=fname)
            )
    await interaction.followup.send(text, files=files)


_GITHUB_RAW = (
    "https://raw.githubusercontent.com/howarthjakob4-prog/Nova-Nexus-3/main/"
)
_RULES_TOKEN = os.environ.get("RULES_TOKEN")  # PAT with read access to Nova-Nexus-3
_POLICY_FILES = (
    "ENGINE_RULES.md",
    "NOVA_NEXUS_3_LICENSE.md",
    "EPIC_UNREAL_LICENSE_RULES.md",
)


def _fetch_github_file(name: str) -> str:
    """Pull any file from the Nova-Nexus-3 repo (single source of truth)."""
    try:
        headers = {"User-Agent": "nova-nexus-3-bot"}
        if _RULES_TOKEN:
            headers["Authorization"] = f"Bearer {_RULES_TOKEN}"
        req = urllib.request.Request(_GITHUB_RAW + name, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _fetch_rules() -> str:
    """Pull the live rules from Nova-Nexus-3's DISCORD_RULES.md (single source)."""
    return _fetch_github_file("DISCORD_RULES.md")[:1900]


@bot.tree.command(name="links", description="Where to find the Nova Nexus 3 project.")
async def links_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Nova Nexus 3 lives on GitHub (private repo — ask the owner for access)."
    )


def _mod_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions if interaction.guild else None
        return bool(perms and (perms.kick_members or perms.ban_members))

    return app_commands.check(predicate)


@bot.tree.command(name="kick", description="Kick a member (mods only).")
@_mod_only()
async def kick_cmd(
    interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"Kicked {member.mention}: {reason}")


@bot.tree.command(name="ban", description="Ban a member (mods only).")
@_mod_only()
async def ban_cmd(
    interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"Banned {member.mention}: {reason}")


@bot.tree.command(name="timeout", description="Time out a member (mods only).")
@_mod_only()
async def timeout_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: int = 10,
    reason: str = "No reason given",
):
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await interaction.response.send_message(
        f"Timed out {member.mention} for {minutes}m: {reason}"
    )


# --- Ticket center -----------------------------------------------------------
# /ticket setup (mods) auto-creates the ticket center: a "Tickets" category,
# a #ticket-center channel with an "Open a Ticket" panel, and per-user
# private ticket channels with a close flow. All buttons use static
# custom_ids and are re-registered as persistent views on startup, so they
# keep working across bot restarts. The bot needs the "Manage Channels"
# permission in the server for setup to work.

_TICKET_CATEGORY = "Tickets"
_TICKET_PANEL_CHANNEL = "ticket-center"
_TICKET_STAFF_ROLE_NAMES = ("moderator", "mod", "staff", "admin")


def _manage_server():
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions if interaction.guild else None
        return bool(perms and perms.manage_guild)

    return app_commands.check(predicate)


def _ticket_staff_roles(guild: discord.Guild) -> list:
    named = [r for r in guild.roles if r.name.lower() in _TICKET_STAFF_ROLE_NAMES]
    # Also cover staff whose powers come from role permissions rather than
    # the role name (e.g. a "Helpers" role with kick/ban permissions).
    extra = [
        r
        for r in guild.roles
        if r not in named
        and r != guild.default_role
        and (
            r.permissions.administrator
            or r.permissions.kick_members
            or r.permissions.ban_members
        )
    ]
    return named + extra


def _is_ticket_staff(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.kick_members or perms.ban_members:
        return True
    staff_ids = {r.id for r in _ticket_staff_roles(member.guild)}
    return any(r.id in staff_ids for r in member.roles)


def _ticket_owner_id(channel: discord.abc.GuildChannel):
    topic = channel.topic or ""
    if topic.startswith("ticket:"):
        try:
            return int(topic.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _ticket_channel_name(user: discord.abc.User) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-") or "user"
    return f"ticket-{base[:80]}"


class TicketPanelView(discord.ui.View):
    """Persistent 'Open a Ticket' panel shown in #ticket-center."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open a Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="n3_ticket_open",
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.followup.send(
                "Tickets only work inside the server.", ephemeral=True
            )
            return
        # Serialize creation per user: two rapid clicks must not both pass
        # the duplicate scan before either channel exists.
        key = (guild.id, user.id)
        if key in _ticket_creating:
            await interaction.followup.send(
                "Your ticket is already being created — one moment.",
                ephemeral=True,
            )
            return
        _ticket_creating.add(key)
        try:
            await self._create_ticket(interaction, guild, user)
        finally:
            _ticket_creating.discard(key)

    async def _create_ticket(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        user: discord.Member,
    ):
        category = discord.utils.get(guild.categories, name=_TICKET_CATEGORY)
        if category is None:
            await interaction.followup.send(
                "The ticket center isn't set up yet — "
                "a mod needs to run /ticket setup first.",
                ephemeral=True,
            )
            return
        marker = f"ticket:{user.id}"
        for ch in category.text_channels:
            if ch.topic == marker:
                await interaction.followup.send(
                    f"You already have an open ticket: {ch.mention}",
                    ephemeral=True,
                )
                return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        for role in _ticket_staff_roles(guild):
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
        # The @everyone denial above would also hide the channel from the bot
        # itself — grant it explicit access so it can post and manage tickets.
        me = guild.me
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        name = _ticket_channel_name(user)
        if discord.utils.get(category.text_channels, name=name):
            name = f"{name}-{str(user.id)[-4:]}"
        try:
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                topic=marker,
                reason=f"Ticket opened by {user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create channels. "
                'Give me the "Manage Channels" permission and try again.',
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=f"Ticket — {user.display_name}",
            description=(
                f"{user.mention}, describe your issue and I'll try to answer "
                "right away.\n\n"
                "I've notified the moderators — someone will be with you shortly.\n\n"
                "Press **Close Ticket** below when it's resolved."
            ),
            color=0x14B8A6,
        )
        # Ping staff (or the server owner) so the new ticket gets looked at.
        staff_roles = _ticket_staff_roles(guild)
        pings = [r.mention for r in staff_roles]
        if not pings and guild.owner:
            pings = [guild.owner.mention]
        await channel.send(
            " ".join(pings) if pings else "New ticket opened.",
            embed=embed,
            view=TicketCloseView(),
        )
        await interaction.followup.send(
            f"Your ticket is ready: {channel.mention}", ephemeral=True
        )


class TicketCloseView(discord.ui.View):
    """Per-ticket 'Close Ticket' button. The channel is interaction.channel,
    so the custom_id stays static and the view is persistent."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="n3_ticket_close",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        channel = interaction.channel
        user = interaction.user
        if (
            channel is None
            or interaction.guild is None
            or not isinstance(channel, discord.TextChannel)
            or _ticket_owner_id(channel) is None
        ):
            await interaction.response.send_message(
                "This isn't a ticket channel.", ephemeral=True
            )
            return
        owner_id = _ticket_owner_id(channel)
        is_staff = isinstance(user, discord.Member) and _is_ticket_staff(user)
        if user.id != owner_id and not is_staff:
            await interaction.response.send_message(
                "Only the ticket owner or staff can close this ticket.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Close this ticket? This deletes the channel.",
            view=TicketCloseConfirmView(),
            ephemeral=True,
        )


class TicketCloseConfirmView(discord.ui.View):
    """Yes/No confirmation for closing a ticket (ephemeral)."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(
        label="Yes, close it",
        style=discord.ButtonStyle.danger,
        custom_id="n3_ticket_yes",
    )
    async def confirm_yes(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        channel = interaction.channel
        if (
            channel is None
            or not isinstance(channel, discord.TextChannel)
            or _ticket_owner_id(channel) is None
        ):
            await interaction.response.send_message(
                "This ticket no longer exists.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Closing the ticket…", ephemeral=True
        )
        try:
            await channel.delete(
                reason=f"Ticket closed by {interaction.user}"
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Keep open",
        style=discord.ButtonStyle.secondary,
        custom_id="n3_ticket_no",
    )
    async def confirm_no(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message("Ticket kept open.", ephemeral=True)


async def _ticket_setup_hook() -> None:
    # NOTE: TicketCloseConfirmView is NOT registered here — it has a timeout
    # (ephemeral confirm), and add_view() raises ValueError for non-persistent
    # views, which would abort startup. It is attached when created instead.
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())
    _ticket_watchdog.start()


bot.setup_hook = _ticket_setup_hook

ticket_group = app_commands.Group(name="ticket", description="Support tickets.")


@ticket_group.command(name="setup", description="Create the ticket center (mods only).")
@_manage_server()
async def ticket_setup_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Run this inside the server.", ephemeral=True)
        return
    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        await interaction.followup.send(
            'I need the "Manage Channels" permission to set up the ticket center. '
            "Give the bot that permission and run /ticket setup again.",
            ephemeral=True,
        )
        return
    category = discord.utils.get(guild.categories, name=_TICKET_CATEGORY)
    try:
        if category is None:
            category = await guild.create_category(
                _TICKET_CATEGORY, reason="Ticket center setup"
            )
        channel = discord.utils.get(
            category.text_channels, name=_TICKET_PANEL_CHANNEL
        )
        if channel is None:
            channel = await category.create_text_channel(
                _TICKET_PANEL_CHANNEL,
                topic="Open a support ticket.",
                reason="Ticket center setup",
            )
    except discord.Forbidden:
        await interaction.followup.send(
            'I need the "Manage Channels" permission to set up the ticket center. '
            "Give the bot that permission and run /ticket setup again.",
            ephemeral=True,
        )
        return
    # Refresh the panel: remove my old panel messages, then post a new one.
    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user and msg.components:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
    except discord.HTTPException:
        pass
    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Need help, want to report something, or have a question for the mods?\n\n"
            "Press **Open a Ticket** below and a private channel will be "
            "created for you and the staff."
        ),
        color=0x14B8A6,
    )
    await channel.send(embed=embed, view=TicketPanelView())
    await interaction.followup.send(
        f"Ticket center is ready in {channel.mention}.", ephemeral=True
    )


bot.tree.add_command(ticket_group)

# --- Engine announcements -------------------------------------------------
# /announce setup (mods) creates an #announcements channel plus an
# "Engine Updates" webhook in it, then wires the Nova-Nexus-3 repo to post
# there on every push to main: it stores the webhook URL as the
# DISCORD_ANNOUNCE_WEBHOOK Actions secret and opens a PR adding
# .github/workflows/announce.yml. The webhook posts straight to Discord,
# so announcements land even while the bot itself is offline.

_ANNOUNCE_CHANNEL = "announcements"
_ENGINE_REPO_OWNER = "howarthjakob4-prog"
_ENGINE_REPO = "Nova-Nexus-3"
_ANNOUNCE_SECRET_NAME = "DISCORD_ANNOUNCE_WEBHOOK"
_ANNOUNCE_WORKFLOW_BRANCH = "feature/engine-announce-webhook"
_ANNOUNCE_WORKFLOW_PATH = ".github/workflows/announce.yml"

_ANNOUNCE_WORKFLOW_YAML = """name: Engine announcements

on:
  push:
    branches: [main]

jobs:
  announce:
    runs-on: ubuntu-latest
    steps:
      - name: Post the update to Discord
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_ANNOUNCE_WEBHOOK }}
          HEAD_COMMIT: ${{ toJSON(github.event.head_commit) }}
          ACTOR: ${{ github.actor }}
        run: |
          python3 - <<'PYEOF'
          import json, os, urllib.request
          webhook = os.environ["DISCORD_WEBHOOK"]
          head = json.loads(os.environ["HEAD_COMMIT"])  # JSON text, not a Python literal
          summary = (head.get("message") or "").split("\\n")[0][:256]
          embed = {
              "title": "⚙️ Nova Nexus 3 engine updated",
              "description": summary or "A new change was pushed.",
              "url": head.get("url"),
              "color": 0x14B8A6,
              "fields": [
                  {"name": "By", "value": os.environ["ACTOR"], "inline": True},
                  {
                      "name": "Commit",
                      "value": (head.get("id") or "")[:7] or "—",
                      "inline": True,
                  },
              ],
          }
          req = urllib.request.Request(
              webhook,
              data=json.dumps({"embeds": [embed]}).encode(),
              headers={"Content-Type": "application/json"},
              method="POST",
          )
          urllib.request.urlopen(req)
          print("announced", (head.get("id") or "")[:7])
          PYEOF
"""


def _github_api(method, path, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "nova-nexus-3-discord-bot",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


def _wire_engine_announcements(webhook_url: str) -> str:
    """Store the webhook as a repo secret and open the workflow PR.

    Returns the PR URL. Raises RuntimeError with a short message on failure.
    """
    token = _RULES_TOKEN
    if not token:
        raise RuntimeError("no GitHub token available")
    repo = f"/repos/{_ENGINE_REPO_OWNER}/{_ENGINE_REPO}"
    try:
        # 1. Save the webhook URL as an Actions secret (sealed-box encrypted).
        import base64 as _b64

        from nacl.public import PublicKey, SealedBox

        pub = _github_api("GET", repo + "/actions/secrets/public-key", token)
        sealed = SealedBox(PublicKey(_b64.b64decode(pub["key"])))
        encrypted = _b64.b64encode(sealed.encrypt(webhook_url.encode())).decode()
        _github_api(
            "PUT",
            repo + "/actions/secrets/" + _ANNOUNCE_SECRET_NAME,
            token,
            {"encrypted_value": encrypted, "key_id": pub["key_id"]},
        )
        # 2. Branch off main and add the workflow file.
        main_ref = _github_api("GET", repo + "/git/ref/heads/main", token)
        main_sha = main_ref["object"]["sha"]
        try:
            _github_api(
                "POST",
                repo + "/git/refs",
                token,
                {"ref": "refs/heads/" + _ANNOUNCE_WORKFLOW_BRANCH, "sha": main_sha},
            )
        except urllib.error.HTTPError as e:
            if e.code != 422:  # 422 = branch already exists; reuse it
                raise
        # Reruns must update the existing file: GitHub requires its blob SHA.
        file_payload = {
            "message": "Post engine updates to the Discord announcements channel",
            "content": _b64.b64encode(_ANNOUNCE_WORKFLOW_YAML.encode()).decode(),
            "branch": _ANNOUNCE_WORKFLOW_BRANCH,
        }
        try:
            existing = _github_api(
                "GET",
                repo + "/contents/" + _ANNOUNCE_WORKFLOW_PATH
                + "?ref=" + _ANNOUNCE_WORKFLOW_BRANCH,
                token,
            )
            if isinstance(existing, dict) and existing.get("sha"):
                file_payload["sha"] = existing["sha"]
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = first run, nothing to update
                raise
        _github_api(
            "PUT",
            repo + "/contents/" + _ANNOUNCE_WORKFLOW_PATH,
            token,
            file_payload,
        )
        # 3. Open the PR.
        pr = _github_api(
            "POST",
            repo + "/pulls",
            token,
            {
                "title": "Announce engine updates in Discord",
                "head": _ANNOUNCE_WORKFLOW_BRANCH,
                "base": "main",
                "body": (
                    "Posts a message to the #announcements channel "
                    "(via the `DISCORD_ANNOUNCE_WEBHOOK` secret) on every push "
                    "to `main`.\n\nMerge this to turn engine update "
                    "announcements on."
                ),
            },
        )
        return pr["html_url"]
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                "GitHub token was rejected (HTTP %d) — it needs the 'repo' "
                "scope to wire up announcements." % e.code
            )
        raise RuntimeError(f"GitHub API error {e.code}")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(str(e))


announce_group = app_commands.Group(
    name="announce", description="Engine announcements."
)


@announce_group.command(
    name="setup", description="Create the announcements channel (mods only)."
)
@_manage_server()
async def announce_setup_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Run this inside the server.", ephemeral=True)
        return
    me = guild.me
    if me is None or not (
        me.guild_permissions.manage_channels and me.guild_permissions.manage_webhooks
    ):
        await interaction.followup.send(
            'I need the "Manage Channels" and "Manage Webhooks" permissions '
            "to set up announcements. Give the bot those permissions and run "
            "/announce setup again.",
            ephemeral=True,
        )
        return
    channel = discord.utils.get(guild.text_channels, name=_ANNOUNCE_CHANNEL)
    try:
        if channel is None:
            channel = await guild.create_text_channel(
                _ANNOUNCE_CHANNEL,
                topic="Nova Nexus 3 engine updates.",
                reason="Announcements setup",
            )
        webhook = discord.utils.get(await channel.webhooks(), name="Engine Updates")
        if webhook is None:
            webhook = await channel.create_webhook(
                name="Engine Updates", reason="Announcements setup"
            )
    except discord.Forbidden:
        await interaction.followup.send(
            'I need the "Manage Channels" and "Manage Webhooks" permissions '
            "to set up announcements. Give the bot those permissions and run "
            "/announce setup again.",
            ephemeral=True,
        )
        return
    try:
        pr_url = await asyncio.to_thread(_wire_engine_announcements, webhook.url)
        wired = True
    except RuntimeError as e:
        print(f"[nova-nexus] announce wiring failed: {e}")
        wired = False
    embed = discord.Embed(
        title="📢 Announcements",
        description=(
            "This channel will post **Nova Nexus 3 engine updates** — "
            "every change pushed to the engine lands here automatically."
        ),
        color=0x14B8A6,
    )
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass
    if wired:
        await interaction.followup.send(
            f"Announcements are ready in {channel.mention}. I opened {pr_url} "
            "in the engine repo — merge it and updates will post here automatically.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"{channel.mention} is created, but I couldn't wire up the engine "
            "repo automatically. Create a webhook in the channel settings, "
            "add its URL as the `DISCORD_ANNOUNCE_WEBHOOK` secret in the "
            "Nova-Nexus-3 repo, and add a workflow that posts to it on push to main.",
            ephemeral=True,
        )


bot.tree.add_command(announce_group)


# --- Ticket auto-answer + takeover -------------------------------------------
# Inside ticket channels the bot answers questions itself using the engine
# FAQ. If a ticket sits with no staff reply for _TAKEOVER_AFTER_MIN minutes,
# the watchdog takes over: it answers from the FAQ when it can, and tells
# the user a moderator will follow up when one can't.

_TAKEOVER_AFTER_MIN = 10
_ticket_acked: set[int] = set()       # tickets that got the "someone will be with you shortly" note
_ticket_answered: set[int] = set()    # user message ids the bot already answered
_ticket_taken_over: set[int] = set()  # tickets the watchdog already took over
_ticket_creating: set[tuple[int, int]] = set()  # (guild_id, user_id) with a ticket being created

_QUESTION_START = (
    "who", "what", "when", "where", "why", "how", "can", "is", "are",
    "do", "does", "which", "should",
)


def _looks_like_question(text: str) -> bool:
    t = text.strip().lower()
    return "?" in t or t.startswith(_QUESTION_START)


def _is_ticket_channel(channel) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category is not None
        and channel.category.name == _TICKET_CATEGORY
        and _ticket_owner_id(channel) is not None
    )


async def _handle_ticket_message(message: discord.Message):
    """Auto-answer questions posted in ticket channels."""
    author = message.author
    if author.bot or not isinstance(author, discord.Member):
        return
    if _is_ticket_staff(author):
        return  # staff are talking; stay out of the way
    text = (message.content or "").strip()
    if not text:
        return
    answer = answer_question(text)
    try:
        if answer:
            await message.reply(answer)
            _ticket_answered.add(message.id)
        elif (
            _looks_like_question(text)
            and message.channel.id not in _ticket_acked
        ):
            _ticket_acked.add(message.channel.id)
            await message.channel.send(
                f"{author.mention}, noted — someone will be with you shortly."
            )
    except discord.HTTPException:
        pass


@tasks.loop(minutes=5)
async def _ticket_watchdog():
    """Take over tickets that have no staff reply."""
    for guild in bot.guilds:
        category = discord.utils.get(guild.categories, name=_TICKET_CATEGORY)
        if category is None:
            continue
        for ch in category.text_channels:
            if _ticket_owner_id(ch) is None or ch.id in _ticket_taken_over:
                continue
            try:
                last_user_msg = None
                async for msg in ch.history(limit=30):
                    if msg.author.bot:
                        continue
                    if isinstance(msg.author, discord.Member) and _is_ticket_staff(
                        msg.author
                    ):
                        last_user_msg = None  # staff have the latest word
                    else:
                        last_user_msg = msg
                    break
                if last_user_msg is None or last_user_msg.id in _ticket_answered:
                    continue
                age_min = (
                    discord.utils.utcnow() - last_user_msg.created_at
                ).total_seconds() / 60
                if age_min < _TAKEOVER_AFTER_MIN:
                    continue
                answer = answer_question(last_user_msg.content or "")
                if answer:
                    text = (
                        "No moderator is available right now, so I'll take this one. "
                        f"{last_user_msg.author.mention}\n\n{answer}"
                    )
                else:
                    text = (
                        f"{last_user_msg.author.mention}, no moderator is available "
                        "right now. I've flagged your question and a moderator "
                        "will follow up as soon as they're back."
                    )
                await ch.send(text)
                _ticket_taken_over.add(ch.id)
            except discord.HTTPException:
                pass


@_ticket_watchdog.before_loop
async def _ticket_watchdog_before():
    await bot.wait_until_ready()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use that."
    else:
        msg = f"Something went wrong: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# No-swearing rule: if a message contains profanity, the bot replies
# "Please do not swear." One warning per user per minute so it can't
# be used to spam the channel.
_PROFANITY = re.compile(
    r"\b("
    r"fuck(?:er|ing|ed|s)?|motherfucker|"
    r"shit(?:ty|ting|s)?|bullshit|"
    r"bitch(?:es|ing)?|"
    r"ass(?:hole|es)?|"
    r"dick(?:head|s)?|"
    r"bastard|damn|hell|"
    r"cunt|whore|slut|twat|"
    r"pussy|cock|tits|"
    r"douche(?:bag)?|"
    r"piss(?:ed|ing)?|crap|"
    r"fag(?:got)?|nigga|nigger|retard|"
    r"wanker|bollocks"
    r")\b",
    re.IGNORECASE,
)
_swear_warned_at: dict[int, float] = {}
_SWEAR_COOLDOWN_S = 60.0

_DM_GREETINGS = {"hi", "hello", "hey", "yo", "sup", "hiya", "howdy", "greetings"}
_DM_MORNING = ("good morning", "good evening", "good afternoon")


async def _handle_dm(message: discord.Message):
    """Simple conversational replies in direct messages."""
    text = (message.content or "").strip().lower()
    if not text:
        return
    name = message.author.display_name
    words = set(re.findall(r"[a-z']+", text))
    try:
        if words & _DM_GREETINGS or text.startswith(_DM_MORNING):
            await message.channel.send(f"How can I help you, {name}?")
        elif "thank" in text:
            await message.channel.send("You're welcome!")
        elif words & {"bye", "goodbye", "goodnight"} or "good night" in text:
            await message.channel.send(f"Goodbye, {name}! Come back anytime.")
        elif "help" in text:
            await message.channel.send(
                "Try /help to see what I can do, or ask me about the Nova Nexus 3 Engine."
            )
        else:
            await message.channel.send(
                "I'm the Nova Nexus 3 Engine bot. "
                "Ask me about the engine, or try /help."
            )
    except discord.HTTPException:
        pass


_CHAT_COOLDOWN_S = 5.0
_chat_answered_at: dict[int, float] = {}


def _talking_to_bot(message: discord.Message) -> bool:
    """True when the message mentions the bot or replies to it."""
    if bot.user is None:
        return False
    if bot.user in message.mentions:
        return True
    ref = message.reference
    return (
        ref is not None
        and isinstance(ref.resolved, discord.Message)
        and ref.resolved.author == bot.user
    )


async def _handle_chat(message: discord.Message) -> None:
    now = time.monotonic()
    last = _chat_answered_at.get(message.author.id, 0.0)
    if now - last < _CHAT_COOLDOWN_S:
        return
    _chat_answered_at[message.author.id] = now
    content = re.sub(r"<@!?\d+>", "", message.content or "").strip()
    if not content:
        await message.reply(
            "Hey — ask me something about the **Nova Nexus 3 engine**."
        )
        return
    await message.reply(answer_question(content) or _ASK_FALLBACK)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    if message.guild is None:
        await _handle_dm(message)
    elif _PROFANITY.search(message.content or ""):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        now = time.monotonic()
        last = _swear_warned_at.get(message.author.id, 0.0)
        if now - last >= _SWEAR_COOLDOWN_S:
            _swear_warned_at[message.author.id] = now
            try:
                await message.channel.send("Please do not swear.")
            except discord.HTTPException:
                pass
    elif _is_ticket_channel(message.channel):
        await _handle_ticket_message(message)
    elif _talking_to_bot(message):
        await _handle_chat(message)
    await bot.process_commands(message)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    # NOVA_RECONNECT=0 disables auto-reconnect: used by the scheduled
    # GitHub Actions runs so a fresh run cleanly takes over from the
    # previous one instead of the two fighting over the token.
    reconnect = os.environ.get("NOVA_RECONNECT", "1") == "1"
    bot.run(TOKEN, reconnect=reconnect)
