"""Guides Hub — a live, data-driven directory for the guides channel.

Rendered with Discord Components V2 (containers, sections, media galleries),
which look far richer than classic embeds. Curated content lives in
embeds/guides-hub.json on GitHub; the bot layers live data on top of it
(thread activity, auto-discovered threads, Steam cover art, a generated banner)
and edits the published hub in place whenever something changes.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import io
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# ── Config ────────────────────────────────────────────────────────────────────
REGISTRY_FILE = "guides-hub.json"
STATE_PATH    = Path(__file__).parent / "state" / "hub.json"
FONT_DIR      = Path(__file__).parent / "assets" / "fonts"
BANNER_NAME   = "guides-hub-banner.png"
ACTIVITY_TTL  = 30 * 60   # seconds a thread's scanned activity stays fresh
STEAM_TTL     = 7 * 86400 # Steam header URLs carry a hash that changes on art updates
DEBOUNCE      = 15        # seconds to coalesce bursts of edits into a single refresh
TEXT_BUDGET   = 3950      # Discord caps a Components V2 message at 4000 display chars
NEW_CATEGORY  = "_new"
UTC           = datetime.timezone.utc

STATUS_BADGES: dict[str, str] = {
    "working":  "🟢 Working",
    "beta":     "🟡 Beta",
    "outdated": "🟠 Outdated",
    "broken":   "🔴 Broken",
}

FetchFn = Callable[[str], Awaitable[dict]]

# ── Persistent state (published message, Steam art cache) ─────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_state: dict = _load_state()
_state.setdefault("hub", None)
_state.setdefault("steam", {})


def _save_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Model ─────────────────────────────────────────────────────────────────────

def _colour(value, default: int = 0xF1C40F) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip().lstrip("#"), 16)
        except ValueError:
            pass
    return default


def _parse_date(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Guide:
    id: str
    title: str
    short: str
    emoji: str
    category: str
    blurb: str
    url: str
    thread_id: int | None = None
    steam_appid: int | None = None
    image: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str | None = None
    featured: bool = False
    auto: bool = False
    created: datetime.datetime | None = None
    updated: datetime.datetime | None = None

    @property
    def link(self) -> str:
        return self.anchor(bold=True)

    def anchor(self, text: str | None = None, *, bold: bool = False) -> str:
        """Markdown link to the guide. Auto-discovered threads use a native <#id>
        mention — raw thread names (emoji, ?, brackets) can break masked links."""
        if self.auto and self.thread_id:
            return f"<#{self.thread_id}>"
        label = (text or self.title).replace("[", "(").replace("]", ")")
        return f"**[{label}]({self.url})**" if bold else f"[{label}]({self.url})"

    def badges(self, new_days: int) -> list[str]:
        now = datetime.datetime.now(tz=UTC)
        out: list[str] = []
        if self.created and (now - self.created).days < new_days:
            out.append("✨ NEW")
        elif self.updated and (now - self.updated).days < 7:
            out.append("🔄 Updated")
        if self.status in STATUS_BADGES:
            out.append(STATUS_BADGES[self.status])
        return out

    def haystack(self) -> str:
        return " ".join([self.title, self.short, self.blurb, self.category, *self.tags]).lower()


@dataclass
class Snapshot:
    hub: dict
    categories: list[dict]
    guides: list[Guide]
    changelog: list[dict]

    @property
    def accent(self) -> int:
        return _colour(self.hub.get("accent"))

    @property
    def new_days(self) -> int:
        return int(self.hub.get("new_days", 14))

    def category(self, cat_id: str) -> dict | None:
        return next((c for c in self.categories if c["id"] == cat_id), None)

    def in_category(self, cat_id: str) -> list[Guide]:
        return [g for g in self.guides if g.category == cat_id]

    def find(self, guide_id: str) -> Guide | None:
        return next((g for g in self.guides if g.id == guide_id), None)

    def recent(self, n: int) -> list[Guide]:
        dated = [g for g in self.guides if g.updated]
        return sorted(dated, key=lambda g: g.updated, reverse=True)[:n]  # type: ignore[arg-type,return-value]

    def featured(self) -> list[Guide]:
        """Pinned guides first, then topped up with the most recently updated."""
        n = int(self.hub.get("featured_count", 4))
        picks = [g for g in self.guides if g.featured][:n]
        for g in self.recent(len(self.guides)):
            if len(picks) >= n:
                break
            if g not in picks and not g.auto:
                picks.append(g)
        return picks

    def search(self, query: str) -> list[Guide]:
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        if not words:
            return []
        scored: list[tuple[int, Guide]] = []
        for g in self.guides:
            title, hay = g.title.lower(), g.haystack()
            score = sum(3 if w in title else 1 if w in hay else 0 for w in words)
            if score:
                scored.append((score, g))
        scored.sort(key=lambda t: (-t[0], t[1].title))
        return [g for _, g in scored]


def _guide_from_raw(raw: dict, guild_id: str) -> Guide:
    thread_id = int(raw["thread"]) if raw.get("thread") else None
    url = raw.get("url") or (
        f"https://discord.com/channels/{guild_id}/{thread_id}" if thread_id else ""
    )
    title = raw.get("title") or "Untitled guide"
    return Guide(
        id=str(raw.get("id") or thread_id or title),
        title=title,
        short=raw.get("short") or title,
        emoji=raw.get("emoji") or "📄",
        category=raw.get("category") or NEW_CATEGORY,
        blurb=raw.get("blurb") or "",
        url=url,
        thread_id=thread_id,
        steam_appid=int(raw["steam_appid"]) if raw.get("steam_appid") else None,
        image=raw.get("image") or None,
        tags=list(raw.get("tags") or []),
        status=raw.get("status"),
        featured=bool(raw.get("featured")),
        updated=_parse_date(raw.get("updated")),
    )

# ── Live data: threads, activity, Steam art ───────────────────────────────────

_activity: dict[int, tuple[datetime.datetime, datetime.datetime | None, datetime.datetime]] = {}
# thread_id -> (fetched_at, last guide activity, thread created)


def invalidate_thread(thread_id: int) -> None:
    _activity.pop(thread_id, None)


async def _thread_activity(
    bot: discord.Client, thread_id: int
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """(last activity by the bot or thread owner, thread creation time)."""
    now = datetime.datetime.now(tz=UTC)
    if (hit := _activity.get(thread_id)) and (now - hit[0]).total_seconds() < ACTIVITY_TTL:
        return hit[1], hit[2]
    thread = bot.get_channel(thread_id)
    if thread is None:
        try:
            thread = await bot.fetch_channel(thread_id)
        except discord.HTTPException:
            return None, None
    if not isinstance(thread, discord.Thread):
        return None, None
    created = thread.created_at or discord.utils.snowflake_time(thread.id)
    latest: datetime.datetime | None = created
    authors = {thread.owner_id, bot.user.id if bot.user else 0}
    try:
        async for m in thread.history(limit=15):
            if m.author.id in authors:
                ts = m.edited_at or m.created_at
                latest = max(latest, ts) if latest else ts
    except discord.HTTPException:
        pass
    _activity[thread_id] = (now, latest, created)
    return latest, created


async def _list_channel_threads(bot: discord.Client, channel_id: int) -> list[discord.Thread]:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return []
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return []
    threads: dict[int, discord.Thread] = {t.id: t for t in channel.threads}
    try:
        async for t in channel.archived_threads(limit=None):
            threads.setdefault(t.id, t)
    except discord.HTTPException:
        pass
    return sorted(threads.values(), key=lambda t: t.id)


async def _steam_header(session: aiohttp.ClientSession, appid: int) -> str | None:
    now = datetime.datetime.now(tz=UTC)
    cached = _state["steam"].get(str(appid))
    if cached and (now - _parse_date(cached["at"])).total_seconds() < STEAM_TTL:  # type: ignore[operator]
        return cached["url"]
    try:
        async with session.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": str(appid), "filters": "basic"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            payload = await resp.json(content_type=None)
        # Steam sometimes keys the reply by a different id than requested.
        data = payload.get(str(appid)) or next(iter(payload.values()), {})
        url = data["data"].get("header_image") if data.get("success") else None
    except Exception:
        return cached["url"] if cached else None
    if url:
        _state["steam"][str(appid)] = {"url": url, "at": now.isoformat()}
        _save_state()
    return url


async def build_snapshot(bot: discord.Client, reg: dict) -> Snapshot:
    hub = dict(reg.get("hub") or {})
    guild_id = str(hub.get("guild_id") or "")
    categories = [dict(c) for c in reg.get("categories") or []]
    guides = [_guide_from_raw(g, guild_id) for g in reg.get("guides") or []]

    # Threads that exist in the channel but aren't in the registry yet show up
    # automatically, so a new guide is never missing from the hub.
    channel_id = hub.get("channel_id")
    if hub.get("auto_discover", True) and channel_id:
        known = {g.thread_id for g in guides if g.thread_id}
        ignored = {int(x) for x in hub.get("ignore_threads") or []}
        for t in await _list_channel_threads(bot, int(channel_id)):
            if t.id in known or t.id in ignored:
                continue
            guides.append(Guide(
                id=f"thread-{t.id}", title=t.name, short=t.name, emoji="🆕",
                category=NEW_CATEGORY, blurb="Freshly posted — not sorted yet",
                url=t.jump_url, thread_id=t.id, auto=True,
            ))

    known_cats = {c["id"] for c in categories}
    if any(g.category not in known_cats for g in guides):
        categories.append({
            "id": NEW_CATEGORY, "name": "Just Added", "emoji": "🆕",
            "color": hub.get("accent"), "blurb": "Brand-new threads waiting to be sorted",
        })
        for g in guides:
            if g.category not in known_cats:
                g.category = NEW_CATEGORY

    for g in guides:
        if g.thread_id:
            latest, created = await _thread_activity(bot, g.thread_id)
            g.created = created
            if latest and (not g.updated or latest > g.updated):
                g.updated = latest

    async with aiohttp.ClientSession() as session:
        for g in guides:
            if not g.image and g.steam_appid:
                g.image = await _steam_header(session, g.steam_appid)

    return Snapshot(hub=hub, categories=categories, guides=guides, changelog=list(reg.get("changelog") or []))

# ── Banner (generated PNG) ────────────────────────────────────────────────────

def _font(name: str, size: int, weight: str | None = None):
    from PIL import ImageFont
    try:
        font = ImageFont.truetype(str(FONT_DIR / name), size)
        if weight:
            font.set_variation_by_name(weight)
        return font
    except Exception:
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except Exception:
            return ImageFont.load_default(size)


def _rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def render_banner(snap: Snapshot, covers: list[bytes]) -> bytes:
    """A 1200×400 hero image: title, live stats, category chips and cover art."""
    from PIL import Image, ImageDraw, ImageFilter

    W, H = 1200, 400
    accent = _rgb(snap.accent)

    # Dark diagonal gradient base.
    top, bottom = (22, 24, 36), (10, 10, 16)
    small = Image.new("RGB", (60, 20))
    for y in range(20):
        for x in range(60):
            t = min(1.0, (x / 60) * 0.35 + (y / 20) * 0.65)
            small.putpixel((x, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    base = small.resize((W, H), Image.BILINEAR)

    # Soft accent glows.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -320, 460, 300), fill=(*accent, 70))
    cat_colours = [_colour(c.get("color")) for c in snap.categories if c["id"] != NEW_CATEGORY]
    second = _rgb(cat_colours[1]) if len(cat_colours) > 1 else accent
    gd.ellipse((760, 120, 1400, 640), fill=(*second, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(base.convert("RGBA"), glow)

    # Fanned cover art on the right.
    cards: list = []
    for raw in covers[:3]:
        try:
            cards.append(Image.open(io.BytesIO(raw)).convert("RGBA"))
        except Exception:
            continue
    # (x, y, angle) per card; index 0 is the front card.
    slots = [(712, 150, 6), (850, 66, -4), (772, -22, 9)]
    for i, card in reversed(list(enumerate(cards))):
        card = card.resize((352, 165))
        mask = Image.new("L", card.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, *card.size), 18, fill=255)
        card.putalpha(mask)
        framed = Image.new("RGBA", (card.width + 60, card.height + 60), (0, 0, 0, 0))
        shadow = Image.new("RGBA", framed.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((30, 38, 30 + card.width, 38 + card.height), 18, fill=(0, 0, 0, 170))
        framed = Image.alpha_composite(framed, shadow.filter(ImageFilter.GaussianBlur(12)))
        framed.alpha_composite(card, (30, 30))
        x, y, angle = slots[i]
        framed = framed.rotate(angle, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(framed, (x, y))

    d = ImageDraw.Draw(img)
    x0 = 64
    kicker = str(snap.hub.get("kicker") or "").upper()
    if kicker:
        f = _font("Montserrat[wght].ttf", 22, "Bold")
        cx = x0
        for ch in kicker:  # letter-spaced kicker
            d.text((cx, 58), ch, font=f, fill=accent)
            cx += d.textlength(ch, font=f) + 5
    title = str(snap.hub.get("title") or "Guides Hub").upper()
    d.text((x0 - 4, 80), title, font=_font("BebasNeue-Regular.ttf", 132), fill=(255, 255, 255))

    guides = [g for g in snap.guides if not g.auto]
    cats = [c for c in snap.categories if c["id"] != NEW_CATEGORY]
    newest = max((g.updated for g in snap.guides if g.updated), default=datetime.datetime.now(tz=UTC))
    stats = f"{len(guides)} guides  •  {len(cats)} categories  •  updated {newest:%b} {newest.day}, {newest.year}"
    d.text((x0, 214), stats, font=_font("Montserrat[wght].ttf", 24, "SemiBold"), fill=(200, 204, 220))

    # Category chips (wrap to two rows, stop before the cover art).
    chip_font = _font("Montserrat[wght].ttf", 15, "Bold")
    chips = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(chips)
    cx, cy, max_x = x0, 264, 690 if cards else W - 64
    for c in cats:
        label = f"{c['name'].upper()}  {len(snap.in_category(c['id']))}"
        w = int(cd.textlength(label, font=chip_font)) + 28
        if cx + w > max_x:
            cx, cy = x0, cy + 40
            if cy + 30 > H - 10:
                break
        col = _rgb(_colour(c.get("color")))
        cd.rounded_rectangle((cx, cy, cx + w, cy + 30), 15, fill=(*col, 60), outline=(*col, 255), width=2)
        cd.text((cx + 14, cy + 7), label, font=chip_font, fill=(245, 245, 250))
        cx += w + 8
    img = Image.alpha_composite(img, chips)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


async def _make_banner(snap: Snapshot) -> discord.File | None:
    setting = snap.hub.get("banner")
    if setting != "generate":
        return None
    covers: list[bytes] = []
    async with aiohttp.ClientSession() as session:
        for g in snap.featured():
            if not g.image:
                continue
            try:
                async with session.get(g.image, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        covers.append(await resp.read())
            except Exception:
                continue
    try:
        png = await asyncio.to_thread(render_banner, snap, covers)
    except ImportError:
        return None  # Pillow not installed — hub renders without a banner
    return discord.File(io.BytesIO(png), filename=BANNER_NAME)

# ── Views (Components V2) ─────────────────────────────────────────────────────

ui = discord.ui
SMALL, LARGE = discord.SeparatorSpacing.small, discord.SeparatorSpacing.large


def _ts(dt: datetime.datetime | None, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "—"


def _meta_line(g: Guide, snap: Snapshot, *, with_updated: bool = True) -> str:
    parts = g.badges(snap.new_days)
    if with_updated and g.updated:
        parts.append(f"updated {_ts(g.updated)}")
    return "-# " + "  ·  ".join(parts) if parts else ""


def _guide_section(g: Guide, snap: Snapshot, *, heading: str = "###") -> ui.Item:
    lines = [f"{heading} {g.emoji} {g.anchor()}", g.blurb]
    if meta := _meta_line(g, snap):
        lines.append(meta)
    text = "\n".join(l for l in lines if l)
    if g.image:
        return ui.Section(text, accessory=ui.Thumbnail(g.image, description=g.title))
    return ui.Section(text, accessory=ui.Button(label="Open", emoji="↗️", url=g.url))


def _directory_text(snap: Snapshot, mode: str) -> str:
    out = ["## 🗂️ All Guides"]
    for c in snap.categories:
        items = snap.in_category(c["id"])
        if not items:
            continue
        if mode == "full":
            out.append(f"**{c['emoji']} {c['name']}**")
            out += [f"{g.emoji} {g.anchor()} — {g.blurb}" for g in items]
        elif mode == "compact":
            out.append(f"**{c['emoji']} {c['name']}**")
            out.append("  ·  ".join(f"{g.emoji} {g.anchor(g.short)}" for g in items))
        elif mode == "mention":
            refs = [f"<#{g.thread_id}>" if g.thread_id else g.anchor(g.short) for g in items]
            out.append(f"**{c['emoji']} {c['name']}** " + " ".join(refs))
        else:  # counts
            out.append(f"{c['emoji']} **{c['name']}** · {len(items)} guide{'s' * (len(items) != 1)}")
    if mode == "counts":
        out.append("-# Open the menu below to browse everything.")
    return "\n".join(out)


def _browse_select(snap: Snapshot, placeholder: str = "📂  Browse a category…") -> ui.Select:
    options = []
    for c in snap.categories[:25]:
        items = snap.in_category(c["id"])
        if not items:
            continue
        preview = ", ".join(g.short for g in items)
        options.append(discord.SelectOption(
            label=f"{c['name']} ({len(items)})"[:100],
            value=c["id"],
            emoji=c.get("emoji") or None,
            description=(c.get("blurb") or preview)[:100],
        ))
    return ui.Select(custom_id="hub:browse", placeholder=placeholder, options=options, min_values=1, max_values=1)


def build_hub_view(snap: Snapshot, banner: discord.File | None) -> ui.LayoutView:
    featured = snap.featured()
    recent = snap.recent(int(snap.hub.get("recent_count", 3)))
    real = [g for g in snap.guides if not g.auto]

    header = [f"# 📚 {snap.hub.get('title', 'Guides Hub')}"]
    if tagline := snap.hub.get("tagline"):
        header.append(tagline)
    newest = max((g.updated for g in snap.guides if g.updated), default=None)
    header.append(
        f"-# {len(real)} guides  ·  {sum(1 for c in snap.categories if snap.in_category(c['id']))} categories"
        + (f"  ·  last update {_ts(newest)}" if newest else "")
    )

    recent_text = "## 🕒 Recently Updated\n" + "\n".join(
        f"{g.emoji} {g.link}  ·  {_ts(g.updated)}" for g in recent
    )

    buttons = [
        ui.Button(custom_id="hub:search", label="Search", emoji="🔎", style=discord.ButtonStyle.primary),
        ui.Button(custom_id="hub:new", label="What's New", emoji="🆕", style=discord.ButtonStyle.secondary),
        ui.Button(custom_id="hub:random", label="Surprise Me", emoji="🎲", style=discord.ButtonStyle.secondary),
    ]
    if snap.hub.get("reports_channel_id"):
        buttons.append(ui.Button(custom_id="hub:report", label="Request / Report", emoji="📨", style=discord.ButtonStyle.success))

    def assemble(dir_mode: str) -> ui.LayoutView:
        view = ui.LayoutView(timeout=None)
        box = ui.Container(accent_colour=snap.accent)
        if banner is not None:
            box.add_item(ui.MediaGallery(discord.MediaGalleryItem(banner)))
        box.add_item(ui.TextDisplay("\n".join(header)))
        box.add_item(ui.Separator(spacing=LARGE))
        if featured:
            box.add_item(ui.TextDisplay("## ⭐ Featured"))
            for g in featured:
                box.add_item(_guide_section(g, snap))
            box.add_item(ui.Separator(spacing=LARGE))
        if recent:
            box.add_item(ui.TextDisplay(recent_text))
            box.add_item(ui.Separator(spacing=LARGE))
        box.add_item(ui.TextDisplay(_directory_text(snap, dir_mode)))
        box.add_item(ui.Separator(spacing=LARGE))
        box.add_item(ui.ActionRow(_browse_select(snap)))
        box.add_item(ui.ActionRow(*buttons))
        if footer := snap.hub.get("footer"):
            box.add_item(ui.TextDisplay(f"-# {footer}"))
        view.add_item(box)
        return view

    # Degrade the directory gracefully as the guide list grows.
    for mode in ("full", "compact", "mention", "counts"):
        view = assemble(mode)
        if view.content_length() <= TEXT_BUDGET:
            return view
    return view


def build_category_view(snap: Snapshot, cat_id: str) -> ui.LayoutView:
    cat = snap.category(cat_id) or {"id": cat_id, "name": "Unknown", "emoji": "❔"}
    items = snap.in_category(cat_id)
    view = ui.LayoutView(timeout=600)
    box = ui.Container(accent_colour=_colour(cat.get("color"), snap.accent))
    box.add_item(ui.TextDisplay(
        f"# {cat.get('emoji', '')} {cat['name']}\n"
        + (f"{cat['blurb']}\n" if cat.get("blurb") else "")
        + f"-# {len(items)} guide{'s' * (len(items) != 1)}"
    ))
    box.add_item(ui.Separator(spacing=LARGE))
    # Each section costs 3 components; keep well inside the 40-component cap.
    for g in items[:9]:
        box.add_item(_guide_section(g, snap))
    if len(items) > 9:
        box.add_item(ui.TextDisplay("-# …and more: " + "  ·  ".join(g.anchor(g.short) for g in items[9:])))
    box.add_item(ui.Separator())
    box.add_item(ui.ActionRow(_browse_select(snap, "📂  Switch category…")))
    view.add_item(box)
    return view


def build_guide_card(snap: Snapshot, g: Guide) -> ui.LayoutView:
    cat = snap.category(g.category) or {}
    view = ui.LayoutView(timeout=None)
    box = ui.Container(accent_colour=_colour(cat.get("color"), snap.accent))
    if g.image:
        box.add_item(ui.MediaGallery(discord.MediaGalleryItem(g.image, description=g.title)))
    lines = [f"## {g.emoji} {g.title}", g.blurb]
    if g.tags:
        lines.append(" ".join(f"`{t}`" for t in g.tags))
    meta = g.badges(snap.new_days)
    if cat:
        meta.insert(0, f"{cat.get('emoji', '')} {cat.get('name', '')}")
    if g.updated:
        meta.append(f"updated {_ts(g.updated)}")
    if meta:
        lines.append("-# " + "  ·  ".join(meta))
    box.add_item(ui.TextDisplay("\n".join(l for l in lines if l)))
    box.add_item(ui.ActionRow(ui.Button(label="Open guide", emoji="📖", url=g.url)))
    view.add_item(box)
    return view


def build_results_view(snap: Snapshot, query: str, hits: list[Guide]) -> ui.LayoutView:
    view = ui.LayoutView(timeout=600)
    box = ui.Container(accent_colour=snap.accent)
    if not hits:
        box.add_item(ui.TextDisplay(
            f"### 🔎 No guides match “{discord.utils.escape_markdown(query)}”\n"
            "Try a game name, a tool (e.g. `jdownloader`) or a topic (e.g. `online`)."
        ))
    else:
        box.add_item(ui.TextDisplay(
            f"### 🔎 {len(hits)} result{'s' * (len(hits) != 1)} for “{discord.utils.escape_markdown(query)}”"
        ))
        box.add_item(ui.Separator())
        for g in hits[:8]:
            box.add_item(_guide_section(g, snap))
    box.add_item(ui.Separator())
    box.add_item(ui.ActionRow(_browse_select(snap)))
    view.add_item(box)
    return view


def build_whats_new_view(snap: Snapshot) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    box = ui.Container(accent_colour=snap.accent)
    lines = ["## 🆕 What's New", "### Latest guide activity"]
    for g in snap.recent(8):
        badge = " ".join(g.badges(snap.new_days)[:1])
        lines.append(f"{g.emoji} {g.link}  ·  {_ts(g.updated)}" + (f"  ·  {badge}" if badge else ""))
    if snap.changelog:
        lines.append("### 📝 Changelog")
        for entry in snap.changelog[:6]:
            when = _parse_date(entry.get("date"))
            lines.append(f"`{when:%b %d}` {entry.get('text', '')}" if when else f"• {entry.get('text', '')}")
    box.add_item(ui.TextDisplay("\n".join(lines)))
    view.add_item(box)
    return view

# ── Modals ────────────────────────────────────────────────────────────────────

class SearchModal(ui.Modal, title="Search the guides"):
    query = ui.TextInput(label="What are you looking for?", placeholder="e.g. forza, online, jdownloader, ps3", max_length=80)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        snap = await current_snapshot()
        q = str(self.query.value)
        await interaction.response.send_message(view=build_results_view(snap, q, snap.search(q)), ephemeral=True)


class ReportModal(ui.Modal, title="Request a guide / report a problem"):
    subject = ui.TextInput(label="Which guide? (or the new guide you want)", max_length=100)
    details = ui.TextInput(label="Details", style=discord.TextStyle.paragraph, max_length=1000,
                           placeholder="What's broken, or what should the guide cover?")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        snap = await current_snapshot()
        ch_id = snap.hub.get("reports_channel_id")
        channel = _bot.get_channel(int(ch_id)) if (_bot and ch_id) else None
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message("Reports aren't set up right now — ping a mod instead.", ephemeral=True)
            return
        view = ui.LayoutView(timeout=None)
        view.add_item(ui.Container(
            ui.TextDisplay(f"### 📨 {discord.utils.escape_markdown(str(self.subject.value))}\n{self.details.value}\n"
                           f"-# from {interaction.user.mention} · {_ts(datetime.datetime.now(tz=UTC))}"),
            accent_colour=snap.accent,
        ))
        await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message("✅ Sent to the mods — thanks!", ephemeral=True)

# ── Refresh engine ────────────────────────────────────────────────────────────

_bot: discord.Client | None = None
_fetch: FetchFn | None = None
_fetch_fresh: FetchFn | None = None
_snapshot: Snapshot | None = None
_last_render_hash: str | None = None
_lock = asyncio.Lock()
_pending: asyncio.Task | None = None


async def _load_registry(*, fresh: bool = False) -> dict:
    """guides-hub.json from GitHub, falling back to the local checkout (e.g. before it's pushed)."""
    assert _fetch and _fetch_fresh
    try:
        return await (_fetch_fresh if fresh else _fetch)(REGISTRY_FILE)
    except json.JSONDecodeError:
        raise
    except Exception:
        local = Path(__file__).parent / "embeds" / REGISTRY_FILE
        if local.exists():
            return json.loads(local.read_text(encoding="utf-8"))
        raise


async def current_snapshot() -> Snapshot:
    """Latest snapshot, building one on first use."""
    global _snapshot
    if _snapshot is None:
        assert _bot and _fetch
        _snapshot = await build_snapshot(_bot, await _load_registry())
    return _snapshot


def _render_hash(view: ui.LayoutView, banner: discord.File | None) -> str:
    h = hashlib.sha256(json.dumps(view.to_components(), sort_keys=True, default=str).encode())
    if banner is not None:
        h.update(banner.fp.getvalue())  # type: ignore[attr-defined]
        banner.fp.seek(0)
    return h.hexdigest()


async def refresh(*, fresh: bool = False, force: bool = False) -> str:
    """Rebuild the snapshot and edit the published hub if its render changed."""
    global _snapshot, _last_render_hash
    assert _bot and _fetch and _fetch_fresh
    async with _lock:
        reg = await _load_registry(fresh=fresh)
        if fresh:
            _activity.clear()
        _snapshot = snap = await build_snapshot(_bot, reg)
        ref = _state.get("hub")
        if not ref:
            return "Hub isn't published yet — use `/hub publish`."
        banner = await _make_banner(snap)
        view = build_hub_view(snap, banner)
        digest = _render_hash(view, banner)
        if digest == _last_render_hash and not force:
            return "Hub already up to date."
        channel = _bot.get_channel(int(ref["channel_id"])) or await _bot.fetch_channel(int(ref["channel_id"]))
        msg = channel.get_partial_message(int(ref["message_id"]))  # type: ignore[union-attr]
        try:
            await msg.edit(view=view, attachments=[banner] if banner else [])
        except discord.NotFound:
            _state["hub"] = None
            _save_state()
            return "Hub message was deleted — use `/hub publish` to post a new one."
        _last_render_hash = digest
        return f"Hub refreshed — {len(snap.guides)} guides."


def schedule_refresh(delay: float = DEBOUNCE) -> None:
    """Coalesce bursts of activity into one refresh a few seconds later."""
    global _pending
    if _pending and not _pending.done():
        return

    async def run() -> None:
        await asyncio.sleep(delay)
        try:
            await refresh()
        except Exception as exc:
            print(f"[hub] refresh failed: {exc!r}")

    _pending = asyncio.create_task(run())


def _is_hub_thread(channel: object) -> bool:
    ch_id = _snapshot.hub.get("channel_id") if _snapshot else None
    return bool(ch_id) and isinstance(channel, discord.Thread) and str(channel.parent_id) == str(ch_id)


def note_activity(channel: object) -> None:
    """Call after the bot sends/edits a message; refreshes the hub if it was a guide."""
    if _is_hub_thread(channel):
        invalidate_thread(channel.id)  # type: ignore[attr-defined]
        schedule_refresh()


@tasks.loop(minutes=30)
async def _periodic() -> None:
    try:
        print(f"[hub] {await refresh()}")
    except Exception as exc:
        print(f"[hub] periodic refresh failed: {exc!r}")

# ── Interactions ──────────────────────────────────────────────────────────────

async def _on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type is not discord.InteractionType.component:
        return
    data = interaction.data or {}
    cid = str(data.get("custom_id", ""))
    if not cid.startswith("hub:"):
        return
    action = cid[4:]

    if action == "search":
        await interaction.response.send_modal(SearchModal())
        return
    if action == "report":
        await interaction.response.send_modal(ReportModal())
        return

    snap = await current_snapshot()
    if action == "browse":
        values = data.get("values") or []
        view = build_category_view(snap, values[0] if values else "")
    elif action == "new":
        view = build_whats_new_view(snap)
    elif action == "random":
        pool = [g for g in snap.guides if not g.auto] or snap.guides
        view = build_guide_card(snap, random.choice(pool))
    else:
        return

    # Inside an ephemeral panel, swap it in place; from the public hub, open a new one.
    if interaction.message is not None and interaction.message.flags.ephemeral:
        await interaction.response.edit_message(view=view)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def _on_ready() -> None:
    if not _periodic.is_running():
        _periodic.start()


async def _on_thread_create(thread: discord.Thread) -> None:
    if _is_hub_thread(thread):
        schedule_refresh()


async def _on_thread_update(before: discord.Thread, after: discord.Thread) -> None:
    if _is_hub_thread(after) and before.name != after.name:
        schedule_refresh()


async def _on_raw_thread_delete(payload: discord.RawThreadDeleteEvent) -> None:
    ch_id = (_snapshot.hub.get("channel_id") if _snapshot else None)
    if ch_id and str(payload.parent_id) == str(ch_id):
        invalidate_thread(payload.thread_id)
        schedule_refresh()


async def _on_message(message: discord.Message) -> None:
    if not _is_hub_thread(message.channel):
        return
    thread: discord.Thread = message.channel  # type: ignore[assignment]
    if message.author.id in {thread.owner_id, _bot.user.id if _bot and _bot.user else 0}:
        note_activity(thread)


async def _on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
    if _bot is None:
        return
    channel = _bot.get_channel(payload.channel_id)
    if _is_hub_thread(channel):
        note_activity(channel)

# ── Slash commands ────────────────────────────────────────────────────────────

hub_group = app_commands.Group(
    name="hub",
    description="Manage the Guides Hub",
    default_permissions=discord.Permissions(manage_guild=True),
    guild_only=True,
)


@hub_group.command(name="publish", description="Post the Guides Hub (defaults to the guides channel)")
@app_commands.describe(target="Channel ID, #mention or link — defaults to the hub channel in guides-hub.json")
async def hub_publish(interaction: discord.Interaction, target: str | None = None) -> None:
    global _snapshot, _last_render_hash
    await interaction.response.defer(ephemeral=True)
    assert _bot and _fetch_fresh
    try:
        reg = await _load_registry(fresh=True)
        _snapshot = snap = await build_snapshot(_bot, reg)
    except Exception as exc:
        await interaction.followup.send(f"Couldn't load `{REGISTRY_FILE}`: {exc}", ephemeral=True)
        return
    raw_id = re.search(r"(\d+)\D*$", target.strip()) if target else None
    channel_id = int(raw_id.group(1)) if raw_id else int(snap.hub.get("channel_id") or interaction.channel_id or 0)
    try:
        channel = _bot.get_channel(channel_id) or await _bot.fetch_channel(channel_id)
    except discord.HTTPException as exc:
        await interaction.followup.send(f"Can't access channel `{channel_id}`: {exc}", ephemeral=True)
        return
    banner = await _make_banner(snap)
    view = build_hub_view(snap, banner)
    try:
        sent = await channel.send(view=view, **({"file": banner} if banner else {}))  # type: ignore[union-attr]
    except discord.HTTPException as exc:
        await interaction.followup.send(f"Send failed: {exc}", ephemeral=True)
        return
    _state["hub"] = {"channel_id": str(sent.channel.id), "message_id": str(sent.id)}
    _save_state()
    _last_render_hash = None
    await interaction.followup.send(
        f"📚 Hub posted → {sent.jump_url}\nIt now auto-refreshes. Delete the old directory message if you like.",
        ephemeral=True,
    )


@hub_group.command(name="refresh", description="Re-fetch guides-hub.json from GitHub and redraw the hub")
async def hub_refresh(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        result = await refresh(fresh=True, force=True)
    except Exception as exc:
        result = f"Refresh failed: {exc}"
    await interaction.followup.send(result, ephemeral=True)


@hub_group.command(name="status", description="Show hub health + JSON stubs for threads missing from the registry")
async def hub_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    snap = await current_snapshot()
    ref = _state.get("hub")
    where = f"https://discord.com/channels/{snap.hub.get('guild_id')}/{ref['channel_id']}/{ref['message_id']}" if ref else "not published"
    auto = [g for g in snap.guides if g.auto]
    no_art = [g.short for g in snap.guides if not g.image and not g.auto and g.thread_id]
    lines = [
        f"**Hub:** {where}",
        f"**Guides:** {len(snap.guides) - len(auto)} registered · {len(auto)} auto-discovered",
        f"**No thumbnail** (add `steam_appid` or `image`): {', '.join(no_art) or 'none'}",
    ]
    if auto:
        stubs = [
            json.dumps({"id": f"thread-{g.thread_id}", "title": g.title, "emoji": "📄", "category": "games",
                        "thread": str(g.thread_id), "blurb": "", "tags": []}, ensure_ascii=False)
            for g in auto
        ]
        lines.append("**Add these to `guides` in guides-hub.json:**\n```json\n" + ",\n".join(stubs)[:1500] + "\n```")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


async def _guide_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    snap = await current_snapshot()
    hits = snap.search(current) if current.strip() else sorted(snap.guides, key=lambda g: g.title)
    return [app_commands.Choice(name=f"{g.title} — {g.blurb}"[:100], value=g.id) for g in hits[:25]]


@app_commands.command(name="guide", description="Pull up a guide from the Guides Hub")
@app_commands.describe(name="Start typing a game, tool or topic", share="Post it publicly in this channel (default: only you see it)")
@app_commands.autocomplete(name=_guide_autocomplete)
async def guide_cmd(interaction: discord.Interaction, name: str, share: bool = False) -> None:
    snap = await current_snapshot()
    g = snap.find(name) or next(iter(snap.search(name)), None)
    if g is None:
        await interaction.response.send_message(view=build_results_view(snap, name, []), ephemeral=True)
        return
    await interaction.response.send_message(view=build_guide_card(snap, g), ephemeral=not share)

# ── Wiring ────────────────────────────────────────────────────────────────────

def setup(bot: discord.Client, *, fetch: FetchFn, fetch_fresh: FetchFn) -> None:
    """Register hub commands, listeners and the background refresher on *bot*."""
    global _bot, _fetch, _fetch_fresh
    _bot, _fetch, _fetch_fresh = bot, fetch, fetch_fresh
    tree: app_commands.CommandTree = bot.tree  # type: ignore[attr-defined]
    tree.add_command(hub_group)
    tree.add_command(guide_cmd)
    bot.add_listener(_on_interaction, "on_interaction")  # type: ignore[attr-defined]
    bot.add_listener(_on_ready, "on_ready")  # type: ignore[attr-defined]
    bot.add_listener(_on_thread_create, "on_thread_create")  # type: ignore[attr-defined]
    bot.add_listener(_on_thread_update, "on_thread_update")  # type: ignore[attr-defined]
    bot.add_listener(_on_raw_thread_delete, "on_raw_thread_delete")  # type: ignore[attr-defined]
    bot.add_listener(_on_message, "on_message")  # type: ignore[attr-defined]
    bot.add_listener(_on_raw_message_edit, "on_raw_message_edit")  # type: ignore[attr-defined]
