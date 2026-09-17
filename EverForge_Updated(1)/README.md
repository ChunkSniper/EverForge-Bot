# EverForge Bot

Discord bot with shop/order tools, moderation, invites, member roles, giveaways, reviews, and configurable reaction roles.

## Reaction roles

Use these slash commands in Discord:

- `/reaction create` — create a reaction-role panel. Choose the channel, title, and instructions.
- `/reaction add` — connect an emoji to a Discord role using the panel message ID.
- `/reaction remove` — remove an emoji → role mapping.
- `/reaction list` — view mappings for a panel.
- `/reaction delete` — delete a panel and its mappings.

Members react to the panel message to receive the configured role. Removing their reaction removes the role.

### Required bot permissions

The bot needs:

- Manage Roles
- Add Reactions
- Read Message History
- Send Messages
- Embed Links

The bot's highest role must be above every role it needs to assign.

## Running

Set the `DISCORD_TOKEN` environment variable and run:

```bash
python EverForge_final_main.py
```

The SQLite database (`everforge.sqlite3`) is created automatically beside the bot when it runs.
