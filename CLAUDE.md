# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

zoffline (zwift-offline) is a partial reimplementation of the Zwift game server that lets you use the Zwift client offline (single player by default; multi-player when `storage/multiplayer.txt` exists). It impersonates `us-or-rly101.zwift.com`, `secure.zwift.com`, `cdn.zwift.com`, and `launcher.zwift.com` so the Zwift game client can be redirected to it via the OS `hosts` file (or a fake DNS server) plus a self-signed certificate (`ssl/cert-zwift-com.pem` / `.p12`).

Do **not** expose it to the public internet — it was not designed for that.

## Common commands

### Install dependencies

```sh
pip install -r requirements.txt
# Optional integrations
pip install garth          # Garmin Connect uploads
pip install discord.py     # Discord chat bridge
```

### Run from source

`standalone.py` is the unified entry point. It starts the HTTP (CDN) server on TCP 80, the encrypted TCP relay on 3025, the encrypted UDP game server on 3024, and (optionally) the fake DNS on UDP 53. Ports 80/443/53 are privileged.

```sh
sudo ./standalone.py                # Linux / macOS
python standalone.py                # Windows (run as Administrator)
```

`zwift-offline.wsgi` is a WSGI shim that exposes `zwift_offline.app` for an external WSGI server (e.g. mod_wsgi under `apache/zwift-offline.conf`); it does **not** start the TCP/UDP servers — use `standalone.py` for that.

### Run via Docker / Compose

```sh
docker build -t zoffline .
docker compose up -d
# or use the published image:
#   docker create --name zwift-offline -p 443:443 -p 80:80 -p 3024:3024/udp -p 3025:3025 -p 53:53/udp \
#     -v <host_storage>:/usr/src/app/zwift-offline/storage -e TZ=<tz> zoffline/zoffline
```

### Build the Windows standalone .exe

```sh
pip install pyinstaller garth upx
pyinstaller standalone.spec          # output -> dist/standalone.exe
```

GitHub Actions builds both the Docker image (`.github/workflows/ci.yml`, multi-arch amd64+arm64 on push/tag) and the Windows exe (`.github/workflows/pyinstaller.yaml`, on push to master) — there is no test workflow.

### Regenerate the protobuf Python stubs

```sh
cd protobuf
make                 # runs protoc --python_out=. on every .proto
make clean           # remove generated *_pb2.py
```

A Windows equivalent lives in `protobuf/make.bat`. The committed `*_pb2.py` files are generated; do not edit them by hand — edit the `.proto` and rerun `make`.

### One-off helper scripts (in `scripts/`)

Most take Zwift account credentials or a player ID and pull data from the real Zwift server — useful when migrating an existing profile, populating the bot/pace-partner pools, or refreshing maps/segments:

- `get_profile.py -u <username>` — fetch `profile.bin`, `achievements.bin`, `economy_config.txt` (single player default).
- `strava_auth.py --client-id … --client-secret …` — OAuth dance to get a Strava token.
- `get_gameassets.py` — refresh `cdn/gameassets/` content from Zwift.
- `get_game_dictionary.py`, `get_climbs.py`, `get_start_lines.py`, `get_events.py`, `get_entitlements.py`, `gen_schedule.py` — refresh the JSON files in `data/`.
- `get_pro_names.py`, `get_strava_names.py` — populate `storage/bot.txt` with realistic bot names.
- `find_equip.py` — produce `storage/ghost_profile.txt`.
- `bot_editor.py` — edit recorded `profile.bin` / `route.bin` for RoboPacer loops.
- `login_to_json.py`, `variants_to_json.py`, `upload_activity.py` — misc. conversions.
- `configure_client.bat`, `disable_zoffline.bat`, `launch.bat` (Windows only) — install/remove the `hosts` entry and certificate.

### Tests

There is no test suite. Validate changes by running `standalone.py` and launching the Zwift client against it (Step 2 of the README is the canonical client-side setup).

## High-level architecture

Two long-running Python files, plus a few supporting modules. Both share a single import-time bootstrap.

### `standalone.py` — game-server side

Implements the encrypted wire protocol Zwift uses in-game. Three sockets are bound on startup:

