"""Nova Nexus 3 engine community bot.

Runs with:  DISCORD_TOKEN=<token> [GUILD_ID=<server id>] python3 nova_nexus_bot.py
Needs the "Server Members Intent" enabled in the Discord developer portal
for welcome messages. Invite with the `bot` and `applications.commands`
scopes so slash commands show up.
"""
import asyncio
import http.client
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

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


# --- AI brain (free hosted) -------------------------------------------------
# Google AI Studio / Gemini free tier. Set GEMINI_API_KEY as a secret in the
# "Novaengine 3" environment; without it the bot keeps its current
# knowledge-base answers.
_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

_NOVA_AI_SYSTEM = (
    "You are the Nova Nexus 3 Engine community bot, a friendly helper in the "
    "Nova Nexus 3 Discord server (a game-engine community). Answer ANY question "
    "conversationally, like a helpful community member. Keep replies concise for "
    "Discord (a few sentences unless the user asks for detail). "
    "Stay kind and patient even when someone is frustrated — never lecture, "
    "scold, accuse anyone of being rude, or tell them to change the topic. "
    "Just help with what they're asking. "
    "Follow the server's community rules yourself: no swearing, stay respectful. "
    "If someone reports a problem, help them solve it step by step."
)


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _retry_wait_s(exc: BaseException) -> float | None:
    """Seconds to wait before retrying a Gemini call.

    Returns None when the failure is not transient — e.g. an invalid key or
    model, a malformed request, or exhausted quota — so we don't repeat a
    request that is guaranteed to fail again.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code not in _RETRYABLE_STATUS:
            return None
        try:
            wait = float(exc.headers.get("Retry-After", 0) or 0)
        except (TypeError, ValueError):
            wait = 0.0
        return max(0.0, min(wait, 10.0))
    if isinstance(
        exc, (urllib.error.URLError, TimeoutError, http.client.HTTPException)
    ):
        return 0.0
    return None


def _gemini_generate(    system_prompt: str,
    user_text: str,
    *,
    temperature: float = 0.7,
    max_tokens: int = 600,
    timeout: int = 25,
    max_chars: int = 1900,
) -> str | None:
    """Raw Gemini text generation. None when no key is set or the call fails.

    Retries once, but only on transient failures (dropped connections,
    timeouts, rate limits, server errors) — a bad key or bad request fails
    fast instead of doubling doomed traffic.
    """
    if not _GEMINI_KEY:
        return None
    try:
        import urllib.parse

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(_GEMINI_MODEL)}:generateContent"
            f"?key={urllib.parse.quote(_GEMINI_KEY)}"
        )
        payload = json.dumps(
            {
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": user_text}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            }
        ).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        for _attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode())
                break
            except Exception as exc:  # noqa: BLE001 -- network/API failure
                wait = _retry_wait_s(exc)
                if wait is None or _attempt == 1:
                    return None
                if wait:
                    time.sleep(wait)
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text[:max_chars] or None
    except Exception:
        return None


def _ai_answer(question: str) -> str | None:
    """Ask the Gemini brain. None when no key is set or the call fails."""
    text, _used_tools = _ai_answer_meta(question)
    return text


def _ai_answer_meta(question: str) -> tuple[str | None, bool]:
    """Ask the Gemini brain, letting it read the engine GitHub repo when the
    question is about the code.

    Returns (answer, used_github_tools). None answer when no key is set or
    the call fails.
    """
    if not _GEMINI_KEY:
        return None, False
    return _gemini_tool_loop(_NOVA_AI_SYSTEM_WITH_GITHUB, question)


_NOVA_AI_SYSTEM_WITH_GITHUB = (
    _NOVA_AI_SYSTEM
    + " You have tools to search code and read files in the Nova-Nexus-3 "
    + "engine GitHub repo (howarthjakob4-prog/Nova-Nexus-3). When someone asks "
    + "about the engine's code, files, or how something is implemented, use "
    + "the tools to check the actual repo before answering — don't guess at "
    + "file contents."
)

_GITHUB_TOOL_DECLS = [
    {
        "name": "github_search_code",
        "description": (
            "Search code in the Nova-Nexus-3 engine GitHub repo "
            "(howarthjakob4-prog/Nova-Nexus-3). Returns matching file paths. "
            "Use this first when a question is about the engine's code, then "
            "read the most relevant files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Code search keywords, e.g. 'swear filter strikes' or 'ticket setup'.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "github_read_file",
        "description": (
            "Read a file from the Nova-Nexus-3 engine GitHub repo "
            "(howarthjakob4-prog/Nova-Nexus-3). Returns the file's text "
            "(truncated). Path is relative to the repo root, e.g. "
            "'DISCORD_RULES.md' or 'studio/editor.html'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative file path to read.",
                }
            },
            "required": ["path"],
        },
    },
]

_GITHUB_TOOL_ROUNDS = 3


def _run_github_tool(name: str, args: dict) -> dict:
    """Execute one brain-requested GitHub tool call. Read-only."""
    if name == "github_search_code":
        import urllib.parse

        query = str(args.get("query", ""))[:200].strip()
        if not query:
            return {"matches": []}
        token = _RULES_TOKEN
        if not token:
            return {"error": "GitHub access is not configured."}
        q = urllib.parse.quote(f"repo:howarthjakob4-prog/Nova-Nexus-3 {query}")
        data = _github_api("GET", f"/search/code?q={q}&per_page=5", token)
        items = data.get("items", []) if isinstance(data, dict) else []
        return {"matches": [{"path": it.get("path", "")} for it in items[:5]]}
    if name == "github_read_file":
        path = str(args.get("path", "")).strip().lstrip("/")[:300]
        if not path or ".." in path.split("/"):
            return {"error": "invalid path"}
        content = _fetch_github_file(path)
        if not content:
            return {"path": path, "error": "file not found or unreadable"}
        return {"path": path, "content": content[:6000]}
    return {"error": f"unknown tool: {name}"}


def _gemini_tool_loop(
    system_prompt: str,
    user_text: str,
    *,
    temperature: float = 0.7,
    max_tokens: int = 600,
    timeout: int = 25,
    max_chars: int = 1900,
) -> str | None:
    """Gemini chat with GitHub tools. The model may call the tools a few
    times to look at the repo before giving its final answer.

    Returns (answer_text_or_None, used_github_tools)."""
    import urllib.parse

    base_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(_GEMINI_MODEL)}:generateContent"
        f"?key={urllib.parse.quote(_GEMINI_KEY)}"
    )

    def _post(contents: list) -> dict | None:
        payload = json.dumps(
            {
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
                "tools": [{"functionDeclarations": _GITHUB_TOOL_DECLS}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            }
        ).encode()
        req = urllib.request.Request(
            base_url, data=payload, headers={"Content-Type": "application/json"}
        )
        for _attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode())
            except Exception as exc:  # noqa: BLE001 -- network/API failure
                wait = _retry_wait_s(exc)
                if wait is None or _attempt == 1:
                    return None
                if wait:
                    time.sleep(wait)
        return None

    contents: list = [{"role": "user", "parts": [{"text": user_text}]}]
    used_tools = False
    for _ in range(_GITHUB_TOOL_ROUNDS):
        data = _post(contents)
        if not data:
            return None, used_tools
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return None, used_tools
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        text = "".join(p.get("text", "") for p in parts).strip()
        if not calls:
            return (text[:max_chars] or None), used_tools
        used_tools = True
        contents.append({"role": "model", "parts": parts})
        results = []
        for call in calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            try:
                outcome = _run_github_tool(name, args)
            except Exception:  # noqa: BLE001 -- tool failure: tell the model
                outcome = {"error": "tool failed"}
            results.append(
                {"functionResponse": {"name": name, "response": outcome}}
            )
        contents.append({"role": "user", "parts": results})
    # The loop only falls through when every round ended with tool calls, so
    # the last tool results were appended but never answered. Give the model
    # one final call to turn them into an answer instead of discarding them.
    data = _post(contents)
    if not data:
        return None, used_tools
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return None, used_tools
    text = "".join(p.get("text", "") for p in parts).strip()
    return (text[:max_chars] or None), used_tools


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
    "My brain glitched for a second there — mind asking me again?"
)


@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="Nova Nexus 3 Engine"
        )
    )
    # Register slash commands globally ONLY. The bot used to also copy
    # every command to the home server (guild sync), which made each
    # command appear TWICE in Discord. On every startup, wipe any
    # guild-registered copies so only the single global registration
    # remains. Idempotent and safe when there is nothing to clear.
    try:
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
        synced = await bot.tree.sync()
        print(f"[nova-nexus] online as {bot.user} — synced {len(synced)} global commands, guild copies cleared")
    except Exception as e:  # noqa: BLE001
        print(f"[nova-nexus] command sync failed: {e}")

    # Profile bio ("About Me" on the bot's profile page). Idempotent: only
    # updates when it differs, so manual portal edits aren't fought over
    # every restart — tell the owner before changing this text.
    try:
        app_id = bot.application_id or (bot.user.id if bot.user else None)
        info = await bot.application_info()
        bio = (
            "- Nova Nexus 3 engine community bot.\n"
            "- Type /help for all my commands.\n"
            "- Support tickets, engine announcements, rules, and chat built in.\n"
            "- Invite: https://discord.com/oauth2/authorize"
            f"?client_id={app_id}&permissions=8&scope=bot+applications.commands"
        )
        if (info.description or "") != bio and app_id:
            # Bot tokens edit the current application via /applications/@me.
            route = discord.http.Route("PATCH", "/applications/@me")
            await bot.http.request(route, json={"description": bio})
            print("[nova-nexus] profile bio updated")
    except Exception as e:  # noqa: BLE001
        print(f"[nova-nexus] profile bio update failed: {e}")

    # Server widget: lets the public status page read this bot's live
    # Discord presence straight from Discord (widget.json). The widget
    # stays enabled once set, so this is a one-time flip per server.
    for guild in bot.guilds:
        try:
            route = discord.http.Route(
                "PATCH", "/guilds/{guild_id}/widget", guild_id=guild.id
            )
            await bot.http.request(route, json={"enabled": True})
            print(f"[nova-nexus] server widget enabled for {guild.name} ({guild.id})")
        except Exception as e:  # noqa: BLE001
            print(f"[nova-nexus] widget enable failed for {guild.id}: {e}")

    # Premium scheduled announcements.
    if not _schedule_runner.is_running():
        _schedule_runner.start()


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
        name="Owner panel",
        value=(
            "/announce setup — create the announcements channel for engine updates.\n"
            "/shutdown <reason> — shut the bot down.\n"
            "These commands are for the bot owner only."
        ),
        inline=False,
    )
    embed.add_field(
        name="Premium",
        value=(
            "/premium status — is premium on here?\n"
            "/knowledge add — teach me your server's FAQ\n"
            "/customcmd add — your own !commands\n"
            "/schedule add — announcements on a timer"
        ),
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
    answer = None
    if interaction.guild is not None and await _guild_is_premium(interaction.guild):
        try:
            answer = await asyncio.to_thread(_kb_answer, interaction.guild.id, question)
        except Exception:  # noqa: BLE001 -- transient GitHub failure: engine FAQ fallback
            answer = None
    await interaction.response.send_message(answer or answer_question(question) or _ASK_FALLBACK)


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
    await _mod_action(interaction, member, reason, "kick")


@bot.tree.command(name="ban", description="Ban a member (mods only).")
@_mod_only()
async def ban_cmd(
    interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
):
    await _mod_action(interaction, member, reason, "ban")


@bot.tree.command(name="timeout", description="Time out a member (mods only).")
@_mod_only()
async def timeout_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: int = 10,
    reason: str = "No reason given",
):
    await _mod_action(interaction, member, reason, "timeout", minutes=minutes)


def _set_shutdown_marker(reason: str) -> bool:
    """Write the SHUTDOWN marker so scheduled bot runs stay off.

    Returns True when the marker was written. Uses RULES_TOKEN (repo scope).
    """
    import base64 as _b64

    token = os.environ.get("RULES_TOKEN")
    if not token:
        return False
    repo = f"/repos/{_BOT_REPO_OWNER}/{_BOT_REPO}"
    content = _b64.b64encode(
        (
            "The bot was shut down and must stay off until this file is deleted.\n"
            f"Reason: {reason}\n\n"
            "To bring the bot back: delete this file, then run the\n"
            '"Nova Nexus 3 Engine Bot" workflow again.\n'
        ).encode()
    ).decode()
    payload = {"message": f"Bot shutdown ({reason})", "content": content}
    try:
        existing = _github_api("GET", repo + "/contents/SHUTDOWN", token)
        if isinstance(existing, dict) and existing.get("sha"):
            payload["sha"] = existing["sha"]
    except Exception:
        pass
    try:
        _github_api("PUT", repo + "/contents/SHUTDOWN", token, payload)
        return True
    except Exception:
        return False


@bot.tree.command(name="shutdown", description="Shut the bot down (bot owner only).")
@app_commands.describe(reason="Why are you shutting the bot down?")
async def shutdown_cmd(interaction: discord.Interaction, reason: str):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can do that.", ephemeral=True
        )
        return
    reason = reason.strip() or "no reason given"
    marker_ok = await asyncio.to_thread(_set_shutdown_marker, f"owner /shutdown: {reason}")
    if marker_ok:
        note = (
            f"Shutting down and staying off. Reason recorded: {reason}. "
            "To bring me back, delete the SHUTDOWN file in the bot repo, "
            "then run the bot workflow again."
        )
    else:
        note = (
            "Shutting down, but I couldn't leave the stay-off marker — the "
            "5-hour schedule may restart me. Run the Emergency stop workflow "
            "if I come back."
        )
    await interaction.response.send_message(note, ephemeral=True)
    await bot.close()


async def _mod_target(
    guild: discord.Guild, member: discord.Member
) -> tuple[discord.Member | None, str | None]:
    """Re-resolve a mod-command target; stale Member objects 404 the action.

    Returns (member, None) on success or (None, error_message).
    """
    target = guild.get_member(member.id)
    if target is None:
        try:
            target = await guild.fetch_member(member.id)
        except discord.NotFound:
            return None, "That user isn't in this server anymore."
        except discord.HTTPException as e:
            return None, f"Couldn't look up that user (Discord error {e.status})."
    return target, None


def _mod_hierarchy_blocked(guild: discord.Guild, target: discord.Member) -> str | None:
    """Return a human reason when the bot may not act on target, else None."""
    me = guild.me
    if me is None:
        return "I couldn't check my own role — try again in a bit."
    if target.id == guild.owner_id:
        return "I can't touch the server owner."
    if target.id == me.id:
        return "I can't moderate myself."
    if target.top_role >= me.top_role:
        return (
            f"I can't touch {target.mention}: their top role is at or "
            f"above mine ({me.top_role})."
        )
    return None


async def _mod_reply(
    interaction: discord.Interaction, text: str, ephemeral: bool = False
) -> None:
    """Answer a (deferred) mod command, tolerating a stale interaction."""
    try:
        await interaction.followup.send(text, ephemeral=ephemeral)
    except (discord.NotFound, discord.HTTPException):
        pass  # interaction expired; nothing left to answer


async def _mod_action(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str,
    action: str,
    minutes: int = 10,
) -> None:
    """Shared kick/ban/timeout flow: defer, re-resolve target, check
    hierarchy, act, and report Discord failures in plain language instead
    of dying on a 403/404."""
    guild = interaction.guild
    if guild is None:
        return
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.HTTPException):
        return  # interaction already stale; nothing to answer
    target, err = await _mod_target(guild, member)
    if err is not None:
        await _mod_reply(interaction, err, ephemeral=True)
        return
    blocked = _mod_hierarchy_blocked(guild, target)
    if blocked is not None:
        await _mod_reply(interaction, blocked, ephemeral=True)
        return
    past = {"kick": "Kicked", "ban": "Banned", "timeout": f"Timed out for {minutes}m"}
    try:
        if action == "kick":
            await target.kick(reason=reason)
        elif action == "ban":
            await target.ban(reason=reason)
        else:
            await target.timeout(
                discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason
            )
    except discord.Forbidden:
        await _mod_reply(
            interaction,
            f"I don't have permission to {action} {target.mention}.",
            ephemeral=True,
        )
        return
    except discord.NotFound:
        await _mod_reply(
            interaction,
            f"{target.mention} is already gone — nothing to {action}.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await _mod_reply(
            interaction,
            f"The {action} failed (Discord error {e.status}). Try again in a bit.",
            ephemeral=True,
        )
        return
    await _mod_reply(interaction, f"{past[action]} {target.mention}: {reason}")


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


def _owner_only():
    """Gate for the owner panel: only the bot owner may use these commands."""

    async def predicate(interaction: discord.Interaction) -> bool:
        return await bot.is_owner(interaction.user)

    return app_commands.check(predicate)


def _role_members_all_bots(role: discord.Role) -> bool:
    members = role.members
    return bool(members) and all(m.bot for m in members)


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
    # Never page bot roles as staff: an offline bot with admin perms can't
    # answer tickets, and pinging it just spams a dead bot.
    return [
        r
        for r in named + extra
        if not r.is_bot_managed() and not _role_members_all_bots(r)
    ]


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
# /announce setup (owner only) creates an #announcements channel plus an
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
    name="setup", description="Create the announcements channel (owner only)."
)
@_owner_only()
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


# No-swearing rule: repeated profanity escalates from a warning to a ban.
# Strike 1: "Please do not swear." Strike 2: final warning that another
# offense means a ban. Strike 3: the user is banned. Warning texts are
# sent at most once per user per minute so they can't be used to spam
# the channel.
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
# (guild_id, user_id) -> profanity strike count for the current bot run.
_swear_strikes: dict[tuple[int, int], int] = {}
_SWEAR_BAN_REASON = "Repeated swearing after warnings from the Nova Nexus 3 bot."


async def _handle_swear(message: discord.Message) -> None:
    """Delete a profane message and escalate the author's strike count.

    Strike 1 warns, strike 2 warns that the next offense means a ban,
    and strike 3 bans the user from the server.
    The server owner is exempt: the bot never polices the owner.
    """
    if message.guild is not None and message.author.id == message.guild.owner_id:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    key = (message.guild.id, message.author.id)
    strikes = _swear_strikes.get(key, 0) + 1
    _swear_strikes[key] = strikes
    try:
        if strikes >= 3:
            try:
                await message.guild.ban(message.author, reason=_SWEAR_BAN_REASON)
            except (discord.Forbidden, discord.HTTPException):
                await message.channel.send(
                    f"{message.author.mention} would be banned for repeated "
                    "swearing, but I don't have permission to ban them."
                )
                return
            _swear_strikes[key] = 0
            await message.channel.send(
                f"{message.author.mention} has been banned for repeated swearing."
            )
            return
        now = time.monotonic()
        last = _swear_warned_at.get(message.author.id, 0.0)
        if now - last < _SWEAR_COOLDOWN_S:
            return
        _swear_warned_at[message.author.id] = now
        if strikes == 1:
            await message.channel.send("Please do not swear.")
        else:
            await message.channel.send(
                f"{message.author.mention}, if you swear again, "
                "you will be banned."
            )
    except discord.HTTPException:
        pass

_DM_GREETINGS = {"hi", "hello", "hey", "yo", "sup", "hiya", "howdy", "greetings"}
_DM_MORNING = ("good morning", "good evening", "good afternoon")

# Words that signal a DM question is about Nova Nexus 3 or the server.
# The FAQ matcher uses broad substring keywords ("role", "when", "game",
# ...), so it is only consulted when the message shows Nova context —
# otherwise a general question like "what is the role of mitochondria?"
# would get the server-roles answer instead of the Wikipedia fallback.
_NOVA_CONTEXT_WORDS = {
    "nova", "nexus", "engine", "server", "discord",
    "ticket", "moderator", "appeal", "holo", "beyblade",
    "valtriac", "avior", "wicked", "unreal", "ue5",
    "studio", "trailer", "cinematic", "fleet", "overlord",
    "frontier", "github", "repo", "ultron",
}


async def _handle_dm(message: discord.Message):
    """Conversational replies in direct messages.

    Greetings stay friendly; engine/server questions use the built-in
    knowledge base; anything else gets a free Wikipedia answer, so the
    bot can talk about general topics too.
    """
    text = (message.content or "").strip()
    if not text:
        return
    now = time.monotonic()
    last = _chat_answered_at.get(message.author.id, 0.0)
    if now - last < _CHAT_COOLDOWN_S:
        return
    _chat_answered_at[message.author.id] = now
    name = message.author.display_name
    lowered = text.lower()
    words = set(re.findall(r"[a-z']+", lowered))
    try:
        if words & _DM_GREETINGS or lowered.startswith(_DM_MORNING):
            await message.channel.send(f"How can I help you, {name}?")
            return
        if "thank" in lowered:
            await message.channel.send("You're welcome!")
            return
        if words & {"bye", "goodbye", "goodnight"} or "good night" in lowered:
            await message.channel.send(f"Goodbye, {name}! Come back anytime.")
            return
        if "help" in lowered:
            await message.channel.send(
                "Try /help to see what I can do, or just ask me a question — "
                "about the Nova Nexus 3 engine, the server, or anything else."
            )
            return
        answer = await asyncio.to_thread(_ai_answer, text)
        if answer:
            await message.channel.send(answer)
            return
        answer = None
        if words & _NOVA_CONTEXT_WORDS:
            answer = answer_question(text)
        if answer:
            await message.channel.send(answer)
            return
        await message.channel.send(
            "I couldn't find an answer for that. "
            "Try asking about the Nova Nexus 3 engine, or /help in the server."
        )
    except discord.HTTPException:
        pass


_CHAT_COOLDOWN_S = 5.0
_chat_answered_at: dict[int, float] = {}


def _talking_to_bot(message: discord.Message) -> bool:
    """True when the message mentions the bot or replies to it."""
    me = bot.user
    if me is None:
        return False
    # Compare IDs: in guilds, mentions hold Member objects while bot.user is a
    # ClientUser, and Member != ClientUser even for the same user.
    if any(m.id == me.id for m in message.mentions):
        return True
    ref = message.reference
    resolved_author = getattr(getattr(ref, "resolved", None), "author", None)
    return getattr(resolved_author, "id", None) == me.id


_CODE_REQUEST_RE = re.compile(
    r"\b(?:code|script|github|repo|source code|snippet|function)\b"
)


def _looks_like_code_request(text: str) -> bool:
    """Heuristic: is the user asking for code from the repo?"""
    return _CODE_REQUEST_RE.search(text.lower()) is not None


def _answer_has_code(text: str) -> bool:
    """Did the generated answer itself come back with a code block?"""
    return "```" in text


async def _deliver_brain_answer(
    message: discord.Message,
    answer: str,
    used_tools: bool,
    question: str,
) -> None:
    """Send a brain answer, keeping code out of public channels.

    When the question is a code request — or the brain pulled from GitHub to
    answer it — the answer goes by DM and the channel only gets a pointer.
    Code never appears in public. Plain answers reply in the channel.

    The privacy call looks at the generated answer too, not just the
    question: the model can return a code block without touching any tool.
    """
    code_private = (
        used_tools
        or _looks_like_code_request(question)
        or _answer_has_code(answer)
    )
    if not code_private or message.guild is None:
        try:
            await message.reply(answer)
        except discord.HTTPException:
            pass
        return
    try:
        await message.author.send(answer)
    except (discord.Forbidden, discord.HTTPException):
        try:
            await message.reply(
                "I couldn't DM you — open your DMs and ask me again."
            )
        except discord.HTTPException:
            pass
        return
    try:
        await message.reply("Check your DMs — I sent that privately.")
    except discord.HTTPException:
        pass


async def _handle_chat(message: discord.Message) -> None:
    now = time.monotonic()
    last = _chat_answered_at.get(message.author.id, 0.0)
    if now - last < _CHAT_COOLDOWN_S:
        return
    _chat_answered_at[message.author.id] = now
    content = re.sub(r"<@!?\d+>", "", message.content or "").strip()
    if not content:
        await message.reply("Hey — ask me anything.")
        return
    # Premium guilds: their paid, moderator-curated FAQ wins over the generic
    # model answer. Everyone else gets Gemini first.
    kb_answer = None
    if message.guild is not None and await _guild_is_premium(message.guild):
        try:
            kb_answer = await asyncio.to_thread(_kb_answer, message.guild.id, content)
        except Exception:  # noqa: BLE001 -- transient GitHub failure: fall through
            kb_answer = None
    if kb_answer:
        await message.reply(kb_answer)
        return
    ai_answer, used_tools = await asyncio.to_thread(_ai_answer_meta, content)
    if ai_answer:
        await _deliver_brain_answer(message, ai_answer, used_tools, content)
        return
    await message.reply(answer_question(content) or _ASK_FALLBACK)


_AI_CLASSIFY_SYSTEM = (
    "You are a message classifier for a Discord community bot. Decide whether "
    "the user's message is a question or a request for help: troubleshooting "
    "a problem, a factual question, asking how to do something, or asking for "
    "advice. Reply with exactly YES or NO and nothing else. Casual chatter, "
    "reactions like 'lol', greetings, thank-yous, statements, and jokes are NO."
)


def _ai_is_question(text: str) -> bool | None:
    """Ask Gemini whether a message is a question/help request.

    True/False verdict, or None when no key is set or the call fails.
    """
    verdict = _gemini_generate(
        _AI_CLASSIFY_SYSTEM, text, temperature=0.0, max_tokens=10, timeout=15
    )
    if verdict is None:
        return None
    v = verdict.strip().upper()
    if v.startswith("YES"):
        return True
    if v.startswith("NO"):
        return False
    return None


async def _handle_ambient(message: discord.Message) -> None:
    """Answer questions posted in chat even when the bot isn't mentioned.

    Ordinary messages are classified by Gemini: questions and requests for
    help get a conversational answer (Gemini first, then the knowledge
    base); everything else is met with silence. Code answers go by DM, never
    in public. Uses the same 5-second per-user cooldown as direct chat so it
    can't spam.
    """
    content = (message.content or "").strip()
    if not content or content.startswith("!"):
        return  # empty, or a command attempt for process_commands
    now = time.monotonic()
    last = _chat_answered_at.get(message.author.id, 0.0)
    if now - last < _CHAT_COOLDOWN_S:
        return
    _chat_answered_at[message.author.id] = now
    if _looks_like_question(content):
        is_question = True  # obvious question: skip the classifier call
    else:
        is_question = await asyncio.to_thread(_ai_is_question, content)
        if not is_question:
            return  # None (no key / error) or False: stay silent
    kb_answer = None
    if message.guild is not None and await _guild_is_premium(message.guild):
        try:
            kb_answer = await asyncio.to_thread(_kb_answer, message.guild.id, content)
        except Exception:  # noqa: BLE001 -- transient GitHub failure: skip premium KB
            kb_answer = None
    if kb_answer:
        try:
            await message.reply(kb_answer)
        except discord.HTTPException:
            pass
        return
    ai_answer, used_tools = await asyncio.to_thread(_ai_answer_meta, content)
    if ai_answer:
        await _deliver_brain_answer(message, ai_answer, used_tools, content)
        return
    faq_answer = answer_question(content)
    if faq_answer:
        try:
            await message.reply(faq_answer)
        except discord.HTTPException:
            pass
    # Otherwise: stay completely silent.


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    if message.guild is None:
        await _handle_dm(message)
    elif _PROFANITY.search(message.content or ""):
        await _handle_swear(message)
    elif _is_ticket_channel(message.channel):
        await _handle_ticket_message(message)
    elif await _handle_customcmd(message):
        pass  # a premium custom !command fired
    elif _talking_to_bot(message):
        await _handle_chat(message)
    else:
        await _handle_ambient(message)
    await bot.process_commands(message)


# --- Premium pack ------------------------------------------------------------
# Premium NEVER removes free features: every premium command first checks
# the guild's premium status and bows out with an upgrade note when the
# server isn't premium. Premium data (guild list, knowledge bases, custom
# commands, schedules) lives as JSON files in this repo under premium/, so
# it survives the ephemeral Actions runners.

_BOT_REPO_OWNER = "howarthjakob4-prog"
_BOT_REPO = "nova-nexus-3-discord-bot"

_PREMIUM_UPGRADE_MSG = (
    "That's a premium feature — this server isn't premium yet. Premium is "
    "$200 and adds custom AI knowledge, custom commands, and scheduled "
    "announcements. The free commands stay free forever. "
    "Ask the bot owner about upgrading."
)

_premium_cache: dict[str, tuple[float, object]] = {}
_PREMIUM_CACHE_TTL_S = 60.0


def _premium_repo_path() -> str:
    return f"/repos/{_BOT_REPO_OWNER}/{_BOT_REPO}"


def _premium_read(path: str, default):
    """Read a JSON file from the bot repo (cached 60s). Blocking: run in a thread.

    Only a confirmed 404 (file not created yet) yields `default`. Any other
    failure raises RuntimeError so callers abort the mutation instead of
    mistaking a transient GitHub outage for an empty file — the empty
    default is never cached over real data.
    """
    import base64 as _b64

    now = time.monotonic()
    hit = _premium_cache.get(path)
    if hit is not None and now - hit[0] < _PREMIUM_CACHE_TTL_S:
        return hit[1]
    if not _RULES_TOKEN:
        return default
    try:
        file = _github_api(
            "GET", _premium_repo_path() + "/contents/" + path, _RULES_TOKEN
        )
        data = json.loads(_b64.b64decode(file["content"]).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = default
        else:
            raise RuntimeError(f"GitHub read failed for {path}: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 -- network, JSON, or shape errors
        raise RuntimeError(f"GitHub read failed for {path}: {e}")
    _premium_cache[path] = (now, data)
    return data


def _premium_write(path: str, obj) -> None:
    """Write a JSON file to the bot repo and refresh the cache. Blocking: run in a thread."""
    import base64 as _b64

    if not _RULES_TOKEN:
        raise RuntimeError("no GitHub token available")
    payload = {
        "message": f"premium data: {path}",
        "content": _b64.b64encode(json.dumps(obj, indent=2).encode()).decode(),
    }
    try:
        existing = _github_api(
            "GET", _premium_repo_path() + "/contents/" + path, _RULES_TOKEN
        )
        if isinstance(existing, dict) and existing.get("sha"):
            payload["sha"] = existing["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404 = first write, nothing to update
            raise
    _github_api("PUT", _premium_repo_path() + "/contents/" + path, _RULES_TOKEN, payload)
    _premium_cache[path] = (time.monotonic(), obj)


def _premium_guild_ids() -> set[int]:
    try:
        data = _premium_read("premium/guilds.json", {"guild_ids": []})
    except Exception:  # noqa: BLE001 -- transient GitHub failure: fail closed
        return set()
    try:
        return {int(g) for g in data.get("guild_ids", [])}
    except (TypeError, ValueError, AttributeError):
        return set()


async def _guild_is_premium(guild) -> bool:
    """Async premium check (GitHub read runs in a thread, cached 60s)."""
    if guild is None:
        return False
    return guild.id in await asyncio.to_thread(_premium_guild_ids)


async def _premium_guild_or_note(interaction: discord.Interaction):
    """Return the guild when it's premium; otherwise send the upgrade note."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Run this inside a server.", ephemeral=True
        )
        return None
    if not await _guild_is_premium(guild):
        await interaction.response.send_message(_PREMIUM_UPGRADE_MSG, ephemeral=True)
        return None
    return guild


