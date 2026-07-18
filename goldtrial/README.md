# GoldTrial Red-DiscordBot Cog

GoldTrial provisions an M3U account through the documented Gold Panel API, treats it as a 24-hour trial, and disables the provider account after the trial expires.

## Guarantees

- Maximum 10 simultaneous bot-managed trials by default.
- One lifetime trial per Discord user.
- Expired and revoked users remain permanently ineligible.
- A slot is not promised before the provider account is successfully created.
- A slot is released only after the provider account is disabled successfully.
- Provider timeouts are marked `unknown` and must be reconciled manually to prevent duplicate accounts.
- Active credentials are shown only in ephemeral Discord responses.

## Important provider limitation

The supplied API does not document a dedicated demo endpoint. This cog calls:

```text
action=new&type=m3u&sub=1
```

It then calls `device_status=disable` after 24 hours. Confirm that creating a one-month M3U account is the intended way to consume one of your provider's trial lines.

## Install locally

Extract the repository to a permanent directory visible to the Red process. The directory passed to `addpath` must contain the `goldtrial` folder.

Using your `/` text prefix, send these as normal Red text commands:

```text
/addpath /absolute/path/to/goldtrial-cog
/load goldtrial
```

Downloader installs the `aiosqlite` requirement when installed from a Git repository. For a local path install, install it in Red's virtual environment if it is not already present:

```bash
python -m pip install 'aiosqlite>=0.20,<1'
```

## Configure the API key

Preferred for Docker or systemd:

```env
GOLDPANEL_API_KEY=replace_with_your_real_key
```

Restart Red after adding the environment variable.

Alternatively, use Red's shared API token storage in a private owner-only channel, then delete the command message:

```text
/set api goldpanel api_key,YOUR_REAL_API_KEY
```

Never commit the key or paste it into a public channel.

## Enable slash commands

After loading the cog:

```text
/slash enablecog GoldTrial
/slash sync
```

Red's `/` text prefix can conflict with Discord's slash command picker. The two commands above are Red text commands. Send them as normal messages.

## Configure AAA3A Tickets

Use a dedicated trial profile and include the Discord user ID in the generated channel name:

```text
/settickets channelname trial trial-{owner_id}
```

This is required by the default strict validation and prevents a member added to another user's ticket from claiming that ticket's trial.

Set the welcome message:

```text
/settickets welcomemessage trial "Welcome {owner_mention}! Run `/trial claim` in this ticket to provision your one-time 24-hour trial. Each Discord user can receive only one lifetime trial."
```

## Configure GoldTrial

Run the slash command and select the category and log channel from Discord's UI:

```text
/trialadmin setup
```

Arguments:

- `package_id`: Gold Panel bouquet/package ID.
- `trial_category`: Category containing open trial ticket channels.
- `logs_channel`: Private support log channel.
- `country`: `ALL`, `CA`, or another documented two-letter country code.

Set the capacity explicitly:

```text
/trialadmin capacity maximum:10 manual_reserved:0
```

`manual_reserved` accounts for active trials created outside this bot. For example, if two of your ten trial lines are already in use manually, set it to `2`.

Verify settings:

```text
/trialadmin settings
/trialadmin availability
```

Enable provisioning:

```text
/trialadmin enable enabled:True
```

## User workflow

Inside their dedicated `trial-<discord_user_id>` ticket, the user runs:

```text
/trial claim
```

The cog:

1. Confirms the ticket category, channel prefix, user ID in the channel name, and explicit owner permission override.
2. Confirms no lifetime claim record exists.
3. Disables any overdue accounts before calculating availability.
4. Calculates `available = maximum - manual_reserved - consumed_slots`.
5. Reserves the Discord user's lifetime record.
6. Calls the Gold Panel API.
7. Returns credentials ephemerally only after provider success.
8. Disables the provider account after 24 hours.

Additional user commands:

```text
/trial status
/trial credentials
```

## Administration

```text
/trialadmin availability
/trialadmin lookup user:@member
/trialadmin revoke user:@member
/trialadmin resolveunknown user:@member outcome:<choice>
/trialadmin ticketnames prefix:trial- require_user_id:True
```

`resolveunknown` must only be used after checking the provider panel:

- **No account was created** removes the reservation and restores eligibility.
- **Account existed and has been disabled** finalizes the record and permanently consumes eligibility.

There is intentionally no command that resets a completed user's eligibility.

## Required bot permissions

In the trial category and log channel, allow the bot:

- View Channel
- Send Messages
- Embed Links
- Read Message History

AAA3A Tickets still requires its own channel management permissions to create ticket channels.

## Data

The SQLite database is stored in Red's normal cog data directory as `goldtrial.sqlite3`. Back up Red's data directory. Losing the database or Red Config data will also lose lifetime eligibility history.
