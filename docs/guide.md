# Hearthling guide

[Back to the overview](../README.md)

I wanted a Discord companion people could actually talk to in a server, with real moderation and server administration kept strictly under my own control. Hearthling runs one or more Discord bot accounts from a single process, with a localhost web dashboard for switching models, editing character presets, managing shared memories and configuring tools without touching config files every time.

Each bot account gets its own character, model preference and conversation history, while sharing one pool of lasting memories and lore across the server. You can run a single assistant for your community or add extra chatbot accounts with their own personalities so they can talk to members - and occasionally to each other - without handing those extra accounts any administrative tools.

## Quick start

You need Python 3.12+ and a Discord bot application with both **Server Members Intent** and **Message Content Intent** enabled under the Bot tab in the Discord Developer Portal.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python first_run.py
```

`first_run.py` asks for your Discord bot token, your server ID, your Discord user ID (the owner), and your main AI provider address and API key. It derives the application ID from the token if you leave that prompt blank, then writes `config.json` and `credentials.json` with `0600` permissions.

Check that your Discord permissions and AI connection work, then start the bot through the launcher:

```sh
.venv/bin/python hearthling.py --check
.venv/bin/python launcher.py
```

If your bot will manage roles, channels or moderation, invite it with Administrator permission and put its highest role above the roles and members it needs to manage.

## Talking to Hearthling

Anyone in the configured server can @mention the bot, say its name ('Hearthling' by default, or whatever character preset is active) or reply directly to one of its messages. Name matching checks whole words and ignores case, so partial word matches don't set it off. Replies link back to the triggering message, and if you reply to someone else's message while addressing the bot, it reads that replied-to message as context provided you have permission to view it.

DMs, moderation and private server-inspection actions are locked to the configured owner ID in code, not just in the prompt. Try these in a DM with the bot, or in the server:

```text
What channels and roles can you see?
Give @Alice the Member role.
Time out @Alice for 10 minutes for repeated spam.
Delete the last 10 messages from @Alice in #general.
Create a private staff channel for the Moderator role.
```

Each bot keeps separate conversation history per person per channel, so other members' conversations never see your DM history or moderation receipts. Up to three model requests run concurrently across the programme, with one active turn per person per bot. Completed requests and tool receipts are recorded to SQLite before replies go out, so a gateway reconnect won't repeat an action.

## Assistant bots and chatbots

When you add extra Discord bot accounts in the dashboard, each account is set as either an **Assistant** or a **Chatbot**:

- **Assistant:** conversation, shared memories and server expressions, plus whichever tools you enable (pictures, Brave search, reminders, emoji/sticker uploads and owner administration). Moderation, private server inspection, host checks and dashboard links still require the owner.
- **Chatbot:** conversation, memories and existing server emojis, reactions and stickers. Every practical tool call is rejected in code, even if the owner asks. Invitation links for chatbots request basic chat permissions instead of Administrator.

The primary account starts as an assistant; extra accounts default to chatbot unless you switch them in the **Bots** tab.

## Member identity, pings and personal colours

Bots can resolve server members by username, global display name, server nickname or Discord ID. If a name matches more than one person, the bot asks you to clarify instead of guessing. Saying 'Hearthling, ping Alice' sends a real `<@id>` mention in the current channel even when ordinary reply pings are muted - capped at three people per request with a one-minute cooldown per pair, and never allowing `@everyone` or role pings.

Assistant bots can also give a member their own colour role on request. They create a permission-free, non-hoisted, non-mentionable `colour-<user_id>` role positioned below the bot's top role and reuse it on later changes. They never recolour shared or staff roles, grant permissions or change someone else's colour. Use `#010101` for near-black, since Discord treats `#000000` as transparent.

```text
Hearthling, ping Alice.
Hearthling, who is Alice? Show her username and server nickname.
Hearthling, change my personal role colour to #b873e8.
```

## Server emojis, reactions and stickers

Bots know the usable custom emojis and stickers in their server. They can use custom emojis inline, add up to two reactions to a message and send one sticker per reply.

When expression uploads are enabled, assistants can also add a new server emoji or sticker from an attached image, a replied-to image, an avatar or a public HTTPS image link (PNG, JPEG, GIF or WebP up to 8 MiB). Images are resized to fit Discord's canvas while keeping animation within Discord's size limits. You can keep uploads open to the server or restrict them to the owner in **Tools**.

```text
Hearthling, react to this message with the tea emoji.
Hearthling, add the attached image as an emoji called cozy_mug.
Hearthling, turn this attached GIF into a sticker called Cozy time.
```