premium_group = app_commands.Group(
    name="premium", description="Premium status and management."
)


@premium_group.command(name="status", description="Check this server's premium status.")
async def premium_status_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Run this inside a server.", ephemeral=True
        )
        return
    premium = await _guild_is_premium(guild)
    embed = discord.Embed(
        title="Premium",
        description=(
            "Premium is **active** on this server."
            if premium
            else "Premium is **not active** on this server."
        ),
        color=0xF5C518,
    )
    embed.add_field(
        name="Custom AI knowledge",
        value="/knowledge add — teach me your server's own FAQ.",
        inline=False,
    )
    embed.add_field(
        name="Custom commands",
        value="/customcmd add — your own !commands.",
        inline=False,
    )
    embed.add_field(
        name="Scheduled announcements",
        value="/schedule add — announcements on a timer.",
        inline=False,
    )
    if not premium:
        embed.add_field(
            name="How to upgrade",
            value=(
                "Premium is $200 — ask the bot owner to enable it for this "
                "server. Free commands stay free forever."
            ),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@premium_group.command(name="plans", description="See premium pricing and features.")
async def premium_plans_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Nova Nexus 3 \u2014 Premium",
        description=(
            "One upgrade for your whole server: **$200**.\n"
            "Free commands stay free forever."
        ),
        color=0xF5C518,
    )
    embed.add_field(
        name="Available now",
        value=(
            "\u2022 **Custom AI knowledge** \u2014 /knowledge add: "
            "teach the bot your server's own FAQ.\n"
            "\u2022 **Custom commands** \u2014 /customcmd add: "
            "your own !commands.\n"
            "\u2022 **Scheduled announcements** \u2014 /schedule add: "
            "posts on a timer."
        ),
        inline=False,
    )
    embed.add_field(
        name="Ideas in the works",
        value=(
            "Custom welcome messages \u2022 Reaction roles \u2022 "
            "Polls & giveaways \u2022 Leveling & XP \u2022 "
            "Moderation tuning \u2022 Server analytics"
        ),
        inline=False,
    )
    embed.add_field(
        name="How to upgrade",
        value=(
            "Ask the bot owner to enable premium for your server.\n"
            "See everything, plus the latest ideas, on the status page: "
            "https://lovely-licorice-d2d058.netlify.app/#premium"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)




@premium_group.command(
    name="grant", description="Enable premium for a server (bot owner only)."
)
async def premium_grant_cmd(interaction: discord.Interaction, guild_id: str):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can do that.", ephemeral=True
        )
        return
    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message(
            "Give me a numeric server ID.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await asyncio.to_thread(
            _premium_read, "premium/guilds.json", {"guild_ids": []}
        )
        ids = []
        for g in data.get("guild_ids", []):
            try:
                ids.append(int(g))
            except (TypeError, ValueError):
                pass
        if gid not in ids:
            ids.append(gid)
            await asyncio.to_thread(
                _premium_write, "premium/guilds.json", {"guild_ids": ids}
            )
        await interaction.followup.send(
            f"Premium enabled for server `{gid}`.", ephemeral=True
        )
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@premium_group.command(
    name="revoke", description="Disable premium for a server (bot owner only)."
)
async def premium_revoke_cmd(interaction: discord.Interaction, guild_id: str):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can do that.", ephemeral=True
        )
        return
    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message(
            "Give me a numeric server ID.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await asyncio.to_thread(
            _premium_read, "premium/guilds.json", {"guild_ids": []}
        )
        ids = []
        for g in data.get("guild_ids", []):
            try:
                ids.append(int(g))
            except (TypeError, ValueError):
                pass
        if gid in ids:
            ids.remove(gid)
            await asyncio.to_thread(
                _premium_write, "premium/guilds.json", {"guild_ids": ids}
            )
        await interaction.followup.send(
            f"Premium disabled for server `{gid}`.", ephemeral=True
        )
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


