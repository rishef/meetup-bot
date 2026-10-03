# Meetup Midpoint Bot

A Discord bot for finding a fair place to meet. Each member saves their address privately. When some of them want to meet, the bot shows only the middle point of their addresses, never anyone's actual address.

## Commands

| Command | Who sees the reply | What it does |
|---|---|---|
| `/register address:<text>` | Only you | Looks up your address and saves it for this server. Run it again to change it. |
| `/myaddress` | Only you | Shows the address you saved. |
| `/unregister` | Only you | Deletes your address. |
| `/registered` | Only you | Lists who has saved an address. It shows names only. |
| `/midpoint members:@a @b @c` | Channel | Finds the meeting point for the people you mention. You are included unless you set `include_me:False`. |
| `/meetup title:<text>` | Channel | Posts a meetup with **I'm in**, **Leave** and **Find midpoint** buttons. People join by clicking, and the organizer presses **Find midpoint**. |

The meeting point is shown as a nearby street or area name, its coordinates, and Google Maps and OpenStreetMap links. Anyone without a saved address is left out of the calculation and named in the reply.

## Setup

1. **Create the bot.** Go to <https://discord.com/developers/applications>, click **New Application**, open **Bot** and click **Reset Token**, then copy the token. No privileged intents are needed.
2. **Invite it.** Go to **OAuth2 → URL Generator**. Tick the scopes `bot` and `applications.commands`, and the bot permissions *Send Messages* and *Embed Links*. Open the URL it gives you and pick your server.
3. **Configure it:**
   ```bash
   cp .env.example .env
   ```
   Then put your token in `DISCORD_TOKEN`. While you're testing, also set `DEV_GUILD_ID` to your server's ID so the commands show up right away.
4. **Run it:**
   ```bash
   pip install -r requirements.txt
   python bot.py
   ```

## How it works

- **Storage:** SQLite (`meetup.db`). Addresses are stored separately for each server. The buttons on meetup posts keep working after the bot restarts.
- **Address lookup:** [OpenStreetMap Nominatim](https://nominatim.org/). It is free and needs no API key, but it allows at most 1 request per second, and the bot keeps to that limit. Set `NOMINATIM_USER_AGENT` to something that identifies you. If all your members live in one country, set `NOMINATIM_COUNTRY_CODES` (for example `in` or `sg`) so lookups are more accurate.
- **Midpoint:** each location is turned into a 3D point on the globe, the points are averaged, and the result is turned back into a latitude and longitude. This stays correct across the date line, where simply averaging latitudes and longitudes would give a wrong answer.

## Privacy notes

- Addresses only ever appear in replies that only their owner can see.
- **When only two people meet, the midpoint gives away the other person's location.** If you know your own address and the midpoint, the other person is the same distance away on the opposite side. With three or more people, you can't work out anyone's address this way.
- The midpoint can land somewhere you can't meet, such as a lake or a highway. Treat it as the area to look in, not the exact spot.