| Port | Proto | Handler | Purpose |
|------|-------|---------|---------|
| 80   | TCP   | `CDNHandler` (SimpleHTTPRequestHandler) | Serves the bundled `cdn/` assets; proxies `gameassets/*` to the real CDN when `storage/cdn-proxy.txt` exists. Special-cases `MapSchedule_v2.xml` / `PortalRoadSchedule_v1.xml` to honor the launcher's map/climb override. |
| 3025 | TCP   | `TCPHandler` | Out-of-band "relay" channel: AES-GCM encrypted `ClientToServer` / `ServerToClient` (`udp_node_msgs_pb2`). Hands the client the UDP endpoint, forwards `subsSegments` acks, and streams `PlayerUpdate` messages from `zo.player_update_queue`. |
| 3024 | UDP   | `UDPHandler` | Real-time in-game channel: position updates, chat, segment results, ride state. Uses the same `encode_packet` / `decode_packet` helpers and per-relay AES-GCM keys stored in `storage/<player_id>/encryption_key.bin`. |

`standalone.py` also owns the bot / ghost / pace-partner simulation: `load_bots`, `play_bots`, `load_pace_partners`, `play_pace_partners`, `load_ghosts`, `regroup_ghosts`, `remove_inactive`, plus a `DiscordThread` (or a no-op dummy) wired in when `storage/discord.cfg` exists. The `fake_dns` thread only starts when `storage/fake-dns.txt` exists (used by the Android non-rooted flow).

### `zwift_offline.py` — HTTP / launcher / REST side

This is the **Flask** app — it is the only thing you serve through a real WSGI server. The supported endpoints are the JSON/protobuf APIs Zwift calls for login, profiles, activities, segment results, events, entitlements, the launcher, the settings page, etc. Notable routes:

- Web (HTML): `/`, `/login/`, `/signup/`, `/forgot/`, `/reset/<u>/`, `/profile/<u>/`, `/strava/<u>/`, `/garmin/<u>/`, `/intervals/<u>/`, `/power_curves/<u>/`, `/settings/<u>/`, `/user/<u>/`, `/download/...`, `/delete/...`, `/restart`, `/cancelrestart`, `/reloadbots`. Templates live in `cdn/static/web/launcher/`.
- REST: `/api/...` — `api_users`, push-token registration, event feed, recommendations, campaigns, announcements, subscriptions, clubs, etc. Most responses are derived from the SQLAlchemy DB or from `data/*.txt` lookups; no real upstream calls.
- Static assets: served from `cdn/gameassets/` via Flask's `static_folder` and a few `send_from_directory` calls.
- Auth: Flask-Login + `pyjwt`. Hard-coded tokens in `tokens.py` (a long-lived Keycloak-style JWT) let the Zwift launcher think the OIDC dance succeeded; the actual password check is local (PBKDF2 `werkzeug.security.check_password_hash`).

The file also defines the **SQLAlchemy models** that back the offline data store: `User`, `AnonUser` (single-player fallback when `multiplayer.txt` is absent), `Activity`, `SegmentResult`, `RouteResult`, `Goal`, `Playback`, `Zfile`, `PrivateEvent`, `Notification`, `ActivityFile`, `ActivityImage`, `PowerCurve`, `Version`. The DB is a single SQLite file at `storage/zwift-offline.db`. Schema version lives in `DATABASE_CUR_VER`; mismatches trigger an automatic migration in `run_standalone()`.

`run_standalone(...)` (called from the bottom of `standalone.py`) wires the Flask app, gevent WSGI server, DB migrations, and the cross-thread queues (`online`, `player_update_queue`, `zc_connect_queue`, `map_override`, `climb_override`, etc.) together — it is the function that actually runs the launcher in-process.

### `online_sync.py` — real-Zwift client helper

Thin `requests`-based client used by the helper scripts (`get_profile.py`, `strava_auth.py`, `login_to_json.py`, `upload_activity.py`) to talk to `secure.zwift.com` and `us-or-rly101.zwift.com`. It is **not** used at server runtime — only the import in `zwift_offline.py` matters (it provides the constants the server hands to the Zwift client).

### `fake_dns.py` / `discord_bot.py` / `tokens.py`

- `fake_dns.py` — minimal UDP DNS server that maps `secure.zwift.com.` and `us-or-rly101.zwift.com.` to the local IP and forwards everything else to 8.8.8.8.
- `discord_bot.py` — optional Discord bridge; `DiscordThread` runs the bot event loop in its own thread, `DiscordBot` relays chat to `zwift_offline.send_message` and shows rider counts.
- `tokens.py` — long-lived JWT strings used by the launcher login flow.

### Protobuf schemas

`protobuf/*.proto` define every wire message used by both the launcher and in-game clients. `make` in `protobuf/Makefile` regenerates the committed `*_pb2.py` files with `protoc --python_out=.`. The main modules used in the server:

- `profile_pb2` — `PlayerProfile`, profiles, friends.
- `login_pb2` — login requests/responses.
- `activity_pb2`, `goal_pb2`, `per_session_info_pb2`, `segment_result_pb2`, `route_result_pb2` — ride data.
- `udp_node_msgs_pb2`, `tcp_node_msgs_pb2` — the encrypted in-game wire format.
- `world_pb2`, `zfiles_pb2`, `hash_seeds_pb2`, `events_pb2`, `variants_pb2`, `playback_pb2`, `user_storage_pb2` — supporting payloads.

When Zwift ships a new client and the wire format changes, the corresponding `.proto` is updated and the `*_pb2.py` files regenerated; `zwift_offline.py` and `standalone.py` are then patched to handle the new fields. The `Zwift_ver_cur.xml` `sversion` attribute is read at startup and is the canonical "what client does this server match?" string.

### Storage layout (`storage/`)

- `zwift-offline.db` — SQLite database (User, Activity, etc.).
- `secret-key.txt`, `credentials-key.bin` — Flask session secret / AES key for credential files; created on first run.
- `multiplayer.txt` — existence flips single-player → multi-player (see `MULTIPLAYER` flag in `zwift_offline.py`).
- `server-ip.txt` — IP the Zwift client is told to reach (defaults to the local LAN IP detected via UDP socket trick).
- `cdn-proxy.txt`, `fake-dns.txt`, `enable_bots.txt`, `enable_ghosts.txt`, `all_time_leaderboards.txt`, `unlock_entitlements.txt`, `unlock_all_equipment.txt`, `auto_launch.txt` — feature toggles (each is just a "file exists" flag, contents are usually ignored).
- `garmin_domain.txt`, `garmin_credentials.txt` (per-player), `strava_token.txt` (per-player), `garth/` (per-player), `discord.cfg`, `gmail_credentials.txt` — integration credentials.
- `<player_id>/` — per-player directory. Holds `profile.bin`, `encryption_key.bin`, `achievements.bin`, `economy_config.txt`, `ghosts/<world>/<route>`, `bookmarks/`, `fit/`, `images/`, `logfiles/`, `customworkouts/`, `customgearing/`, `last_activity.bin`, `zwift_credentials.bin`, `garmin_credentials.bin`. **Back this directory up** — the README explicitly warns that switching between single-player and multi-player can lose activities/segments/goals if you do not.

### `data/`

JSON lookups loaded once at startup: `climbs.txt`, `events.txt`, `game_dictionary.txt` (numeric keys are coerced to int via the custom `object_hook`), `start_lines.txt`, `variants.txt`, `economy_config.txt`, `entitlements.txt`, `names.txt`, `game_info.txt`. These are refreshed via the `get_*` scripts in `scripts/`.

### `cdn/`

Static game assets the client would normally pull from `cdn.zwift.com`. The launcher HTML/JS lives in `cdn/static/web/launcher/`. `MapSchedule_v2.xml` and `PortalRoadSchedule_v1.xml` are hot-patched by `CDNHandler.do_GET` when the launcher sets a map/climb override.

### `pace_partners/`

Saved activities that act as the recorded motion of the in-game RoboPacer / Pace Partner bots. Each subdirectory is one bot's route — the names encode difficulty and target (e.g. `B - 3.0 WKG - Watopia - Sand and Sequoias`).

### `apache/`

`zwift-offline.conf` and `cdn.zwift.com.conf` are example vhosts for hosting `zwift_offline.app` and the `cdn/` static tree behind Apache when you do not want to use `standalone.py`'s built-in HTTP server.

## Things to know before changing code

- `zwift_offline.py` and `standalone.py` share **module-level state** (`online`, `player_update_queue`, `global_relay`, etc.) and import-time side effects (DB migration, config parsing, `STORAGE_DIR` creation, Discord thread start). Reloading one without the other will break things — always start via `standalone.py`.
- The encrypted protocol uses AES-GCM with a per-relay key in `storage/<player_id>/encryption_key.bin`; the IV is reconstructed from `(DeviceType, ChannelType, ci, sn)` — do not change `InitializationVector` without updating both encode and decode.
- `MULTIPLAYER` is checked at import time; toggling it requires a restart and the README warns of data loss if you flip-flop.
- `ZWIFT_VER_CUR` is parsed at import time from `cdn/gameassets/Zwift_Updates_Root/Zwift_ver_cur.xml`. If you bump the supported client version, update that XML and the matching `.proto` changes.
- Many DB columns are intentionally named `f5`, `f22`, `act_f32` etc. — those are proto fields whose meaning has not been reverse-engineered; preserve the names when adding migrations.
- `pip install garth` and `pip install discord.py` are imported lazily; the modules must be optional at runtime.

## Community

- Discord: <https://discord.gg/GMdn8F8>
- Strava club: <https://www.strava.com/clubs/zoffline>
- License: GPL (see `LICENSE`).