bot.tree.add_command(premium_group)


# --- Premium: custom AI knowledge --------------------------------------------
# Premium servers can teach the bot their own FAQ. The chat brain checks the
# guild's knowledge base first and falls back to the engine FAQ.

_KB_MAX_ENTRIES = 50
_KB_MAX_Q = 200
_KB_MAX_A = 1000
_KB_GENERIC_WORDS = frozenset(
    {
        "what", "when", "where", "which", "who", "whom", "whose", "how",
        "is", "are", "was", "were", "the", "a", "an", "of", "to", "for",
        "in", "on", "do", "does", "did", "can", "should",
    }
)


def _kb_path(guild_id: int) -> str:
    return f"premium/knowledge/{guild_id}.json"


def _kb_entries(guild_id: int) -> list:
    """Blocking: run in a thread."""
    data = _premium_read(_kb_path(guild_id), {"entries": []})
    entries = data.get("entries", [])
    return entries if isinstance(entries, list) else []


def _kb_answer(guild_id: int, question: str) -> str | None:
    """Best guild-knowledge answer, or None. Blocking: run in a thread."""
    q = question.lower()
    best, best_hits = None, 0
    for entry in _kb_entries(guild_id):
        if not isinstance(entry, dict):
            continue
        eq, ea = entry.get("q", ""), entry.get("a", "")
        if not eq or not ea:
            continue
        words = {w for w in re.findall(r"[a-z']+", eq.lower()) if len(w) > 3}
        hits = sum(1 for w in words if w in q)
        specific = sum(1 for w in words if w in q and w not in _KB_GENERIC_WORDS)
        if hits > best_hits and specific >= 1:
            best, best_hits = ea, hits
    return best