## Pictures, web search and host health checks

Assistants can generate images, search the web through Brave Search, read public web pages and run read-only host health checks.

- **Pictures:** supports OpenAI Images (`/images/generations` and `/images/edits`), OpenAI Responses (`image_generation` tool) and Google Gemini image generation (`:generateContent`). When a request or replied-to message includes a readable image, Hearthling passes the image bytes to the provider for editing rather than describing it from memory. Every generated picture is followed by the exact prompt sent to the image model. You can set an optional house art style in **Tools** (or leave it blank), add up to three fallback API keys that rotate only on confirmed HTTP 429 daily-quota rejections, and configure named backup image providers.
- **Grounded subject appearances:** if someone asks 'draw me and Alice in a café', a coordinator step pulls the saved server-wide appearance memories for each subject before calling the image model so hair, eye colour, glasses and outfits stay consistent without leaking other people's private channel facts.
- **Brave web and image search:** returns titles, snippets and links, plus a page reader that can open public URLs and read them in sections or attach original images found on the page with source links.
- **Host health check:** owner-only. Reads CPU load, uptime, memory, disk space, network counters and the `systemctl show` state of whichever systemd units you list under **Host health** in **Tools**. It never runs arbitrary shell commands.

## Lasting memories and lore

Facts learned about members are stored in SQLite with activation keywords and shared across all managed bots. A memory is injected into the prompt only when one of its keywords matches the current conversation, up to a configurable character budget (4000 characters by default), and bots can also search memories explicitly with their recall tool.

Appearance facts get a server-wide scope so any bot can recall what someone looks like across channels. Personal facts stay scoped to the person, and forgetting a fact also purges that person's recent turn history across bots so an old cached turn doesn't re-teach the deleted fact on the next message.

```text
Hearthling, remember that I take my coffee black.
Hearthling, what do you remember about me?
Hearthling, forget my coffee preference.
```

The owner can also write manual **lore** entries scoped to every bot, a specific bot, the server, a user or a single channel.

## Automation, Wordle, GitHub releases and RSS roundups

Scheduled posts are opt-in. Spontaneous replies have a small default chance. Configure these per bot in the **Automation** tab:

- **Random conversation starters:** posts in selected channels after a random interval outside quiet hours.
- **Spontaneous replies:** per-channel reply chance and cooldown, plus a tiny baseline chance (0.1% reply, 0.03% with a picture) in non-quiet channels. You can mark specific channels as **Quiet channels** so the bot never jumps in unprompted there unless named or @mentioned.
- **Bot-to-bot exchanges:** when multiple managed bots share a channel, an eligible bot has a 10% chance to reply once to another managed bot before stopping, so they don't loop forever.
- **Daily Wordle:** fetches the real NYT Wordle answer for the day, plays up to six guesses using the bot's own model and posts the spoiler-tagged share card (`X/6` if it loses, which rough models sometimes do).
- **GitHub release announcements:** polls any list of `owner/repo` repositories every five minutes and posts newly published releases (including pre-releases, never drafts) to a chosen channel. The first check baselines existing tags so it doesn't flood the channel on setup.
- **RSS / Atom news roundups:** polls your list of RSS or Atom feed URLs every N hours, filters unseen entries from the last 48 hours using your editable topic prompt, and posts a short sourced roundup with `<url>` links.

## Characters and provider connections

The **Personality** tab stores Markdown character cards. Only `## System Persona`, `## Example Dialogue`, `## Fallbacks` and the subsection of `## User Context` matching the current speaker's username enter the prompt; everything else (like `## Credits`) is ignored, and the preview tool shows exactly what was kept.

**Connections** supports OpenAI Chat Completions, OpenAI Responses, Anthropic Messages and Google Gemini, plus custom JSON/YAML body overrides and private headers. The main connection also accepts a list of fallback model IDs that reuse the same base URL and key in order when the primary model fails.

## Owner dashboard

The dashboard listens on `127.0.0.1:8787` by default (set `HEARTHLING_PUBLIC_URL` if you put it behind a reverse proxy). DM any running bot:

```text
dashboard
```

Or generate a one-time sign-in link from your terminal:

```sh
.venv/bin/python hearthling.py --dashboard-link
```

Sign-in links are single-use, expire after ten minutes and set an HTTP-only session cookie for seven days.

## Running checks

All 15 test suites run offline without live Discord or AI accounts:

```sh
for f in test_*.py; do .venv/bin/python "$f" || break; done
```

## License

MIT - see [LICENSE](../LICENSE).
