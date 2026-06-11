"""Diagnostic: list all channels/threads in the guild so we can find #guides."""
from __future__ import annotations
import os
import discord
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = 976680631012589610
KNOWN_THREAD_ID = 1102946725431345222  # from guides-directory.json

intents = discord.Intents.default()


class C(discord.Client):
    async def on_ready(self) -> None:
        try:
            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
            print(f"Guild: {guild.name}\n")
            for ch in sorted(await guild.fetch_channels(), key=lambda c: (c.type.value, c.position)):
                print(f"{ch.type.name:12} {ch.id}  {ch.name!r}")
            # Resolve the parent of a known guide thread
            try:
                t = await self.fetch_channel(KNOWN_THREAD_ID)
                print(f"\nKnown thread {KNOWN_THREAD_ID}: {t.name!r}  parent_id={getattr(t,'parent_id',None)}")
            except Exception as e:
                print(f"\nKnown thread fetch failed: {e}")
        finally:
            await self.close()


C(intents=intents).run(BOT_TOKEN)
