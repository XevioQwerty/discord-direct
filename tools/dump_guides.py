"""One-off dumper: export the #guides channel + all its threads to disk.

Reuses the bot token from .env. Requires the **Message Content Intent** to be
enabled for the application (Discord Dev Portal → Bot → Privileged Gateway
Intents) so historical message text/embeds can be read.

Usage:
    python tools/dump_guides.py                # find a channel named "guides"
    python tools/dump_guides.py 123456789      # use an explicit channel ID

Outputs (repo root):
    guides_dump.json   full structured data (for tooling/Claude)
    guides_dump.md     human-readable rendering
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = 976680631012589610  # discogoon's server
CHANNEL_NAME = "guides"

OUT_JSON = Path("guides_dump.json")
OUT_MD = Path("guides_dump.md")

intents = discord.Intents.default()
intents.message_content = True  # privileged — must be enabled in the dev portal


def _ser_message(m: discord.Message) -> dict:
    return {
        "id": str(m.id),
        "author": {"id": str(m.author.id), "name": str(m.author), "bot": m.author.bot},
        "created_at": m.created_at.isoformat(),
        "edited_at": m.edited_at.isoformat() if m.edited_at else None,
        "pinned": m.pinned,
        "content": m.content,
        "embeds": [e.to_dict() for e in m.embeds],
        "attachments": [
            {"filename": a.filename, "url": a.url, "content_type": a.content_type}
            for a in m.attachments
        ],
        "reactions": [
            {"emoji": str(r.emoji), "count": r.count} for r in m.reactions
        ],
        "jump_url": m.jump_url,
    }


async def _dump_thread(thread: discord.Thread) -> dict:
    messages: list[dict] = []
    async for m in thread.history(limit=None, oldest_first=True):
        messages.append(_ser_message(m))
    return {
        "id": str(thread.id),
        "name": thread.name,
        "archived": thread.archived,
        "locked": thread.locked,
        "created_at": thread.created_at.isoformat() if thread.created_at else None,
        "message_count": len(messages),
        "messages": messages,
    }


async def _collect_threads(channel) -> list[discord.Thread]:
    seen: dict[int, discord.Thread] = {}
    for t in getattr(channel, "threads", []):
        seen[t.id] = t
    # archived public + private threads
    for kwargs in ({"limit": None}, {"private": True, "limit": None}):
        try:
            async for t in channel.archived_threads(**kwargs):
                seen.setdefault(t.id, t)
        except (discord.Forbidden, AttributeError, TypeError):
            pass
    return list(seen.values())


class Dumper(discord.Client):
    def __init__(self, target_id: int | None) -> None:
        super().__init__(intents=intents)
        self.target_id = target_id

    async def on_ready(self) -> None:
        try:
            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)

            channel = None
            if self.target_id:
                channel = self.get_channel(self.target_id) or await self.fetch_channel(self.target_id)
            else:
                for ch in await guild.fetch_channels():
                    if ch.name == CHANNEL_NAME and isinstance(
                        ch, (discord.TextChannel, discord.ForumChannel)
                    ):
                        channel = ch
                        break

            if channel is None:
                print(f"Could not locate channel (#{CHANNEL_NAME} / id={self.target_id}).")
                return

            print(f"Dumping #{channel.name} ({channel.id}) — type {type(channel).__name__}")

            # Parent-channel messages (text channels only; forums hold no direct messages)
            parent_messages: list[dict] = []
            if isinstance(channel, discord.TextChannel):
                async for m in channel.history(limit=None, oldest_first=True):
                    parent_messages.append(_ser_message(m))

            threads = await _collect_threads(channel)
            print(f"Found {len(threads)} thread(s). Reading history…")
            thread_dumps = []
            for t in threads:
                td = await _dump_thread(t)
                print(f"  • {td['name']} — {td['message_count']} msg")
                thread_dumps.append(td)

            data = {
                "guild_id": str(GUILD_ID),
                "channel": {"id": str(channel.id), "name": channel.name, "type": type(channel).__name__},
                "parent_messages": parent_messages,
                "threads": thread_dumps,
            }
            OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            OUT_MD.write_text(_render_md(data), encoding="utf-8")
            print(f"\nWrote {OUT_JSON} and {OUT_MD}")
        finally:
            await self.close()


def _render_md(data: dict) -> str:
    lines = [f"# #{data['channel']['name']} dump", ""]
    if data["parent_messages"]:
        lines.append(f"## Channel messages ({len(data['parent_messages'])})\n")
        for m in data["parent_messages"]:
            lines.append(f"**{m['author']['name']}** · {m['created_at']} · {m['jump_url']}")
            if m["content"]:
                lines.append(m["content"])
            for e in m["embeds"]:
                if e.get("title"):
                    lines.append(f"> _embed:_ **{e['title']}**")
            lines.append("")
    lines.append(f"## Threads ({len(data['threads'])})\n")
    for t in data["threads"]:
        flag = " [archived]" if t["archived"] else ""
        lines.append(f"### {t['name']}{flag}  ·  id `{t['id']}`  ·  {t['message_count']} msg")
        for m in t["messages"]:
            lines.append(f"\n**{m['author']['name']}** · {m['created_at']}")
            if m["content"]:
                lines.append(m["content"])
            for e in m["embeds"]:
                if e.get("title"):
                    lines.append(f"> _embed:_ **{e.get('title','')}**")
                if e.get("description"):
                    lines.append(f"> {e['description']}")
            for a in m["attachments"]:
                lines.append(f"> _attachment:_ [{a['filename']}]({a['url']})")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    if not BOT_TOKEN:
        print("DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)
    target_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    Dumper(target_id).run(BOT_TOKEN)


if __name__ == "__main__":
    main()
