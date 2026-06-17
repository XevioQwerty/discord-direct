"""discogoon — Discord bot mirroring Discohook functionality natively via GitHub-hosted JSON."""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN     = os.getenv("DISCORD_BOT_TOKEN")
GITHUB_USER   = "XevioQwerty"
GITHUB_REPO   = "discord-direct"
GITHUB_BRANCH = "main"
CACHE_DIR     = "cache"

# ── Runtime state ─────────────────────────────────────────────────────────────
_cache: dict[str, dict]                    = {}
_cache_times: dict[str, datetime.datetime] = {}
_registered_ephemerals: set[str]           = set()

# ── GitHub helpers ─────────────────────────────────────────────────────────────

def _github_url(filename: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}"
        f"/{GITHUB_BRANCH}/embeds/{filename}"
    )


async def _fetch_github(filename: str) -> dict:
    """Fetch an embed file fresh.

    Uses the GitHub Contents API, which reflects a push within seconds. The raw
    CDN (raw.githubusercontent.com) caches `main` for ~5 min and ignores query-
    string cache-busters, so a freshly-pushed change would otherwise take minutes
    to appear via /update. Falls back to the raw CDN only if the API fails
    (e.g. rate-limited).
    """
    api_url = (
        f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
        f"/contents/embeds/{filename}?ref={GITHUB_BRANCH}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers={"Accept": "application/vnd.github.raw"}) as resp:
            if resp.status == 200:
                return json.loads(await resp.text())  # raises JSONDecodeError on bad JSON
            api_status = resp.status

        # Fallback: raw CDN with a cache-buster (best-effort freshness).
        cb = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
        async with session.get(
            f"{_github_url(filename)}?_cb={cb}",
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"HTTP {resp.status} (API returned {api_status}) fetching `{filename}`"
                )
            return json.loads(await resp.text())


async def _list_github_embeds() -> list[str]:
    """Return every *.json filename in the repo's embeds/ folder via the GitHub contents API."""
    url = (
        f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
        f"/contents/embeds?ref={GITHUB_BRANCH}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} listing embeds/ folder")
            listing = await resp.json()
    return [
        entry["name"]
        for entry in listing
        if entry.get("type") == "file" and entry.get("name", "").endswith(".json")
    ]


# ── Embed-file listing cache (for autocomplete & the right-click editor) ─────────
_embed_list: list[str] = []
_embed_list_time: datetime.datetime | None = None


async def _get_embed_list(max_age: float = 60.0) -> list[str]:
    """Cached, sorted list of *.json files in the repo's embeds/ folder."""
    global _embed_list, _embed_list_time
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    if _embed_list_time and (now - _embed_list_time).total_seconds() < max_age and _embed_list:
        return _embed_list
    try:
        _embed_list = sorted(await _list_github_embeds())
        _embed_list_time = now
    except Exception:
        if not _embed_list:
            _embed_list = sorted(_cache.keys())  # fall back to whatever we have cached
    return _embed_list


def _save_disk(filename: str, data: dict) -> None:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    (Path(CACHE_DIR) / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_disk() -> None:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    for p in sorted(Path(CACHE_DIR).glob("*.json")):
        try:
            _cache[p.name] = json.loads(p.read_text(encoding="utf-8"))
            _cache_times[p.name] = datetime.datetime.fromtimestamp(
                p.stat().st_mtime, tz=datetime.timezone.utc
            )
        except Exception:
            pass  # silently skip malformed cache files


async def get_file(filename: str) -> dict:
    """Return parsed JSON for *filename*, pulling from GitHub when not cached."""
    if filename in _cache:
        return _cache[filename]
    data = await _fetch_github(filename)
    _cache[filename] = data
    _cache_times[filename] = datetime.datetime.now(tz=datetime.timezone.utc)
    _save_disk(filename, data)
    return data


async def get_file_fresh(filename: str) -> dict:
    """Always re-fetch *filename* from GitHub (cache-busted) and refresh caches."""
    data = await _fetch_github(filename)
    _cache[filename] = data
    _cache_times[filename] = datetime.datetime.now(tz=datetime.timezone.utc)
    _save_disk(filename, data)
    return data


async def _file_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Live dropdown of repo embed files for the `file`/`buttons` command options."""
    cur = current.lower()
    files = await _get_embed_list()
    matches = [f for f in files if cur in f.lower()] or files
    return [app_commands.Choice(name=f, value=f) for f in matches[:25]]

# ── Embed builder ──────────────────────────────────────────────────────────────

def build_embeds(msg_block: dict) -> list[discord.Embed]:
    result: list[discord.Embed] = []
    for e in msg_block.get("embeds", []):
        kw: dict = {}
        for key in ("title", "description", "color", "url"):
            if key in e:
                kw[key] = e[key]
        embed = discord.Embed(**kw)
        if ts_str := e.get("timestamp"):
            try:
                embed.timestamp = datetime.datetime.fromisoformat(ts_str)
            except ValueError:
                pass
        if author := e.get("author"):
            embed.set_author(
                name=author.get("name") or "",
                url=author.get("url"),
                icon_url=author.get("icon_url"),
            )
        if footer := e.get("footer"):
            embed.set_footer(
                text=footer.get("text") or "",
                icon_url=footer.get("icon_url"),
            )
        if thumb := e.get("thumbnail"):
            embed.set_thumbnail(url=thumb.get("url"))
        if img := e.get("image"):
            embed.set_image(url=img.get("url"))
        for field in e.get("fields", []):
            embed.add_field(
                name=field.get("name") or "​",
                value=field.get("value") or "​",
                inline=bool(field.get("inline", False)),
            )
        result.append(embed)
    return result

# ── Role picker constants ──────────────────────────────────────────────────────

COLOR_ROLES: dict[str, int] = {
    "Orange": 1100670573207638128,
    "Yellow": 1100670707668631552,
    "Navy":   1100670211448918086,
    "Lime":   1100670815734866002,
    "Green":  1100671139518357585,
    "Red":    1100670392571543624,
    "Blue":   1100671251464343592,
    "Teal":   1100670052719657000,
    "Purple": 1100671367088705567,
}
COLOR_ROLE_IDS = set(COLOR_ROLES.values())

REGION_ROLES: dict[str, int] = {
    "N.America": 1100662863527432253,
    "S.America": 1100663436834263122,
    "Europe":    1100663529385762816,
    "Africa":    1100663648826966056,
    "Asia":      1100663938649182229,
    "Oceania":   1100664003572813925,
}
REGION_ROLE_IDS = set(REGION_ROLES.values())

ANNOUNCEMENT_ROLES: dict[str, int] = {
    "General Announcements": 1299815962362642497,
    "New Stuff":             1336081085817290802,
    "Free Games":            1299817922763554908,
}
ANNOUNCEMENT_ROLE_IDS = set(ANNOUNCEMENT_ROLES.values())

PLATFORM_ROLES: dict[str, int] = {
    "PC":          1515972633173295104,
    "Xbox":        1515975013788811345,
    "PlayStation": 1515975014329876583,
    "Switch":      1515975014820614245,
}
PLATFORM_ROLE_IDS = set(PLATFORM_ROLES.values())

MEMBER_ROLE_ID = 995288435071909919


class ColorSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            custom_id="roles:color",
            placeholder="🎨 Pick a color…",
            min_values=0,
            max_values=1,
            options=[discord.SelectOption(label=name, value=str(rid)) for name, rid in COLOR_ROLES.items()],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Could not resolve your server membership.", ephemeral=True)
            return
        current_colors = [r for r in member.roles if r.id in COLOR_ROLE_IDS]
        if not self.values:
            if current_colors:
                await member.remove_roles(*current_colors, reason="Role picker: removed color")
            await interaction.followup.send("Color role removed.", ephemeral=True)
            return
        selected_id = int(self.values[0])
        new_role = interaction.guild.get_role(selected_id)  # type: ignore[union-attr]
        if selected_id in {r.id for r in member.roles}:
            await member.remove_roles(*current_colors, reason="Role picker: toggled off color")
            await interaction.followup.send("Color role removed.", ephemeral=True)
            return
        await member.remove_roles(*current_colors, reason="Role picker: color swap")
        if new_role:
            await member.add_roles(new_role, reason="Role picker: color assigned")
        name = next(k for k, v in COLOR_ROLES.items() if v == selected_id)
        await interaction.followup.send(f"Color set to **{name}**.", ephemeral=True)


class RegionSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            custom_id="roles:region",
            placeholder="🌍 Pick your region…",
            min_values=0,
            max_values=1,
            options=[discord.SelectOption(label=name, value=str(rid)) for name, rid in REGION_ROLES.items()],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Could not resolve your server membership.", ephemeral=True)
            return
        current_regions = [r for r in member.roles if r.id in REGION_ROLE_IDS]
        if not self.values:
            if current_regions:
                await member.remove_roles(*current_regions, reason="Role picker: removed region")
            await interaction.followup.send("Region role removed.", ephemeral=True)
            return
        selected_id = int(self.values[0])
        new_role = interaction.guild.get_role(selected_id)  # type: ignore[union-attr]
        if selected_id in {r.id for r in member.roles}:
            await member.remove_roles(*current_regions, reason="Role picker: toggled off region")
            await interaction.followup.send("Region role removed.", ephemeral=True)
            return
        await member.remove_roles(*current_regions, reason="Role picker: region swap")
        if new_role:
            await member.add_roles(new_role, reason="Role picker: region assigned")
        name = next(k for k, v in REGION_ROLES.items() if v == selected_id)
        await interaction.followup.send(f"Region set to **{name}**.", ephemeral=True)


class AnnouncementsSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            custom_id="roles:announcements",
            placeholder="📢 Opt into announcements…",
            min_values=0,
            max_values=len(ANNOUNCEMENT_ROLES),
            options=[discord.SelectOption(label=name, value=str(rid)) for name, rid in ANNOUNCEMENT_ROLES.items()],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Could not resolve your server membership.", ephemeral=True)
            return
        guild = interaction.guild  # type: ignore[union-attr]
        selected_ids = {int(v) for v in self.values}
        to_add, to_remove = [], []
        for rid in ANNOUNCEMENT_ROLE_IDS:
            role = guild.get_role(rid)
            if not role:
                continue
            has_it = rid in {r.id for r in member.roles}
            if rid in selected_ids and not has_it:
                to_add.append(role)
            elif rid not in selected_ids and has_it:
                to_remove.append(role)
        if to_add:
            await member.add_roles(*to_add, reason="Role picker: announcement opt-in")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Role picker: announcement opt-out")
        if selected_ids:
            names = [k for k, v in ANNOUNCEMENT_ROLES.items() if v in selected_ids]
            await interaction.followup.send(f"Announcements updated: **{', '.join(names)}**", ephemeral=True)
        else:
            await interaction.followup.send("Opted out of all announcements.", ephemeral=True)


class PlatformSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            custom_id="roles:platform",
            placeholder="🖥️ Pick your platform(s)…",
            min_values=0,
            max_values=len(PLATFORM_ROLES),
            options=[discord.SelectOption(label=name, value=str(rid)) for name, rid in PLATFORM_ROLES.items()],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Could not resolve your server membership.", ephemeral=True)
            return
        guild = interaction.guild  # type: ignore[union-attr]
        selected_ids = {int(v) for v in self.values}
        to_add, to_remove = [], []
        for rid in PLATFORM_ROLE_IDS:
            role = guild.get_role(rid)
            if not role:
                continue
            has_it = rid in {r.id for r in member.roles}
            if rid in selected_ids and not has_it:
                to_add.append(role)
            elif rid not in selected_ids and has_it:
                to_remove.append(role)
        if to_add:
            await member.add_roles(*to_add, reason="Role picker: platform opt-in")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Role picker: platform opt-out")
        if selected_ids:
            names = [k for k, v in PLATFORM_ROLES.items() if v in selected_ids]
            await interaction.followup.send(f"Platforms updated: **{', '.join(names)}**", ephemeral=True)
        else:
            await interaction.followup.send("Platform roles removed.", ephemeral=True)


class VerifyButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            custom_id="verify:member",
            label="✅  Verify",
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Could not resolve your server membership.", ephemeral=True)
            return
        if any(r.id == MEMBER_ROLE_ID for r in member.roles):
            await interaction.response.send_message("You're already verified!", ephemeral=True)
            return
        role = interaction.guild.get_role(MEMBER_ROLE_ID)  # type: ignore[union-attr]
        if not role:
            await interaction.response.send_message("Member role not found — ping an admin.", ephemeral=True)
            return
        await member.add_roles(role, reason="Self-verify")
        await interaction.response.send_message("✅ Verified! Welcome to the server.", ephemeral=True)


class RolePickerView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ColorSelect())
        self.add_item(RegionSelect())
        self.add_item(AnnouncementsSelect())
        self.add_item(PlatformSelect())
        self.add_item(VerifyButton())


# ── Persistent button / view helpers ──────────────────────────────────────────

_STYLES: dict[str, discord.ButtonStyle] = {
    "primary":   discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success":   discord.ButtonStyle.success,
    "danger":    discord.ButtonStyle.danger,
}


class EphemeralButton(discord.ui.Button):
    """Non-link button that sends a file's message as an ephemeral reply."""

    def __init__(self, *, label: str, style: discord.ButtonStyle, ephemeral_file: str) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=f"ephemeral:{ephemeral_file}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        filename = self.custom_id[len("ephemeral:"):]
        try:
            data = await get_file(filename)
        except json.JSONDecodeError as exc:
            await interaction.response.send_message(
                f"JSON error in `{filename}`: {exc}", ephemeral=True
            )
            return
        except Exception as exc:
            await interaction.response.send_message(
                f"Could not load `{filename}`: {exc}", ephemeral=True
            )
            return
        
        msg_block, _ = extract_message_payload(data)
        embeds = build_embeds(msg_block)
        content = msg_block.get("content") or None

        if not content and not embeds:
            await interaction.response.send_message(
                f"The target file `{filename}` resolved to an empty message layout.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            content=content,
            embeds=embeds,
            ephemeral=True,
        )


def _ensure_registered(ephemeral_file: str) -> None:
    """Register a one-button persistent view for *ephemeral_file* if not done yet."""
    if ephemeral_file in _registered_ephemerals:
        return
    v = discord.ui.View(timeout=None)
    v.add_item(
        EphemeralButton(label="btn", style=discord.ButtonStyle.primary, ephemeral_file=ephemeral_file)
    )
    bot.add_view(v)
    _registered_ephemerals.add(ephemeral_file)


def parse_buttons(data: dict) -> list[dict]:
    """Normalize both legacy 'buttons' config and native Discohook 'components' layouts."""
    normalized: list[dict] = []
    
    # Check manual/legacy configuration list first
    if "buttons" in data and isinstance(data["buttons"], list):
        return data["buttons"]

    # Read standard Discohook style components (Action Rows)
    if "components" in data and isinstance(data["components"], list):
        for row in data["components"]:
            if not isinstance(row, dict):
                continue
            
            # Action rows contain lists of actual interactive items (type 1 = action row)
            items = row.get("components", []) if row.get("type") == 1 else [row]
            for item in items:
                if not isinstance(item, dict) or item.get("type") != 2: # 2 = Button
                    continue
                
                style_val = item.get("style")
                style_map = {1: "primary", 2: "secondary", 3: "success", 4: "danger", 5: "link"}
                
                if isinstance(style_val, int):
                    style_str = style_map.get(style_val, "primary")
                else:
                    style_str = str(style_val or "primary").lower()

                btn_data = {
                    "label": item.get("label") or "Button",
                    "style": style_str,
                    "url": item.get("url"),
                    "custom_id": item.get("custom_id"),
                    "ephemeral": item.get("ephemeral")
                }

                # Extract trigger targets mapped directly inside Discohook custom ID labels
                cid = item.get("custom_id") or ""
                if cid.startswith("ephemeral:"):
                    btn_data["ephemeral"] = cid[len("ephemeral:"):]

                normalized.append(btn_data)
                
    return normalized


def build_view(buttons: list[dict]) -> discord.ui.View | None:
    """Build a persistent View from the normalized buttons list."""
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for btn in buttons:
        style_str = (btn.get("style") or "primary").lower()
        label     = btn.get("label") or "Button"
        if style_str == "link" or btn.get("url"):
            url = btn.get("url") or ""
            if url:
                view.add_item(discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.link,
                    url=url,
                ))
        else:
            eph_file = btn.get("ephemeral") or ""
            if not eph_file:
                continue
            style = _STYLES.get(style_str, discord.ButtonStyle.primary)
            view.add_item(EphemeralButton(label=label, style=style, ephemeral_file=eph_file))
            _ensure_registered(eph_file)
    return view if view.children else None


def get_special_view(data: dict) -> discord.ui.View | None:
    """Return a special persistent view if `view_type` is set in the data."""
    vt = data.get("view_type") or (data.get("message") or {}).get("view_type")
    if vt == "role_picker":
        return RolePickerView()
    return None


def extract_message_payload(data: dict) -> tuple[dict, list[dict]]:
    """
    Extracts the displayable message block and button references.
    Handles legacy wrapper structure and flat, raw Discohook structures.
    """
    if "message" in data and isinstance(data["message"], dict):
        msg_block = data["message"]
    else:
        msg_block = data
        
    buttons = parse_buttons(data)
    return msg_block, buttons

# ── Message link parser ────────────────────────────────────────────────────────

_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def parse_message_link(link: str) -> tuple[int, int, int] | None:
    m = _LINK_RE.search(link.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

# ── Bot ────────────────────────────────────────────────────────────────────────

class DiscoGoon(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )

    async def setup_hook(self) -> None:
        _load_disk()
        # Re-register persistent views for every ephemeral button target in cache
        for data in _cache.values():
            _, buttons = extract_message_payload(data)
            for btn in buttons:
                if eph := btn.get("ephemeral"):
                    _ensure_registered(eph)
        # Role picker is always registered — its custom_ids are stable
        self.add_view(RolePickerView())
        # Sync global slash commands
        await self.tree.sync()

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"discogoon online — {self.user} ({self.user.id})")


bot = DiscoGoon()

# ── /send ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="send", description="Send a message defined by a JSON file to a channel/thread ID or link")
@app_commands.describe(
    file="Filename in the GitHub embeds/ folder, e.g. welcome.json",
    target="Channel/Thread ID, Channel Mention (#channel), or full Discord URL Link",
)
@app_commands.autocomplete(file=_file_autocomplete)
async def cmd_send(
    interaction: discord.Interaction,
    file: str,
    target: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    
    # Parse out ID
    match = re.search(r"(\d+)\s*$", target.strip())
    if not match:
        await interaction.followup.send(
            f"Could not parse a valid ID from target: `{target}`. Ensure it is an ID, mention, or link.", 
            ephemeral=True
        )
        return
        
    target_id = int(match.group(1))

    # Resolve target destination
    destination = bot.get_channel(target_id)
    if destination is None:
        try:
            destination = await bot.fetch_channel(target_id)
        except discord.NotFound:
            await interaction.followup.send(
                f"Could not find a channel or thread with ID `{target_id}`.", 
                ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                f"Missing permissions to access channel/thread `{target_id}`.", 
                ephemeral=True
            )
            return
        except Exception as exc:
            await interaction.followup.send(f"Error fetching channel: {exc}", ephemeral=True)
            return

    if not hasattr(destination, "send"):
        await interaction.followup.send(
            f"Target {destination.mention} does not support text messages.",
            ephemeral=True,
        )
        return

    # Fetch JSON payload
    try:
        data = await get_file(file)
    except json.JSONDecodeError as exc:
        await interaction.followup.send(f"JSON parse error in `{file}`: {exc}", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"Failed to fetch `{file}`: {exc}", ephemeral=True)
        return

    msg_block, buttons = extract_message_payload(data)
    embeds = build_embeds(msg_block)
    content = msg_block.get("content") or None
    view = get_special_view(data) or build_view(buttons)

    # Prevent sending empty payloads to avoid 400 Bad Request error
    if not content and not embeds:
        await interaction.followup.send(
            f"Error: The JSON payload inside `{file}` contains no message text and no embeds.",
            ephemeral=True
        )
        return

    send_kw: dict = dict(content=content, embeds=embeds)
    if view is not None:
        send_kw["view"] = view

    try:
        sent = await destination.send(**send_kw)  # type: ignore[attr-defined]
    except discord.Forbidden:
        await interaction.followup.send(
            f"Missing permissions to send messages in {destination.mention}.", ephemeral=True
        )
        return
    except Exception as exc:
        await interaction.followup.send(f"Send failed: {exc}", ephemeral=True)
        return

    await interaction.followup.send(
        f"Sent! [Jump to message]({sent.jump_url})", ephemeral=True
    )

# ── /edit ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="edit", description="Edit a message the bot previously sent")
@app_commands.describe(
    message_link="Full Discord message link",
    file="JSON file to replace message content/embeds (optional)",
    buttons="JSON file whose components replace current buttons (optional)",
)
@app_commands.autocomplete(file=_file_autocomplete, buttons=_file_autocomplete)
async def cmd_edit(
    interaction: discord.Interaction,
    message_link: str,
    file: str | None = None,
    buttons: str | None = None,
) -> None:
    if not file and not buttons:
        await interaction.response.send_message(
            "Provide at least one of `file` or `buttons`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    parsed = parse_message_link(message_link)
    if not parsed:
        await interaction.followup.send(
            "Invalid message link. Expected: `https://discord.com/channels/...`",
            ephemeral=True,
        )
        return

    _, channel_id, message_id = parsed
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            await interaction.followup.send("Cannot access that channel.", ephemeral=True)
            return

    try:
        msg = await ch.fetch_message(message_id)  # type: ignore[union-attr]
    except discord.NotFound:
        await interaction.followup.send("Message not found.", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send("No permission to read that channel.", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"Could not fetch message: {exc}", ephemeral=True)
        return

    edit_kw: dict = {}

    if file:
        try:
            fdata = await get_file(file)
        except json.JSONDecodeError as exc:
            await interaction.followup.send(f"JSON parse error in `{file}`: {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"Failed to fetch `{file}`: {exc}", ephemeral=True)
            return
        mb, buttons_list = extract_message_payload(fdata)
        edit_kw["content"] = mb.get("content") or None
        edit_kw["embeds"]  = build_embeds(mb)
        special = get_special_view(fdata)
        if special:
            edit_kw["view"] = special
        elif buttons_list:
            edit_kw["view"] = build_view(buttons_list)

    if buttons:
        try:
            bdata = await get_file(buttons)
        except json.JSONDecodeError as exc:
            await interaction.followup.send(f"JSON parse error in `{buttons}`: {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"Failed to fetch `{buttons}`: {exc}", ephemeral=True)
            return
        _, parsed_btns = extract_message_payload(bdata)
        edit_kw["view"] = build_view(parsed_btns)

    try:
        await msg.edit(**edit_kw)
    except discord.Forbidden:
        await interaction.followup.send("No permission to edit that message.", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"Edit failed: {exc}", ephemeral=True)
        return

    await interaction.followup.send("Message updated.", ephemeral=True)

# ── /update ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="update", description="Re-fetch all files in the GitHub embeds/ folder")
async def cmd_update(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    # Discover every file currently in the repo so new additions are picked up,
    # even if they've never been sent/edited before. Fall back to known cache
    # keys if the listing API is unreachable.
    try:
        discovered = await _list_github_embeds()
    except Exception as exc:
        discovered = []
        if not _cache:
            await interaction.followup.send(
                f"Could not list the embeds/ folder ({exc}) and cache is empty — nothing to update.",
                ephemeral=True,
            )
            return

    filenames = sorted(set(discovered) | set(_cache.keys()))
    if not filenames:
        await interaction.followup.send("No JSON files found in the embeds/ folder.", ephemeral=True)
        return

    _cache.clear()
    _cache_times.clear()
    for p in Path(CACHE_DIR).glob("*.json"):
        try:
            p.unlink()
        except OSError:
            pass

    succeeded: list[str] = []
    failed:    list[str] = []

    for fn in filenames:
        try:
            data = await _fetch_github(fn)
            _cache[fn]       = data
            _cache_times[fn] = datetime.datetime.now(tz=datetime.timezone.utc)
            _save_disk(fn, data)
            
            _, buttons = extract_message_payload(data)
            for btn in buttons:
                if eph := btn.get("ephemeral"):
                    _ensure_registered(eph)
            succeeded.append(fn)
        except Exception as exc:
            failed.append(f"`{fn}` — {exc}")

    parts: list[str] = []
    if succeeded:
        parts.append("**Refreshed:**\n" + "\n".join(f"• `{fn}`" for fn in succeeded))
    if failed:
        parts.append("**Failed:**\n" + "\n".join(f"• {f}" for f in failed))
    await interaction.followup.send("\n\n".join(parts) or "Done.", ephemeral=True)

# ── /list ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="list", description="List all currently cached JSON files")
async def cmd_list(interaction: discord.Interaction) -> None:
    if not _cache:
        await interaction.response.send_message("Cache is empty.", ephemeral=True)
        return
    lines: list[str] = []
    for fn in sorted(_cache.keys()):
        ts     = _cache_times.get(fn)
        ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "unknown"
        lines.append(f"• `{fn}` — last fetched {ts_str}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ── Right-click message editor ("Edit with Helper") ─────────────────────────────

class _FileEditSelect(discord.ui.Select):
    """Dropdown of repo embed files; applies the chosen one to a target message."""

    def __init__(self, target: discord.Message, files: list[str]) -> None:
        self.target = target
        options = [discord.SelectOption(label=f[:100], value=f[:100]) for f in files[:25]]
        super().__init__(
            placeholder="Choose an embed file to apply…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        fn = self.values[0]
        try:
            data = await get_file_fresh(fn)
        except json.JSONDecodeError as exc:
            await interaction.followup.send(f"JSON error in `{fn}`: {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"Failed to load `{fn}`: {exc}", ephemeral=True)
            return

        msg_block, buttons = extract_message_payload(data)
        embeds = build_embeds(msg_block)
        content = msg_block.get("content") or None
        if not content and not embeds:
            await interaction.followup.send(f"`{fn}` has no content or embeds.", ephemeral=True)
            return

        edit_kw: dict = dict(content=content, embeds=embeds)
        view = build_view(buttons)
        if view is not None:
            edit_kw["view"] = view
        try:
            await self.target.edit(**edit_kw)
        except discord.Forbidden:
            await interaction.followup.send("I can't edit that message.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"Edit failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Edited with `{fn}`. [Jump to message]({self.target.jump_url})", ephemeral=True
        )


class _FileEditView(discord.ui.View):
    def __init__(self, target: discord.Message, files: list[str]) -> None:
        super().__init__(timeout=120)
        self.add_item(_FileEditSelect(target, files))


@bot.tree.context_menu(name="Edit with Helper")
async def edit_with_helper(interaction: discord.Interaction, message: discord.Message) -> None:
    if bot.user is None or message.author.id != bot.user.id:
        await interaction.response.send_message(
            "I can only edit messages **I** sent.", ephemeral=True
        )
        return
    files = await _get_embed_list()
    if not files:
        await interaction.response.send_message(
            "No embed files found in the repo.", ephemeral=True
        )
        return
    note = "" if len(files) <= 25 else f"\n-# Showing first 25 of {len(files)} files."
    await interaction.response.send_message(
        f"Pick an embed file to apply to [that message]({message.jump_url}):{note}",
        view=_FileEditView(message, files),
        ephemeral=True,
    )

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("CRITICAL: DISCORD_BOT_TOKEN environment variable not set.")
    else:
        bot.run(BOT_TOKEN)