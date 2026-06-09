"""discogoon — Discord bot mirroring Discohook functionality via GitHub-hosted JSON."""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

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
    async with aiohttp.ClientSession() as session:
        async with session.get(_github_url(filename)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} fetching `{filename}`")
            text = await resp.text()
    return json.loads(text)  # raises JSONDecodeError on bad JSON


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
        msg_block = data.get("message", {})
        await interaction.response.send_message(
            content=msg_block.get("content") or None,
            embeds=build_embeds(msg_block),
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


def build_view(buttons: list[dict]) -> discord.ui.View | None:
    """Build a persistent View from the JSON buttons list. Returns None if no valid buttons."""
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for btn in buttons:
        style_str = (btn.get("style") or "primary").lower()
        label     = btn.get("label") or "Button"
        if style_str == "link":
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
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
        )

    async def setup_hook(self) -> None:
        _load_disk()
        # Re-register persistent views for every ephemeral button target in cache
        for data in _cache.values():
            for btn in data.get("buttons", []):
                if eph := btn.get("ephemeral"):
                    _ensure_registered(eph)
        # Sync global slash commands (propagation can take up to ~1 hour after first deploy)
        await self.tree.sync()

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"discogoon online — {self.user} ({self.user.id})")


bot = DiscoGoon()

# ── /send ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="send", description="Send a message via JSON using a Channel Name, ID, Mention, or Link")
@app_commands.describe(
    file="Filename in the GitHub embeds/ folder, e.g. welcome.json",
    target="Channel/Thread ID, Mention (#channel), or full Discord URL link",
)
async def cmd_send(
    interaction: discord.Interaction,
    file: str,
    target: str,  # Switched to str to accept IDs and Links directly
) -> None:
    await interaction.response.defer(ephemeral=True)
    
    # 1. Parse out raw numbers if the user pasted a link or mention layout
    # This extracts the ID whether it's '12345', '<#12345>', or '.../channels/guild/12345'
    match = re.search(r"(\d+)\s*$", target.strip())
    if not match:
        await interaction.followup.send(
            f"Could not parse a valid ID from your target input: `{target}`. Please provide a raw ID or Link.", 
            ephemeral=True
        )
        return
        
    target_id = int(match.group(1))

    # 2. Try to fetch the channel or thread from cache or API
    destination = bot.get_channel(target_id)
    if destination is None:
        try:
            destination = await bot.fetch_channel(target_id)
        except discord.NotFound:
            await interaction.followup.send(
                f"Could not find a channel or thread with ID `{target_id}`. Make sure the bot is in that server.", 
                ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                f"Bot does not have permission to access channel/thread `{target_id}`.", 
                ephemeral=True
            )
            return
        except Exception as exc:
            await interaction.followup.send(f"Error fetching destination: {exc}", ephemeral=True)
            return

    # 3. Verify the resolved destination supports message sending
    if not hasattr(destination, "send"):
        await interaction.followup.send(
            f"Target {destination.mention} is a category or voice channel that doesn't support text messages.",
            ephemeral=True,
        )
        return

    # 4. Fetch JSON payload and build message
    try:
        data = await get_file(file)
    except json.JSONDecodeError as exc:
        await interaction.followup.send(f"JSON parse error in `{file}`: {exc}", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"Failed to fetch `{file}`: {exc}", ephemeral=True)
        return

    msg_block = data.get("message", {})
    view      = build_view(data.get("buttons", []))
    send_kw: dict = dict(
        content=msg_block.get("content") or None,
        embeds=build_embeds(msg_block),
    )
    if view is not None:
        send_kw["view"] = view

    # 5. Ship it out
    try:
        sent = await destination.send(**send_kw)  # type: ignore[attr-defined]
    except discord.Forbidden:
        await interaction.followup.send(
            f"Missing permission to send messages in {destination.mention}.", ephemeral=True
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
    buttons="JSON file whose buttons array replaces current buttons (optional)",
)
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
        mb = fdata.get("message", {})
        edit_kw["content"] = mb.get("content") or None
        edit_kw["embeds"]  = build_embeds(mb)

    if buttons:
        try:
            bdata = await get_file(buttons)
        except json.JSONDecodeError as exc:
            await interaction.followup.send(f"JSON parse error in `{buttons}`: {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"Failed to fetch `{buttons}`: {exc}", ephemeral=True)
            return
        # None clears components; a View replaces them
        edit_kw["view"] = build_view(bdata.get("buttons", []))

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

@bot.tree.command(name="update", description="Re-fetch all cached files from GitHub")
async def cmd_update(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not _cache:
        await interaction.followup.send("Cache is empty — nothing to update.", ephemeral=True)
        return

    filenames = list(_cache.keys())
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
            for btn in data.get("buttons", []):
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

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
