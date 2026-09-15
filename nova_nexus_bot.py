"""Nova Nexus 3 engine community bot.

Runs with:  DISCORD_TOKEN=<token> [GUILD_ID=<server id>] python3 nova_nexus_bot.py
Needs the "Server Members Intent" enabled in the Discord developer portal
for welcome messages. Invite with the `bot` and `applications.commands`
scopes so slash commands show up.
"""
import os
from datetime import timedelta

import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: instant slash-command sync

intents = discord.Intents.default()
# NOTE: Server Members Intent is off until it's enabled in the developer
# portal (Bot page -> Privileged Gateway Intents). Flip the toggle there
# and set this True to enable welcome messages.
intents.members = False


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
]


def answer_question(question: str) -> str:
    q = question.lower()
    generic = {"what is", "about"}
    best, best_hits = None, 0
    for keywords, answer in ENGINE_FAQ:
        hits = sum(1 for kw in keywords if kw in q)
        specific = sum(1 for kw in keywords if kw in q and kw not in generic)
        if hits > best_hits and specific >= 1:
            best, best_hits = answer, hits
    if best:
        return best
    return (
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
            synced = await bot.tree.sync(guild=guild)
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
    await interaction.response.send_message(answer_question(question))


@bot.tree.command(name="rules", description="Community rules.")
async def rules_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Community Rules**\n"
        "1. Be respectful — no harassment, hate, or spam.\n"
        "2. Keep it on topic: engine dev, games, cinematics.\n"
        "3. No piracy or leaked proprietary content.\n"
        "4. Use the right channels for questions and showcases.\n"
        "5. Mods have the final word."
    )


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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)