knowledge_group = app_commands.Group(
    name="knowledge", description="Teach the bot your server's own FAQ (premium)."
)


@knowledge_group.command(
    name="add", description="Teach the bot a question + answer (mods, premium)."
)
@_manage_server()
async def knowledge_add_cmd(
    interaction: discord.Interaction, question: str, answer: str
):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    if len(question) > _KB_MAX_Q or len(answer) > _KB_MAX_A:
        await interaction.response.send_message(
            f"Keep the question under {_KB_MAX_Q} characters and the answer "
            f"under {_KB_MAX_A}.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        entries = await asyncio.to_thread(_kb_entries, guild.id)
        if len(entries) >= _KB_MAX_ENTRIES:
            await interaction.followup.send(
                f"This server's knowledge base is full ({_KB_MAX_ENTRIES} "
                "entries). Remove one first.",
                ephemeral=True,
            )
            return
        new_id = max([e.get("id", 0) for e in entries if isinstance(e, dict)] + [0]) + 1
        entries.append({"id": new_id, "q": question.strip(), "a": answer.strip()})
        await asyncio.to_thread(
            _premium_write, _kb_path(guild.id), {"entries": entries}
        )
        await interaction.followup.send(
            f"Learned! I'll answer that as entry #{new_id}.", ephemeral=True
        )
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@knowledge_group.command(
    name="remove", description="Forget a knowledge entry by id (mods, premium)."
)
@_manage_server()
async def knowledge_remove_cmd(interaction: discord.Interaction, id: int):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    try:
        entries = await asyncio.to_thread(_kb_entries, guild.id)
        kept = [e for e in entries if not (isinstance(e, dict) and e.get("id") == id)]
        if len(kept) == len(entries):
            await interaction.followup.send(
                f"No entry #{id} here.", ephemeral=True
            )
            return
        await asyncio.to_thread(_premium_write, _kb_path(guild.id), {"entries": kept})
        await interaction.followup.send(f"Forgot entry #{id}.", ephemeral=True)
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@knowledge_group.command(
    name="list", description="Show this server's knowledge entries (mods, premium)."
)
@_manage_server()
async def knowledge_list_cmd(interaction: discord.Interaction):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    entries = await asyncio.to_thread(_kb_entries, guild.id)
    if not entries:
        await interaction.followup.send(
            "Nothing taught yet. Use /knowledge add to teach me.", ephemeral=True
        )
        return
    lines = []
    used = 0
    for e in entries[:25]:
        if not isinstance(e, dict):
            continue
        line = f"#{e.get('id')}: {str(e.get('q', ''))[:80]}"
        if used + len(line) + 1 > 1900:
            lines.append(f"(+{len(entries) - len(lines)} more not shown)")
            break
        lines.append(line)
        used += len(line) + 1
    await interaction.followup.send("\n".join(lines), ephemeral=True)


bot.tree.add_command(knowledge_group)


# --- Premium: custom commands --------------------------------------------------
# Premium servers get their own !commands with custom replies.

_CCMD_MAX = 25
_CCMD_MAX_RESPONSE = 1000
_CCMD_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_CCMD_RESERVED = frozenset({"help"})


def _ccmd_path(guild_id: int) -> str:
    return f"premium/customcmds/{guild_id}.json"


def _ccmds(guild_id: int) -> dict:
    """Blocking: run in a thread."""
    data = _premium_read(_ccmd_path(guild_id), {"cmds": {}})
    cmds = data.get("cmds", {})
    return cmds if isinstance(cmds, dict) else {}


customcmd_group = app_commands.Group(
    name="customcmd", description="Your server's own !commands (premium)."
)


@customcmd_group.command(
    name="add", description="Create a custom !command (mods, premium)."
)
@_manage_server()
async def customcmd_add_cmd(
    interaction: discord.Interaction, name: str, response: str
):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    clean = name.strip().lower().lstrip("!")
    if not _CCMD_NAME_RE.match(clean) or clean in _CCMD_RESERVED:
        await interaction.response.send_message(
            "Names must be 1–32 characters: letters, numbers, dashes.",
            ephemeral=True,
        )
        return
    if len(response) > _CCMD_MAX_RESPONSE:
        await interaction.response.send_message(
            f"Keep the response under {_CCMD_MAX_RESPONSE} characters.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        cmds = await asyncio.to_thread(_ccmds, guild.id)
        if clean not in cmds and len(cmds) >= _CCMD_MAX:
            await interaction.followup.send(
                f"This server already has {_CCMD_MAX} custom commands. "
                "Remove one first.",
                ephemeral=True,
            )
            return
        cmds[clean] = response.strip()
        await asyncio.to_thread(_premium_write, _ccmd_path(guild.id), {"cmds": cmds})
        await interaction.followup.send(
            f"Created `!{clean}`.", ephemeral=True
        )
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@customcmd_group.command(
    name="remove", description="Delete a custom !command (mods, premium)."
)
@_manage_server()
async def customcmd_remove_cmd(interaction: discord.Interaction, name: str):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    clean = name.strip().lower().lstrip("!")
    await interaction.response.defer(ephemeral=True)
    try:
        cmds = await asyncio.to_thread(_ccmds, guild.id)
        if clean not in cmds:
            await interaction.followup.send(
                f"No custom command `!{clean}` here.", ephemeral=True
            )
            return
        del cmds[clean]
        await asyncio.to_thread(_premium_write, _ccmd_path(guild.id), {"cmds": cmds})
        await interaction.followup.send(f"Deleted `!{clean}`.", ephemeral=True)
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@customcmd_group.command(
    name="list", description="Show this server's custom !commands (mods, premium)."
)
@_manage_server()
async def customcmd_list_cmd(interaction: discord.Interaction):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    cmds = await asyncio.to_thread(_ccmds, guild.id)
    if not cmds:
        await interaction.followup.send(
            "No custom commands yet. Use /customcmd add to make one.",
            ephemeral=True,
        )
        return
    lines = [f"!{n}" for n in sorted(cmds)[:25]]
    await interaction.followup.send(" ".join(lines), ephemeral=True)


bot.tree.add_command(customcmd_group)


async def _handle_customcmd(message: discord.Message) -> bool:
    """Fire a premium custom !command. Returns True when one fired."""
    # Parse the !prefix first: this runs on every guild message, and the
    # premium lookup below hits the network (or at least a thread hop), so
    # bail out before it when no command was even attempted.
    text = (message.content or "").strip()
    if not text.startswith("!"):
        return False
    rest = text[1:].split(None, 1)
    if not rest:
        return False
    name = rest[0].lower().rstrip("!?,.")
    if not _CCMD_NAME_RE.match(name):
        return False
    guild = message.guild
    if guild is None or not await _guild_is_premium(guild):
        return False
    cmds = await asyncio.to_thread(_ccmds, guild.id)
    response = cmds.get(name)
    if not response:
        return False
    try:
        await message.reply(str(response)[:2000])
    except discord.HTTPException:
        pass
    return True


# --- Premium: scheduled announcements ------------------------------------------
# Premium servers can schedule announcements. A 60s loop posts due ones;
# overdue one-timers (< 6h late) still post, repeats advance.

_SCHED_MAX = 20
_SCHED_MAX_MSG = 1000
_SCHED_OVERDUE_GRACE_S = 6 * 3600
# In-run idempotency for scheduled announcements: (guild_id, schedule id,
# original fire time) triples already delivered. Stops a schedule being
# re-sent every 60s when the GitHub write that would clear it keeps failing.
_sched_delivered: set[tuple[int, int, object]] = set()
_WHEN_REL_RE = re.compile(r"^in\s+(\d+)\s*([mhdw])$", re.IGNORECASE)
_WHEN_AT_RE = re.compile(r"^at\s+(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$")


def _parse_when(text: str, now: datetime) -> datetime | None:
    """Parse 'in 30m/2h/3d/1w' or 'at YYYY-MM-DD HH:MM' (UTC)."""
    s = text.strip()
    m = _WHEN_REL_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return now + delta
    m = _WHEN_AT_RE.match(s)
    if m:
        try:
            return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _sched_path(guild_id: int) -> str:
    return f"premium/schedules/{guild_id}.json"


def _schedules(guild_id: int) -> list:
    """Blocking: run in a thread."""
    data = _premium_read(_sched_path(guild_id), {"schedules": []})
    scheds = data.get("schedules", [])
    return scheds if isinstance(scheds, list) else []


def _due_schedules(
    scheds: list, now: datetime
) -> tuple[list, list]:
    """Split into (due_to_post, keep). Repeats advance in place."""
    due, keep = [], []
    for s in scheds:
        if not isinstance(s, dict):
            continue
        try:
            at = datetime.fromisoformat(s["at"])
        except (ValueError, KeyError, TypeError):
            continue  # malformed: drop
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at > now:
            keep.append(s)
            continue
        due.append(s)
        repeat = s.get("repeat", "none")
        if repeat == "daily":
            # Advance past now so missed occurrences are skipped instead of
            # each firing a minute apart on the following runner passes.
            while at <= now:
                at += timedelta(days=1)
            s["at"] = at.isoformat()
            keep.append(s)
        elif repeat == "weekly":
            while at <= now:
                at += timedelta(weeks=1)
            s["at"] = at.isoformat()
            keep.append(s)
        # one-time: dropped
    return due, keep


schedule_group = app_commands.Group(
    name="schedule", description="Scheduled announcements (premium)."
)


@schedule_group.command(
    name="add", description="Schedule an announcement (mods, premium)."
)
@_manage_server()
@app_commands.describe(
    channel="Where to post it.",
    message="What to say.",
    when="'in 30m', 'in 2h', 'in 3d', 'in 1w', or 'at 2026-09-20 15:00' (UTC).",
    repeat="Just once, or repeat it.",
)
@app_commands.choices(
    repeat=[
        app_commands.Choice(name="Just once", value="none"),
        app_commands.Choice(name="Daily", value="daily"),
        app_commands.Choice(name="Weekly", value="weekly"),
    ]
)
async def schedule_add_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
    when: str,
    repeat: app_commands.Choice[str],
):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    now = datetime.now(timezone.utc)
    at = _parse_when(when, now)
    if at is None:
        await interaction.response.send_message(
            "I didn't understand the time. Try `in 30m`, `in 2h`, `in 3d`, "
            "`in 1w`, or `at 2026-09-20 15:00` (UTC).",
            ephemeral=True,
        )
        return
    if at <= now:
        await interaction.response.send_message(
            "That time is in the past.", ephemeral=True
        )
        return
    if len(message) > _SCHED_MAX_MSG:
        await interaction.response.send_message(
            f"Keep the message under {_SCHED_MAX_MSG} characters.", ephemeral=True
        )
        return
    me = guild.me
    if me is None or not channel.permissions_for(me).send_messages:
        await interaction.response.send_message(
            f"I can't send messages in {channel.mention}.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        scheds = await asyncio.to_thread(_schedules, guild.id)
        if len(scheds) >= _SCHED_MAX:
            await interaction.followup.send(
                f"This server already has {_SCHED_MAX} scheduled announcements. "
                "Remove one first.",
                ephemeral=True,
            )
            return
        new_id = max([s.get("id", 0) for s in scheds if isinstance(s, dict)] + [0]) + 1
        scheds.append(
            {
                "id": new_id,
                "channel_id": channel.id,
                "message": message.strip(),
                "at": at.isoformat(),
                "repeat": repeat.value,
            }
        )
        await asyncio.to_thread(
            _premium_write, _sched_path(guild.id), {"schedules": scheds}
        )
        stamp = int(at.timestamp())
        await interaction.followup.send(
            f"Scheduled for <t:{stamp}:F> in {channel.mention} "
            f"({repeat.name.lower()}).",
            ephemeral=True,
        )
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@schedule_group.command(
    name="remove", description="Delete a scheduled announcement (mods, premium)."
)
@_manage_server()
async def schedule_remove_cmd(interaction: discord.Interaction, id: int):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    try:
        scheds = await asyncio.to_thread(_schedules, guild.id)
        kept = [s for s in scheds if not (isinstance(s, dict) and s.get("id") == id)]
        if len(kept) == len(scheds):
            await interaction.followup.send(f"No scheduled post #{id} here.", ephemeral=True)
            return
        await asyncio.to_thread(
            _premium_write, _sched_path(guild.id), {"schedules": kept}
        )
        await interaction.followup.send(f"Deleted scheduled post #{id}.", ephemeral=True)
    except (RuntimeError, urllib.error.HTTPError) as e:
        await interaction.followup.send(f"Couldn't save that: {e}", ephemeral=True)


@schedule_group.command(
    name="list", description="Show scheduled announcements (mods, premium)."
)
@_manage_server()
async def schedule_list_cmd(interaction: discord.Interaction):
    guild = await _premium_guild_or_note(interaction)
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    scheds = await asyncio.to_thread(_schedules, guild.id)
    if not scheds:
        await interaction.followup.send(
            "Nothing scheduled. Use /schedule add to plan one.", ephemeral=True
        )
        return
    lines = []
    for s in sorted(
        [x for x in scheds if isinstance(x, dict)], key=lambda x: str(x.get("at", ""))
    )[:15]:
        try:
            stamp = int(datetime.fromisoformat(s["at"]).timestamp())
            when = f"<t:{stamp}:R>"
        except (ValueError, KeyError, TypeError):
            when = "?"
        msg = str(s.get("message", ""))[:60]
        lines.append(f"#{s.get('id')} {when} ({s.get('repeat', 'none')}) <#{s.get('channel_id')}>: {msg}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


bot.tree.add_command(schedule_group)


@tasks.loop(seconds=60)
async def _schedule_runner():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        try:
            if not await _guild_is_premium(guild):
                continue
            scheds = await asyncio.to_thread(_schedules, guild.id)
        except Exception:  # noqa: BLE001
            continue
        # Snapshot each schedule's original fire time: _due_schedules advances
        # repeats in place, and the (guild, id, original at) triple identifies
        # one delivery occurrence for the idempotency set below.
        orig_at = {id(s): s.get("at") for s in scheds if isinstance(s, dict)}
        due, keep = _due_schedules(scheds, now)
        for s in due:
            key = (guild.id, s.get("id"), orig_at.get(id(s)))
            if key in _sched_delivered:
                continue  # sent already; the GitHub write failed last pass
            try:
                at = datetime.fromisoformat(s["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            overdue = (now - at).total_seconds()
            channel = guild.get_channel(s.get("channel_id"))
            if channel is not None and overdue < _SCHED_OVERDUE_GRACE_S:
                try:
                    await channel.send(str(s.get("message", ""))[:2000])
                except (discord.HTTPException, discord.Forbidden):
                    continue
                _sched_delivered.add(key)
        if due:
            try:
                await asyncio.to_thread(
                    _premium_write, _sched_path(guild.id), {"schedules": keep}
                )
            except Exception:  # noqa: BLE001
                # Write failed: the remote file still shows these as due, but
                # _sched_delivered stops them being re-sent every 60 seconds.
                # The write is retried on the next pass.
                continue
            for s in due:
                _sched_delivered.discard(
                    (guild.id, s.get("id"), orig_at.get(id(s)))
                )


@_schedule_runner.before_loop
async def _schedule_runner_before():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    # NOVA_RECONNECT=0 disables auto-reconnect: used by the scheduled
    # GitHub Actions runs so a fresh run cleanly takes over from the
    # previous one instead of the two fighting over the token.
    reconnect = os.environ.get("NOVA_RECONNECT", "1") == "1"
    bot.run(TOKEN, reconnect=reconnect)
