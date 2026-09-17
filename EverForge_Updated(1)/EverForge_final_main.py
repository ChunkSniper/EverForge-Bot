import os
import sqlite3
import secrets
import asyncio
import random
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("everforge")

# ============================================================
# CONFIG
# ============================================================

DB_FILE = "everforge.sqlite3"
ORDER_CATEGORY = "════ Order ════"
WELCOME_CHANNEL = "🤚・welcome"
BOOST_CHANNEL = "🚀・boosts"
REVIEW_CHANNEL = "⭐・reviews"
MEMBER_ROLE = "・Members"
MEMBER_ROLE_DELAY = timedelta(days=3)

# ============================================================
# DATABASE
# ============================================================

def get_db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")

    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            payment_info TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS products (
            guild_id INTEGER,
            product_id TEXT,
            title TEXT,
            description TEXT,
            price REAL,
            currency TEXT,
            created_by INTEGER,
            created_at TEXT,
            PRIMARY KEY (guild_id, product_id)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            guild_id INTEGER,
            buyer_id INTEGER,
            product_id TEXT,
            product_title TEXT,
            price REAL,
            currency TEXT,
            status TEXT,
            channel_id INTEGER,
            created_at TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            created_at TEXT
        )
    """)

    # Community features added without changing the existing shop/order tables.
    con.execute("""
        CREATE TABLE IF NOT EXISTS member_joins (
            guild_id INTEGER,
            user_id INTEGER,
            joined_at TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS invite_stats (
            guild_id INTEGER,
            user_id INTEGER,
            invites INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS invite_cache (
            guild_id INTEGER,
            code TEXT,
            uses INTEGER DEFAULT 0,
            inviter_id INTEGER,
            PRIMARY KEY (guild_id, code)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            guild_id INTEGER,
            order_id TEXT PRIMARY KEY,
            buyer_id INTEGER,
            product_title TEXT,
            rating INTEGER,
            review TEXT,
            created_at TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS order_proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            message_id INTEGER,
            attachment_url TEXT,
            attachment_name TEXT,
            created_at TEXT
        )
    """)

    # Persistent reaction-role configuration
    con.execute("""
        CREATE TABLE IF NOT EXISTS reaction_role_panels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS reaction_roles (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (message_id, emoji)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_reaction_roles_message ON reaction_roles(message_id)")

    # Persistent giveaways
    con.execute("""
        CREATE TABLE IF NOT EXISTS giveaways (
            giveaway_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winners_count INTEGER NOT NULL DEFAULT 1,
            ends_at TEXT NOT NULL,
            image_url TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            winner_ids TEXT,
            created_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            entered_at TEXT NOT NULL,
            PRIMARY KEY (giveaway_id, user_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS giveaway_winner_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            giveaway_id TEXT NOT NULL,
            round_type TEXT NOT NULL,
            round_number INTEGER NOT NULL DEFAULT 1,
            winner_ids TEXT NOT NULL,
            selected_at TEXT NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_winner_history ON giveaway_winner_history(giveaway_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_giveaways_status_ends ON giveaways(status, ends_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_entries ON giveaway_entries(giveaway_id)")

    con.execute("CREATE INDEX IF NOT EXISTS idx_orders_channel ON orders(channel_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_proofs_order ON order_proofs(order_id)")

    # Safe migrations for the older shop database.
    order_columns = {row[1] for row in con.execute("PRAGMA table_info(orders)").fetchall()}
    if "updated_at" not in order_columns:
        con.execute("ALTER TABLE orders ADD COLUMN updated_at TEXT")
        con.execute("UPDATE orders SET updated_at=created_at WHERE updated_at IS NULL")
    if "proof_message_id" not in order_columns:
        con.execute("ALTER TABLE orders ADD COLUMN proof_message_id INTEGER")

    con.commit()
    return con


def get_payment_info(guild_id):
    con = get_db()
    row = con.execute(
        "SELECT payment_info FROM settings WHERE guild_id=?",
        (guild_id,)
    ).fetchone()
    con.close()
    return row[0] if row else None

# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class EverForgeBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        get_db().close()
        con = get_db()
        try:
            product_ids = [row[0] for row in con.execute("SELECT product_id FROM products").fetchall()]
        finally:
            con.close()
        for product_id in product_ids:
            self.add_view(BuyView(product_id))

        # Re-register active giveaway buttons so they keep working after a restart.
        con = get_db()
        try:
            active_giveaways = con.execute(
                "SELECT giveaway_id FROM giveaways WHERE status='active'"
            ).fetchall()
        finally:
            con.close()
        for (giveaway_id,) in active_giveaways:
            self.add_view(GiveawayView(giveaway_id), message_id=None)

        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash commands.")

    async def on_ready(self):
        # Cache invites for accurate invite tracking.
        for guild in self.guilds:
            await cache_guild_invites(guild)

        if not member_role_loop.is_running():
            member_role_loop.start()
        if not hasattr(bot, "giveaway_scheduler_task") or bot.giveaway_scheduler_task.done():
            bot.giveaway_scheduler_task = asyncio.create_task(giveaway_scheduler())

        print("=" * 45)
        print(f"Logged in as {self.user}")
        print("EverForge Bot is ONLINE!")
        print("=" * 45)

bot = EverForgeBot()

# ============================================================
# COMMUNITY FEATURES
# ============================================================

invite_cache = {}


def utc_now_iso():
    return datetime.utcnow().isoformat()


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def get_text_channel(guild, name):
    return discord.utils.get(guild.text_channels, name=name)


async def cache_guild_invites(guild):
    """Cache invite usage so the next join can identify the inviter."""
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return

    invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

    con = get_db()
    for invite in invites:
        con.execute(
            """
            INSERT INTO invite_cache(guild_id, code, uses, inviter_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, code)
            DO UPDATE SET uses=excluded.uses, inviter_id=excluded.inviter_id
            """,
            (guild.id, invite.code, invite.uses or 0, invite.inviter.id if invite.inviter else None)
        )
    con.commit()
    con.close()


async def detect_inviter(guild):
    """Return the invite code/inviter whose use count increased."""
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None, None

    old = invite_cache.get(guild.id, {})
    used_code = None
    inviter_id = None
    newest = {}

    for invite in invites:
        uses = invite.uses or 0
        newest[invite.code] = uses
        if uses > old.get(invite.code, 0):
            used_code = invite.code
            inviter_id = invite.inviter.id if invite.inviter else None

    invite_cache[guild.id] = newest

    con = get_db()
    for invite in invites:
        con.execute(
            """
            INSERT INTO invite_cache(guild_id, code, uses, inviter_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, code)
            DO UPDATE SET uses=excluded.uses, inviter_id=excluded.inviter_id
            """,
            (guild.id, invite.code, invite.uses or 0, invite.inviter.id if invite.inviter else None)
        )
    con.commit()
    con.close()

    if inviter_id:
        con = get_db()
        con.execute(
            """
            INSERT INTO invite_stats(guild_id, user_id, invites)
            VALUES (?, ?, 1)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET invites=invites+1
            """,
            (guild.id, inviter_id)
        )
        con.commit()
        con.close()

    return used_code, inviter_id


async def give_member_role_if_ready(member):
    if member.bot or not member.guild:
        return False

    role = discord.utils.get(member.guild.roles, name=MEMBER_ROLE)
    if role is None or role >= member.guild.me.top_role:
        return False

    joined_at = member.joined_at
    if joined_at is None:
        return False

    if datetime.utcnow() - joined_at.replace(tzinfo=None) < MEMBER_ROLE_DELAY:
        return False

    if role in member.roles:
        return False

    try:
        await member.add_roles(role, reason="EverForge: member has been in the server for 3 days")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


@tasks.loop(minutes=1)
async def member_role_loop():
    """Grant ・Members to eligible existing members and members whose timer elapsed."""
    for guild in bot.guilds:
        for member in guild.members:
            await give_member_role_if_ready(member)


@member_role_loop.before_loop
async def before_member_role_loop():
    await bot.wait_until_ready()


@bot.event
async def on_member_join(member: discord.Member):
    # Store the join time so the 3-day system survives restarts.
    joined_at = member.joined_at or discord.utils.utcnow()
    joined_naive = joined_at.replace(tzinfo=None)

    con = get_db()
    con.execute(
        """
        INSERT INTO member_joins(guild_id, user_id, joined_at)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET joined_at=excluded.joined_at
        """,
        (member.guild.id, member.id, joined_naive.isoformat())
    )
    con.commit()
    con.close()

    # This is the welcome message only — no separate generic join/leave log.
    channel = get_text_channel(member.guild, WELCOME_CHANNEL)
    if channel:
        count = member.guild.member_count or len(member.guild.members)
        embed = discord.Embed(
            description=(
                f"Welcome to **EverForge**, {member.mention}!\\n\\n"
                f"**You are our {count}th member!**"
            ),
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"{count} members • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # Update invite statistics. This is internal tracking, not a join notification.
    await detect_inviter(member.guild)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Boost notification only when the member starts boosting.
    if before.premium_since is None and after.premium_since is not None:
        channel = get_text_channel(after.guild, BOOST_CHANNEL)
        if channel:
            boost_count = after.guild.premium_subscription_count or 0
            embed = discord.Embed(
                title="🚀 New Server Boost!",
                description=f"Thank you **{after.mention}** for boosting **{after.guild.name}**!",
                color=discord.Color.fuchsia()
            )
            embed.set_thumbnail(url=after.display_avatar.url)
            embed.add_field(name="Boosts", value=f"**{boost_count}**", inline=True)
            embed.set_footer(text="EverForge • Server Boost")
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    # No boost-removal notification unless explicitly requested later.


@bot.tree.command(name="invites", description="View how many members you have invited.")
async def invites(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    con = get_db()
    row = con.execute(
        "SELECT invites FROM invite_stats WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, target.id)
    ).fetchone()
    con.close()
    count = row[0] if row else 0

    embed = discord.Embed(
        title="🔗 Invite Stats",
        description=f"{target.mention} has invited **{count}** member(s).",
        color=discord.Color.blurple()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="review", description="Leave one review for a completed/closed EverForge order.")
@app_commands.describe(order_id="Your EverForge order ID", rating="1-5 stars", review="Your review")
async def review(interaction: discord.Interaction, order_id: str, rating: int, review: str):
    if not 1 <= rating <= 5:
        return await interaction.response.send_message("❌ Rating must be between **1 and 5**.", ephemeral=True)
    if len(review.strip()) < 3:
        return await interaction.response.send_message("❌ Your review is too short.", ephemeral=True)
    if len(review) > 1000:
        return await interaction.response.send_message("❌ Your review must be 1000 characters or less.", ephemeral=True)

    con = get_db()
    order = con.execute(
        """
        SELECT product_title, status, buyer_id
        FROM orders
        WHERE order_id=? AND guild_id=?
        """,
        (order_id.strip().upper(), interaction.guild.id)
    ).fetchone()

    if not order:
        con.close()
        return await interaction.response.send_message("❌ I couldn't find that order.", ephemeral=True)

    product_title, status, buyer_id = order
    if buyer_id != interaction.user.id:
        con.close()
        return await interaction.response.send_message("❌ That order doesn't belong to you.", ephemeral=True)
    if status != "closed":
        con.close()
        return await interaction.response.send_message("❌ You can review an order after it has been closed/delivered.", ephemeral=True)

    existing = con.execute("SELECT 1 FROM reviews WHERE order_id=?", (order_id.strip().upper(),)).fetchone()
    if existing:
        con.close()
        return await interaction.response.send_message("❌ You already reviewed this order.", ephemeral=True)

    con.execute(
        "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
        (interaction.guild.id, order_id.strip().upper(), interaction.user.id, product_title, rating, review.strip(), utc_now_iso())
    )
    con.commit()

    stats = con.execute(
        "SELECT COUNT(*), AVG(rating) FROM reviews WHERE guild_id=?",
        (interaction.guild.id,)
    ).fetchone()
    con.close()

    channel = get_text_channel(interaction.guild, REVIEW_CHANNEL)
    if not channel:
        return await interaction.response.send_message(
            f"✅ Review saved! I couldn't find `{REVIEW_CHANNEL}` to publish it.", ephemeral=True
        )

    stars = "⭐" * rating + "☆" * (5 - rating)
    embed = discord.Embed(
        title="⭐ EverForge Customer Review",
        description=stars,
        color=discord.Color.gold()
    )
    embed.add_field(name="👤 Customer", value=interaction.user.mention, inline=True)
    embed.add_field(name="📦 Product", value=product_title, inline=True)
    embed.add_field(name="🆔 Order", value=f"`{order_id.strip().upper()}`", inline=True)
    embed.add_field(name="💬 Review", value=review.strip(), inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"Average: {stats[1]:.2f}/5 • {stats[0]} review(s)")

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass

    await interaction.response.send_message("⭐ Thank you! Your review has been posted.", ephemeral=True)

# ============================================================
# ERROR HANDLING
# ============================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You do not have the required permissions to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = "⏳ Please wait before using that command again."
    else:
        log.exception("Error executing slash command", exc_info=error)
        msg = "❌ An unexpected error occurred while processing your command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        pass

# ============================================================
# BASIC
# ============================================================

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 **Pong!** `{ms} ms`")

# ============================================================
# PAYMENT SETUP
# ============================================================

@bot.tree.command(name="setpayment", description="Set the payment information shown to buyers.")
@app_commands.checks.has_permissions(manage_guild=True)
async def setpayment(interaction: discord.Interaction, payment_info: str):
    con = get_db()
    con.execute("""
        INSERT INTO settings(guild_id, payment_info)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET payment_info=excluded.payment_info
    """, (interaction.guild.id, payment_info))
    con.commit()
    con.close()
    
    await interaction.response.send_message("✅ Payment information has been updated.", ephemeral=True)

# ============================================================
# SHOP POST
# ============================================================

class BuyView(discord.ui.View):
    def __init__(self, product_id):
        super().__init__(timeout=None)
        self.product_id = product_id
        button = discord.ui.Button(
            label="🛒 Buy",
            style=discord.ButtonStyle.green,
            custom_id=f"everforge:buy:{product_id}"
        )
        button.callback = self.buy
        self.add_item(button)

    async def buy(self, interaction: discord.Interaction):
        guild = interaction.guild
        buyer = interaction.user
        category = discord.utils.get(guild.categories, name=ORDER_CATEGORY)
        if category is None:
            return await interaction.response.send_message(
                f"❌ I couldn't find the category:\n`{ORDER_CATEGORY}`\n\nCreate it with that exact name.", ephemeral=True
            )

        con = get_db()
        try:
            product = con.execute("""
                SELECT title, description, price, currency
                FROM products WHERE guild_id=? AND product_id=?
            """, (guild.id, self.product_id)).fetchone()
            existing = None
            if product:
                existing = con.execute("""
                    SELECT channel_id FROM orders
                    WHERE guild_id=? AND buyer_id=? AND product_id=?
                    AND status IN ('pending','awaiting_verification','accepted')
                """, (guild.id, buyer.id, self.product_id)).fetchone()
        finally:
            con.close()

        if product is None:
            return await interaction.response.send_message("❌ This product no longer exists.", ephemeral=True)
        if existing:
            existing_channel = guild.get_channel(existing[0])
            if existing_channel:
                return await interaction.response.send_message(
                    f"❌ You already have an open order:\n{existing_channel.mention}", ephemeral=True
                )

        title, description, price, currency = product
        order_id = "EF-" + secrets.token_hex(4).upper()
        safe_name = "".join(c.lower() if c.isalnum() else "-" for c in buyer.display_name).strip("-")[:20] or "buyer"
        channel_name = f"order-{safe_name}-{order_id.lower()}"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            buyer: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_messages=True, attach_files=True, embed_links=True
            )
        }
        try:
            channel = await guild.create_text_channel(
                name=channel_name, category=category, overwrites=overwrites,
                topic=f"Order {order_id} | Buyer {buyer.id}"
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I can't create the order channel.\nGive the bot **Manage Channels** permission.", ephemeral=True
            )
        except discord.HTTPException as exc:
            log.exception("Order channel creation failed: %s", exc)
            return await interaction.response.send_message("❌ Discord failed to create the order channel.", ephemeral=True)

        con = get_db()
        try:
            con.execute("""
                INSERT INTO orders(order_id,guild_id,buyer_id,product_id,product_title,price,currency,status,channel_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (order_id,guild.id,buyer.id,self.product_id,title,price,currency,"pending",channel.id,datetime.utcnow().isoformat(),datetime.utcnow().isoformat()))
            con.commit()
        except Exception:
            con.rollback()
            try:
                await channel.delete(reason="Order database insert failed")
            except discord.HTTPException:
                pass
            raise
        finally:
            con.close()

        payment_info = get_payment_info(guild.id)
        if currency.lower() == "robux":
            price_text = f"💵 **{int(price):,} Robux**"
        else:
            price_text = f"💵 **€{price:.2f}**"
        payment_text = (f"{payment_info}\n\nAmount to pay: {price_text}" if payment_info else
                        "⚠️ Payment information has not been configured yet.\n\nPlease wait for staff.")

        # This is intentionally the same shop/order presentation you were using.
        embed = discord.Embed(title="════ Order ════", color=discord.Color.blurple())
        embed.add_field(name="📦 Product", value=f"**{title}**", inline=False)
        embed.add_field(name="📝 Description", value=description or "No description.", inline=False)
        embed.add_field(name="💰 Price", value=price_text, inline=True)
        embed.add_field(name="👤 Buyer", value=buyer.mention, inline=True)
        embed.add_field(name="🆔 Order ID", value=f"`{order_id}`", inline=True)
        embed.add_field(name="📋 Status", value="🟡 **Waiting for payment**", inline=False)
        embed.add_field(name="💳 Payment", value=payment_text, inline=False)
        embed.add_field(name="📸 Payment Proof", value="After paying, upload your **payment screenshot/proof** here.", inline=False)
        embed.set_footer(text=f"EverForge • {order_id}")
        await channel.send(content=buyer.mention, embed=embed)
        await channel.send(
            "👋 **Welcome to your order!**\n\n"
            "1️⃣ Pay using the payment information above.\n"
            "2️⃣ Upload your payment proof here.\n"
            "3️⃣ Wait for staff to verify it.\n\n"
            "⚠️ Never send passwords, account cookies, or other sensitive account credentials."
        )
        await interaction.response.send_message(
            f"✅ Your order has been created!\n\n🆔 Order ID: `{order_id}`\n📂 {channel.mention}", ephemeral=True
        )


@bot.tree.command(name="post", description="Create a shop product post.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    title="Product name", description="Product description", price="Price", 
    currency="EUR or Robux", image_url="Optional product image URL"
)
async def post(interaction: discord.Interaction, title: str, description: str, price: float, currency: str, image_url: str = None):
    currency = currency.lower().strip()
    if currency not in ("eur", "robux"):
        return await interaction.response.send_message("❌ Currency must be `EUR` or `Robux`.", ephemeral=True)
    if price <= 0:
        return await interaction.response.send_message("❌ Price must be greater than 0.", ephemeral=True)

    if currency == "robux":
        price = int(price)

    product_id = "P-" + secrets.token_hex(4).upper()
    con = get_db()
    con.execute("""
        INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (interaction.guild.id, product_id, title, description, price, currency.upper(), interaction.user.id, datetime.utcnow().isoformat()))
    con.commit()
    con.close()

    if currency == "ROBUX":
        price_text = f"💰 **{int(price):,} Robux**"
    else:
        price_text = f"💰 **€{price:.2f}**"

    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.add_field(name="Price", value=price_text, inline=False)
    embed.add_field(name="Product ID", value=f"`{product_id}`", inline=False)
    embed.set_footer(text="Click 🛒 Buy to create an order.")
    if image_url:
        embed.set_image(url=image_url)

    await interaction.response.send_message(embed=embed, view=BuyView(product_id))

# ============================================================
# ORDERS
# ============================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.TextChannel) and message.attachments:
        con = get_db()
        try:
            order = con.execute(
                "SELECT order_id,guild_id,buyer_id,status FROM orders WHERE channel_id=?",
                (message.channel.id,)
            ).fetchone()
            if order and message.author.id == order[2]:
                order_id, guild_id, buyer_id, status = order
                attachment = message.attachments[0]
                con.execute(
                    "INSERT INTO order_proofs(order_id,guild_id,buyer_id,message_id,attachment_url,attachment_name,created_at) VALUES(?,?,?,?,?,?,?)",
                    (order_id,guild_id,buyer_id,message.id,attachment.url,attachment.filename,datetime.utcnow().isoformat())
                )
                con.execute(
                    "UPDATE orders SET status='awaiting_verification', proof_message_id=?, updated_at=? WHERE order_id=?",
                    (message.id,datetime.utcnow().isoformat(),order_id)
                )
                con.commit()
                await message.channel.send(
                    "📸 **Payment proof received.**\n"
                    "Your proof has been sent for staff verification. You cannot mark the order as paid yourself.",
                    allowed_mentions=discord.AllowedMentions.none()
                )
                await message.channel.send(
                    f"🔔 **Staff:** payment proof uploaded for `{order_id}`.",
                    allowed_mentions=discord.AllowedMentions.none()
                )
        except sqlite3.Error:
            log.exception("Could not record payment proof")
        finally:
            con.close()
    await bot.process_commands(message)

def get_order(order_id, guild_id):
    con = get_db()
    try:
        return con.execute(
            "SELECT order_id,guild_id,buyer_id,product_id,product_title,price,currency,status,channel_id,created_at FROM orders WHERE order_id=? AND guild_id=?",
            (order_id.strip().upper(), guild_id)
        ).fetchone()
    finally:
        con.close()

def order_from_channel(channel_id):
    con = get_db()
    try:
        return con.execute(
            "SELECT order_id,guild_id,buyer_id,product_id,product_title,price,currency,status,channel_id,created_at FROM orders WHERE channel_id=?",
            (channel_id,)
        ).fetchone()
    finally:
        con.close()

def staff_check(interaction):
    return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.manage_channels

@bot.tree.command(name="accept", description="Accept payment for an order.")
@app_commands.checks.has_permissions(manage_channels=True)
async def accept(interaction: discord.Interaction, order_id: str = None):
    order = order_from_channel(interaction.channel.id) if not order_id else get_order(order_id, interaction.guild.id)
    if not order:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
    oid = order[0]
    await interaction.response.defer(ephemeral=True)
    con=get_db()
    try:
        con.execute("UPDATE orders SET status='accepted', updated_at=? WHERE order_id=?", (datetime.utcnow().isoformat(),oid)); con.commit()
    finally: con.close()
    channel=interaction.guild.get_channel(order[8]) or interaction.channel
    await channel.send(f"🟢 **Payment accepted** for `{oid}`. Staff may now deliver the product.")
    await interaction.followup.send(f"✅ Payment accepted for `{oid}`.", ephemeral=True)

@bot.tree.command(name="decline", description="Decline payment proof for an order.")
@app_commands.checks.has_permissions(manage_channels=True)
async def decline(interaction: discord.Interaction, order_id: str = None, reason: str = "Payment proof could not be verified."):
    order = order_from_channel(interaction.channel.id) if not order_id else get_order(order_id, interaction.guild.id)
    if not order:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
    oid=order[0]
    await interaction.response.defer(ephemeral=True)
    con=get_db()
    try:
        con.execute("UPDATE orders SET status='declined', updated_at=? WHERE order_id=?", (datetime.utcnow().isoformat(),oid)); con.commit()
    finally: con.close()
    channel=interaction.guild.get_channel(order[8]) or interaction.channel
    buyer=interaction.guild.get_member(order[2])
    await channel.send(f"🔴 **Payment declined** for `{oid}`.\nReason: {reason}\n{buyer.mention if buyer else ''}")
    await interaction.followup.send(f"❌ Payment declined for `{oid}`.", ephemeral=True)

@bot.tree.command(name="sendfile", description="Send the purchased file to the buyer.")
@app_commands.checks.has_permissions(manage_channels=True)
async def sendfile(interaction: discord.Interaction, file: discord.Attachment, order_id: str = None):
    order = order_from_channel(interaction.channel.id) if not order_id else get_order(order_id, interaction.guild.id)
    if not order:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
    oid=order[0]; channel=interaction.guild.get_channel(order[8]) or interaction.channel
    buyer=interaction.guild.get_member(order[2])
    await interaction.response.defer(ephemeral=True)
    try:
        upload=await file.to_file()
        await channel.send(content=f"📦 **Delivery for {buyer.mention if buyer else 'buyer'}**\nThank you for ordering from EverForge!", file=upload)
        con=get_db()
        try:
            con.execute("UPDATE orders SET status='delivered', updated_at=? WHERE order_id=?", (datetime.utcnow().isoformat(),oid)); con.commit()
        finally: con.close()
        await interaction.followup.send(f"✅ File delivered for `{oid}`.", ephemeral=True)
    except discord.HTTPException:
        await interaction.followup.send("❌ Discord could not send that file. Try again or upload a smaller file.", ephemeral=True)

@bot.tree.command(name="orders", description="Show your current orders.")
async def orders(interaction: discord.Interaction):
    con = get_db()
    rows = con.execute("""
        SELECT order_id, product_title, price, currency, status, channel_id
        FROM orders
        WHERE guild_id=? AND buyer_id=?
        ORDER BY created_at DESC LIMIT 10
    """, (interaction.guild.id, interaction.user.id)).fetchall()
    con.close()

    if not rows:
        return await interaction.response.send_message("📦 You don't have any orders.", ephemeral=True)

    lines = []
    for order_id, title, price, currency, status, channel_id in rows:
        if currency.upper() == "ROBUX":
            price_text = f"{int(price):,} Robux"
        else:
            price_text = f"€{price:.2f}"

        channel = interaction.guild.get_channel(channel_id)
        channel_text = channel.mention if channel else "Channel unavailable"
        lines.append(f"**`{order_id}`** — {title}\n💰 {price_text} • 📋 {status} • 📂 {channel_text}")

    embed = discord.Embed(title="📦 Your Orders", description="\n\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="closeorder", description="Close the current order channel.")
async def closeorder(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("❌ This isn't a text channel.", ephemeral=True)

    con = get_db()
    order = con.execute("SELECT order_id, buyer_id FROM orders WHERE channel_id=?", (channel.id,)).fetchone()
    if not order:
        con.close()
        return await interaction.response.send_message("❌ This isn't an order channel.", ephemeral=True)

    order_id, buyer_id = order
    is_staff = interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.manage_channels

    if interaction.user.id != buyer_id and not is_staff:
        con.close()
        return await interaction.response.send_message("❌ Only the buyer or staff can close this order.", ephemeral=True)

    con.execute("UPDATE orders SET status='closed' WHERE order_id=?", (order_id,))
    con.commit()
    con.close()

    await interaction.response.send_message("🔒 Order closed. This channel will be deleted in 5 seconds.")
    await asyncio.sleep(5)
    
    try:
        await channel.delete(reason=f"Order {order_id} closed")
    except discord.NotFound:
        pass


# ============================================================
# ROLE MANAGEMENT
# ============================================================

@bot.tree.command(name="roleeveryone", description="Give a role to every non-bot member in the server.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(role="The role to give to everyone")
async def roleeveryone(interaction: discord.Interaction, role: discord.Role):
    """Safely give a role to all members, respecting Discord role hierarchy."""
    guild = interaction.guild
    me = guild.me

    if role.is_default():
        return await interaction.response.send_message("❌ You can't give the @everyone role.", ephemeral=True)
    if role.managed:
        return await interaction.response.send_message("❌ I can't manually assign an integration/bot-managed role.", ephemeral=True)
    if me is None or role >= me.top_role:
        return await interaction.response.send_message(
            "❌ I can't assign that role. Move my highest role **above** the target role.",
            ephemeral=True,
        )
    if interaction.user != guild.owner and role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "❌ That role is equal to or higher than your highest role.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    added = 0
    skipped = 0
    failed = 0

    # Process sequentially to avoid hammering Discord's role endpoints.
    for member in guild.members:
        if member.bot or role in member.roles:
            skipped += 1
            continue
        try:
            await member.add_roles(role, reason=f"/roleeveryone by {interaction.user}")
            added += 1
            # Small pacing delay keeps a large server from immediately hitting rate limits.
            await asyncio.sleep(0.08)
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    await interaction.followup.send(
        f"✅ **Role assignment complete**\n"
        f"Role: {role.mention}\n"
        f"Added: **{added}**\n"
        f"Already had/skipped: **{skipped}**\n"
        f"Failed: **{failed}**",
        ephemeral=True,
    )


# ============================================================
# MODERATION
# ============================================================

def hierarchy_error(interaction, member):
    if member == interaction.user:
        return "❌ You can't moderate yourself."
    if member == interaction.guild.owner:
        return "❌ You can't moderate the server owner."
    if interaction.user != interaction.guild.owner and member.top_role >= interaction.user.top_role:
        return "❌ That member has an equal/higher role than you."
    if interaction.guild.me and member.top_role >= interaction.guild.me.top_role:
        return "❌ My highest role must be above that member."
    return None


@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    error = hierarchy_error(interaction, member)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 **{member}** was kicked.\nReason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't kick that member.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    error = hierarchy_error(interaction, member)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 **{member}** was banned.\nReason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't ban that member.", ephemeral=True)


@bot.tree.command(name="timeout", description="Timeout a member.")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    if not 1 <= minutes <= 40320:
        return await interaction.response.send_message("❌ Use 1-40320 minutes.", ephemeral=True)
    error = hierarchy_error(interaction, member)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(f"⏳ **{member}** timed out for **{minutes} minutes**.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't timeout that member.", ephemeral=True)


@bot.tree.command(name="untimeout", description="Remove a timeout.")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None)
        await interaction.response.send_message(f"✅ Timeout removed from **{member}**.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't remove that timeout.", ephemeral=True)


@bot.tree.command(name="warn", description="Warn a member.")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    # A database lock can legitimately take longer than Discord's 3-second initial
    # response window. Defer first, then write and use a follow-up response.
    await interaction.response.defer(ephemeral=True)
    try:
        con = get_db()
        try:
            con.execute("""
                INSERT INTO warnings VALUES (?, ?, ?, ?, ?)
            """, (interaction.guild.id, member.id, interaction.user.id, reason, datetime.utcnow().isoformat()))
            con.commit()
        finally:
            con.close()
        await interaction.followup.send(f"⚠️ **{member}** warned.\nReason: {reason}", ephemeral=True)
    except sqlite3.OperationalError as exc:
        log.exception("Warning database error: %s", exc)
        await interaction.followup.send("❌ The database was busy. Please try `/warn` again in a moment.", ephemeral=True)


@bot.tree.command(name="warnings", description="View warnings for a member.")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    con = get_db()
    rows = con.execute("""
        SELECT reason, moderator_id, created_at FROM warnings
        WHERE guild_id=? AND user_id=?
        ORDER BY rowid DESC
    """, (interaction.guild.id, member.id)).fetchall()
    con.close()

    if not rows:
        return await interaction.response.send_message("✅ No warnings.", ephemeral=True)

    lines = [f"**{i}.** {reason} — <@{moderator}>" for i, (reason, moderator, created) in enumerate(rows[:10], 1)]
    await interaction.response.send_message(f"⚠️ **Warnings for {member.mention}**\n\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="purge", description="Delete messages.")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    if not 1 <= amount <= 100:
        return await interaction.response.send_message("❌ Amount must be between 1 and 100.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted **{len(deleted)} messages**.", ephemeral=True)


@bot.tree.command(name="lock", description="Lock the current channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message("🔒 Channel locked.")


@bot.tree.command(name="unlock", description="Unlock the current channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=None)
    await interaction.response.send_message("🔓 Channel unlocked.")


@bot.tree.command(name="slowmode", description="Set channel slowmode.")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int):
    if not 0 <= seconds <= 21600:
        return await interaction.response.send_message("❌ Use 0-21600 seconds.", ephemeral=True)
    await interaction.channel.edit(slowmode_delay=seconds)
    message = "🐌 Slowmode disabled." if seconds == 0 else f"🐌 Slowmode set to **{seconds} seconds**."
    await interaction.response.send_message(message)

# ============================================================
# REACTION ROLES
# ============================================================

reaction_group = app_commands.Group(name="reaction", description="Configure reaction roles for your server.")


def normalize_reaction_emoji(emoji: str) -> str:
    emoji = emoji.strip()
    if emoji.startswith("<") and emoji.endswith(">"):
        parsed = discord.PartialEmoji.from_str(emoji)
        if parsed.id:
            return str(parsed)
    return emoji


def parse_reaction_emoji(emoji: str):
    value = normalize_reaction_emoji(emoji)
    if value.startswith("<") and value.endswith(">"):
        parsed = discord.PartialEmoji.from_str(value)
        if parsed.id:
            return parsed
    return value


def reaction_role_embed(title: str, description: str, mappings):
    lines = []
    for emoji, role_id in mappings:
        lines.append(f"{emoji}  →  <@&{role_id}>")
    text = description.strip()
    if lines:
        text += "\n\n" + "\n".join(lines)
    return discord.Embed(title=title.strip(), description=text, color=discord.Color.blurple())


@reaction_group.command(name="create", description="Create a reaction-role panel in a channel.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(channel="Channel where the reaction-role panel should be posted", title="Panel title", description="Instructions shown to members")
async def reaction_create(interaction: discord.Interaction, title: str, description: str, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("❌ Choose a text channel.", ephemeral=True)
    if not title.strip() or not description.strip():
        return await interaction.response.send_message("❌ Title and description cannot be empty.", ephemeral=True)
    me = interaction.guild.me
    if not me or not channel.permissions_for(me).send_messages or not channel.permissions_for(me).embed_links:
        return await interaction.response.send_message("❌ I need **Send Messages** and **Embed Links** in that channel.", ephemeral=True)

    embed = reaction_role_embed(title, description, [])
    message = await channel.send(embed=embed)
    con = get_db()
    try:
        con.execute(
            "INSERT INTO reaction_role_panels(guild_id,channel_id,message_id,title,description,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (interaction.guild.id, channel.id, message.id, title.strip(), description.strip(), interaction.user.id, utc_now_iso())
        )
        con.commit()
    finally:
        con.close()
    await interaction.response.send_message(f"✅ Reaction-role panel created: {message.jump_url}\nUse `/reaction add` to connect emojis to roles.", ephemeral=True)


@reaction_group.command(name="add", description="Add an emoji → role to a reaction-role panel.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(message_id="ID of the reaction-role panel message", emoji="Emoji to react with", role="Role members receive")
async def reaction_add(interaction: discord.Interaction, message_id: int, emoji: str, role: discord.Role):
    con = get_db()
    try:
        panel = con.execute("SELECT channel_id FROM reaction_role_panels WHERE message_id=? AND guild_id=?", (message_id, interaction.guild.id)).fetchone()
        if not panel:
            return await interaction.response.send_message("❌ That message is not a configured reaction-role panel.", ephemeral=True)
        normalized = normalize_reaction_emoji(emoji)
        con.execute("INSERT OR REPLACE INTO reaction_roles(guild_id,message_id,emoji,role_id) VALUES(?,?,?,?)", (interaction.guild.id, message_id, normalized, role.id))
        mappings = con.execute("SELECT emoji, role_id FROM reaction_roles WHERE message_id=? ORDER BY rowid", (message_id,)).fetchall()
        panel_row = con.execute("SELECT title, description FROM reaction_role_panels WHERE message_id=?", (message_id,)).fetchone()
        con.commit()
    finally:
        con.close()

    me = interaction.guild.me
    if role.is_default() or role.managed:
        return await interaction.response.send_message("❌ That role cannot be assigned by a bot.", ephemeral=True)
    if not me or role >= me.top_role:
        return await interaction.response.send_message("❌ My highest role must be above that role.", ephemeral=True)
    channel = interaction.guild.get_channel(panel[0])
    if not channel:
        return await interaction.response.send_message("❌ I cannot find the panel channel.", ephemeral=True)
    try:
        message = await channel.fetch_message(message_id)
        await message.add_reaction(parse_reaction_emoji(normalized))
        await message.edit(embed=reaction_role_embed(panel_row[0], panel_row[1], mappings))
    except discord.NotFound:
        return await interaction.response.send_message("❌ The panel message no longer exists.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ I don't have permission to manage reactions or edit that message.", ephemeral=True)
    except discord.HTTPException:
        return await interaction.response.send_message("❌ Discord rejected that emoji. Use a valid Unicode or custom server emoji.", ephemeral=True)
    await interaction.response.send_message(f"✅ Added {normalized} → {role.mention}", ephemeral=True)


@reaction_group.command(name="remove", description="Remove an emoji → role from a panel.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(message_id="ID of the reaction-role panel message", emoji="Emoji mapping to remove")
async def reaction_remove(interaction: discord.Interaction, message_id: int, emoji: str):
    normalized = normalize_reaction_emoji(emoji)
    con = get_db()
    try:
        panel = con.execute("SELECT channel_id,title,description FROM reaction_role_panels WHERE message_id=? AND guild_id=?", (message_id, interaction.guild.id)).fetchone()
        if not panel:
            return await interaction.response.send_message("❌ Reaction-role panel not found.", ephemeral=True)
        deleted = con.execute("DELETE FROM reaction_roles WHERE message_id=? AND emoji=?", (message_id, normalized)).rowcount
        mappings = con.execute("SELECT emoji, role_id FROM reaction_roles WHERE message_id=? ORDER BY rowid", (message_id,)).fetchall()
        con.commit()
    finally:
        con.close()
    if not deleted:
        return await interaction.response.send_message("❌ That emoji is not configured on this panel.", ephemeral=True)
    channel = interaction.guild.get_channel(panel[0])
    if channel:
        try:
            message = await channel.fetch_message(message_id)
            await message.clear_reaction(parse_reaction_emoji(normalized))
            await message.edit(embed=reaction_role_embed(panel[1], panel[2], mappings))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    await interaction.response.send_message(f"✅ Removed {normalized} from the panel.", ephemeral=True)


@reaction_group.command(name="list", description="List all emoji → role mappings on a panel.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(message_id="ID of the reaction-role panel message")
async def reaction_list(interaction: discord.Interaction, message_id: int):
    con = get_db()
    try:
        rows = con.execute("SELECT emoji, role_id FROM reaction_roles WHERE message_id=? ORDER BY rowid", (message_id,)).fetchall()
    finally:
        con.close()
    if not rows:
        return await interaction.response.send_message("ℹ️ No reaction roles are configured on that panel.", ephemeral=True)
    lines = [f"{emoji} → <@&{role_id}>" for emoji, role_id in rows]
    await interaction.response.send_message("**Reaction roles**\n" + "\n".join(lines), ephemeral=True)


@reaction_group.command(name="delete", description="Delete a reaction-role panel and its configuration.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(message_id="ID of the reaction-role panel message")
async def reaction_delete(interaction: discord.Interaction, message_id: int):
    con = get_db()
    try:
        panel = con.execute("SELECT channel_id FROM reaction_role_panels WHERE message_id=? AND guild_id=?", (message_id, interaction.guild.id)).fetchone()
        if not panel:
            return await interaction.response.send_message("❌ Reaction-role panel not found.", ephemeral=True)
        con.execute("DELETE FROM reaction_roles WHERE message_id=?", (message_id,))
        con.execute("DELETE FROM reaction_role_panels WHERE message_id=? AND guild_id=?", (message_id, interaction.guild.id))
        con.commit()
    finally:
        con.close()
    channel = interaction.guild.get_channel(panel[0])
    if channel:
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    await interaction.response.send_message("✅ Reaction-role panel deleted.", ephemeral=True)


bot.tree.add_command(reaction_group)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not payload.guild_id or payload.user_id == bot.user.id:
        return
    emoji = normalize_reaction_emoji(str(payload.emoji))
    con = get_db()
    try:
        row = con.execute("SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=? AND guild_id=?", (payload.message_id, emoji, payload.guild_id)).fetchone()
    finally:
        con.close()
    if not row:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    role = guild.get_role(row[0])
    if not member or not role or role.is_default() or role.managed:
        return
    me = guild.me
    if not me or role >= me.top_role or role in member.roles:
        return
    try:
        await member.add_roles(role, reason="Reaction role")
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Could not add reaction role %s to member %s in guild %s", role.id, member.id, guild.id)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if not payload.guild_id or payload.user_id == bot.user.id:
        return
    emoji = normalize_reaction_emoji(str(payload.emoji))
    con = get_db()
    try:
        row = con.execute("SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=? AND guild_id=?", (payload.message_id, emoji, payload.guild_id)).fetchone()
    finally:
        con.close()
    if not row:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    role = guild.get_role(row[0])
    if not member or not role or role.is_default() or role.managed:
        return
    me = guild.me
    if not me or role >= me.top_role or role not in member.roles:
        return
    try:
        await member.remove_roles(role, reason="Reaction role removed")
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Could not remove reaction role %s from member %s in guild %s", role.id, member.id, guild.id)


# GIVEAWAY
# ============================================================

GIVEAWAY_BUTTON_PREFIX = "ef_giveaway_enter:"


def parse_giveaway_duration(value: str):
    """Parse friendly durations: 30s, 5m, 1h, 1h30m, 1h 30m 15s, etc.
    A plain number is treated as minutes for backwards compatibility.
    """
    import re
    value = str(value or "").strip().lower().replace(",", " ")
    if not value:
        return None
    if value.isdigit():
        seconds = int(value) * 60
    else:
        compact = re.sub(r"\s+", "", value)
        matches = re.findall(r"(\d+(?:\.\d+)?)(s|m|h)", compact)
        if not matches or "".join(n + u for n, u in matches) != compact:
            return None
        seconds = sum(float(n) * {"s": 1, "m": 60, "h": 3600}[u] for n, u in matches)
        seconds = int(seconds)
    if seconds < 10 or seconds > 30 * 86400:
        return None
    return seconds


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if secs or not parts:
        parts.append(f"{secs} second" + ("s" if secs != 1 else ""))
    return " ".join(parts)


def format_giveaway_ends(dt):
    timestamp = int(dt.timestamp())
    return f"<t:{timestamp}:F>\n<t:{timestamp}:R>"


def giveaway_embed(giveaway_row, entries, ended=False, winner_mentions=None, duration_seconds=None):
    (
        giveaway_id, guild_id, channel_id, message_id, host_id,
        prize, winners_count, ends_at, image_url, status, winner_ids, created_at
    ) = giveaway_row[:12]
    ends_dt = parse_iso(ends_at) or datetime.utcnow()
    host_mention = f"<@{host_id}>"

    title = f"🎉 {prize}"
    description = "🔴 **This giveaway has ended.**" if ended else "Click the button below to enter the giveaway!"
    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    embed.add_field(name="Ended" if ended else "Ends", value=format_giveaway_ends(ends_dt), inline=False)
    embed.add_field(name="Hosted by", value=host_mention, inline=True)
    embed.add_field(name="Entries", value=f"**{entries:,}**", inline=True)
    embed.add_field(name="Winners", value=f"**{winners_count}**", inline=True)

    if ended:
        if winner_mentions:
            embed.add_field(name="Winner" if len(winner_mentions) == 1 else "Winners", value="\n".join(winner_mentions), inline=False)
        else:
            embed.add_field(name="Winner", value="No valid entries.", inline=False)
    elif duration_seconds:
        embed.add_field(name="Duration", value=format_duration(duration_seconds), inline=False)

    if image_url:
        embed.set_thumbnail(url=image_url)

    created_dt = parse_iso(created_at) or datetime.utcnow()
    embed.set_footer(text=f"EverForge — Giveaway • {created_dt.strftime('%d/%m/%Y %H:%M UTC')}")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str, disabled=False):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        button = discord.ui.Button(
            label="🎉 Enter Giveaway",
            style=discord.ButtonStyle.green,
            custom_id=f"{GIVEAWAY_BUTTON_PREFIX}{giveaway_id}",
            disabled=disabled,
        )
        button.callback = self.enter
        self.add_item(button)

    async def enter(self, interaction: discord.Interaction):
        con = get_db()
        try:
            row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (self.giveaway_id,)).fetchone()
            if row is None:
                return await interaction.response.send_message("❌ This giveaway no longer exists.", ephemeral=True)
            ends_at = parse_iso(row[7])
            if row[9] != "active" or not ends_at or datetime.utcnow() >= ends_at:
                return await interaction.response.send_message("⏰ This giveaway has already ended.", ephemeral=True)

            existing = con.execute("SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (self.giveaway_id, interaction.user.id)).fetchone()
            if existing:
                con.execute("DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (self.giveaway_id, interaction.user.id))
                message = "❌ You left the giveaway."
            else:
                con.execute("INSERT INTO giveaway_entries(giveaway_id,user_id,entered_at) VALUES(?,?,?)", (self.giveaway_id, interaction.user.id, utc_now_iso()))
                message = "🎉 You entered the giveaway! Good luck!"
            con.commit()
            entry_count = con.execute("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (self.giveaway_id,)).fetchone()[0]
            row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (self.giveaway_id,)).fetchone()
        finally:
            con.close()

        await interaction.response.send_message(message, ephemeral=True)
        try:
            channel = interaction.guild.get_channel(row[2])
            if channel:
                public_message = await channel.fetch_message(row[3])
                ends = parse_iso(row[7])
                created = parse_iso(row[11])
                duration = int((ends-created).total_seconds()) if ends and created else None
                await public_message.edit(embed=giveaway_embed(row, entry_count, duration_seconds=duration), view=GiveawayView(self.giveaway_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Could not update giveaway message %s", self.giveaway_id)


async def draw_giveaway_winners(giveaway_id: str, round_type: str, exclude_ids=None):
    exclude_ids = set(exclude_ids or [])
    con = get_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
        if not row:
            con.rollback()
            return None, "Giveaway not found."
        entries = [r[0] for r in con.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,)).fetchall()]
        eligible = [uid for uid in entries if uid not in exclude_ids]
        if not eligible:
            con.rollback()
            return row, "There are no eligible entries for this draw."
        count = min(max(1, row[6]), len(eligible))
        winners = random.sample(eligible, count)
        old_history = con.execute("SELECT COALESCE(MAX(round_number),0) FROM giveaway_winner_history WHERE giveaway_id=?", (giveaway_id,)).fetchone()[0]
        round_number = old_history + 1
        winner_ids = ",".join(map(str, winners))
        con.execute("INSERT INTO giveaway_winner_history(giveaway_id,round_type,round_number,winner_ids,selected_at) VALUES(?,?,?,?,?)", (giveaway_id, round_type, round_number, winner_ids, utc_now_iso()))
        con.execute("UPDATE giveaways SET winner_ids=?, status=? WHERE giveaway_id=?", (winner_ids, "ended" if round_type == "initial" else row[9], giveaway_id))
        con.commit()
        row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
        return row, winners
    finally:
        con.close()


async def end_giveaway(giveaway_id: str):
    con = get_db()
    try:
        row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
    finally:
        con.close()
    if not row or row[9] != "active":
        return
    ends_dt = parse_iso(row[7])
    if not ends_dt or datetime.utcnow() < ends_dt:
        return

    con = get_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
        if not current or current[9] != "active":
            con.rollback(); return
        entries = [r[0] for r in con.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,)).fetchall()]
        winners = random.sample(entries, min(current[6], len(entries))) if entries else []
        winner_ids = ",".join(map(str, winners))
        con.execute("UPDATE giveaways SET status='ended', winner_ids=? WHERE giveaway_id=? AND status='active'", (winner_ids, giveaway_id))
        con.execute("INSERT INTO giveaway_winner_history(giveaway_id,round_type,round_number,winner_ids,selected_at) VALUES(?,?,?,?,?)", (giveaway_id, "initial", 1, winner_ids, utc_now_iso()))
        con.commit()
        row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
    finally:
        con.close()

    winner_mentions = [f"<@{uid}>" for uid in winners]
    channel = bot.get_channel(row[2])
    if not channel: return
    try:
        message = await channel.fetch_message(row[3])
        entry_count = len(entries)
        await message.edit(embed=giveaway_embed(row, entry_count, ended=True, winner_mentions=winner_mentions), view=GiveawayView(giveaway_id, disabled=True))
        if winners:
            await message.reply(f"🎉 Congratulations {', '.join(winner_mentions)}!\nYou won **{row[5]}**!")
        else:
            await message.reply("😢 The giveaway ended with no entries, so there was no winner.")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.exception("Could not finish giveaway message %s", giveaway_id)


async def giveaway_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            con = get_db()
            try:
                rows = con.execute("SELECT giveaway_id, ends_at FROM giveaways WHERE status='active'").fetchall()
            finally:
                con.close()
            now = datetime.utcnow()
            for giveaway_id, ends_at in rows:
                ends_dt = parse_iso(ends_at)
                if ends_dt and now >= ends_dt:
                    await end_giveaway(giveaway_id)
        except Exception:
            log.exception("Giveaway scheduler error")
        await asyncio.sleep(5)


async def giveaway_autocomplete(interaction: discord.Interaction, current: str):
    con = get_db()
    try:
        rows = con.execute("SELECT giveaway_id, prize FROM giveaways WHERE guild_id=? AND status='ended' ORDER BY created_at DESC LIMIT 25", (interaction.guild.id,)).fetchall()
    finally:
        con.close()
    current = current.lower()
    return [app_commands.Choice(name=f"{gid} — {prize[:70]}", value=gid) for gid, prize in rows if not current or current in gid.lower() or current in prize.lower()][:25]


@bot.tree.command(name="giveaway", description="Create a polished EverForge giveaway.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(prize="Prize, e.g. 5,000 Robux", duration="Use 30s, 5m, 1h, or 1h 30m", winners="Number of winners (1-20)", image="Optional direct image URL")
async def giveaway(interaction: discord.Interaction, prize: str, duration: str, winners: int = 1, image: str = None):
    if not prize.strip():
        return await interaction.response.send_message("❌ Prize cannot be empty.", ephemeral=True)
    if not 1 <= winners <= 20:
        return await interaction.response.send_message("❌ Winners must be between 1 and 20.", ephemeral=True)
    seconds = parse_giveaway_duration(duration)
    if seconds is None:
        return await interaction.response.send_message("❌ Invalid duration. Examples: `30s`, `5m`, `1h`, `1h 30m`, `2h 15m 10s`.", ephemeral=True)
    if image and not image.startswith(("http://", "https://")):
        return await interaction.response.send_message("❌ Image must be a valid `http://` or `https://` URL.", ephemeral=True)

    await interaction.response.defer()
    giveaway_id = "GW-" + secrets.token_hex(5).upper()
    created_at = datetime.utcnow()
    ends_at = created_at + timedelta(seconds=seconds)
    row = (giveaway_id, interaction.guild.id, interaction.channel.id, 0, interaction.user.id, prize.strip(), winners, ends_at.isoformat(), image, "active", None, created_at.isoformat())
    message = await interaction.followup.send(embed=giveaway_embed(row, 0, duration_seconds=seconds), view=GiveawayView(giveaway_id), wait=True)
    con = get_db()
    try:
        con.execute("INSERT INTO giveaways(giveaway_id,guild_id,channel_id,message_id,host_id,prize,winners_count,ends_at,image_url,status,winner_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (giveaway_id, interaction.guild.id, interaction.channel.id, message.id, interaction.user.id, prize.strip(), winners, ends_at.isoformat(), image, "active", None, created_at.isoformat()))
        con.commit()
    finally:
        con.close()
    final_row = (giveaway_id, interaction.guild.id, interaction.channel.id, message.id, interaction.user.id, prize.strip(), winners, ends_at.isoformat(), image, "active", None, created_at.isoformat())
    await interaction.edit_original_response(embed=giveaway_embed(final_row, 0, duration_seconds=seconds), view=GiveawayView(giveaway_id))


@bot.tree.command(name="reroll", description="Reroll the winner(s) of an ended giveaway.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(giveaway_id="Choose the ended giveaway to reroll")
@app_commands.autocomplete(giveaway_id=giveaway_autocomplete)
async def reroll(interaction: discord.Interaction, giveaway_id: str):
    await interaction.response.defer(ephemeral=True)
    con = get_db()
    try:
        row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=? AND guild_id=?", (giveaway_id, interaction.guild.id)).fetchone()
        if not row:
            return await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        if row[9] != "ended":
            return await interaction.followup.send("❌ You can only reroll an ended giveaway.", ephemeral=True)
        previous = set(int(x) for x in (row[10] or "").split(",") if x.strip().isdigit())
        entries = [r[0] for r in con.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,)).fetchall()]
    finally:
        con.close()
    eligible = [uid for uid in entries if uid not in previous]
    if not eligible:
        return await interaction.followup.send("❌ There are no eligible entries left for a reroll.", ephemeral=True)
    count = min(row[6], len(eligible))
    winners = random.sample(eligible, count)
    winner_ids = ",".join(map(str, winners))
    con = get_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT * FROM giveaways WHERE giveaway_id=? AND status='ended'", (giveaway_id,)).fetchone()
        if not current:
            con.rollback(); return await interaction.followup.send("❌ Giveaway state changed. Try again.", ephemeral=True)
        round_number = con.execute("SELECT COALESCE(MAX(round_number),0)+1 FROM giveaway_winner_history WHERE giveaway_id=?", (giveaway_id,)).fetchone()[0]
        con.execute("INSERT INTO giveaway_winner_history(giveaway_id,round_type,round_number,winner_ids,selected_at) VALUES(?,?,?,?,?)", (giveaway_id, "reroll", round_number, winner_ids, utc_now_iso()))
        con.execute("UPDATE giveaways SET winner_ids=? WHERE giveaway_id=?", (winner_ids, giveaway_id))
        con.commit()
        row = con.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
    finally:
        con.close()

    mentions = [f"<@{uid}>" for uid in winners]
    try:
        channel = bot.get_channel(row[2])
        message = await channel.fetch_message(row[3]) if channel else None
        entry_count = len(entries)
        if message:
            await message.edit(embed=giveaway_embed(row, entry_count, ended=True, winner_mentions=mentions))
            await message.reply(f"🔄 **Giveaway rerolled!** Congratulations {', '.join(mentions)}!\nYou won **{row[5]}**!")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.exception("Could not update rerolled giveaway %s", giveaway_id)
    await interaction.followup.send(f"✅ Rerolled **{giveaway_id}**. New winner(s): {', '.join(mentions)}", ephemeral=True)



# ============================================================
# EXECUTION
# ============================================================

if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    
    if not TOKEN:
        print("=" * 45)
        print("ERROR: DISCORD_TOKEN environment variable is not set.")
        print("Please set your bot token as an environment variable before running.")
        print("=" * 45)
    else:
        bot.run(TOKEN)