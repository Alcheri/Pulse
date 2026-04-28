<!-- Pulse: RSS 2.0 feed and announcement plugin for Limnoria. -->

<h1 align="center">Pulse</h1>

<p align="center">
  <em>RSS 2.0 feed polling and announcement plugin for Limnoria.</em>
</p>

## Features

- Registers named RSS 2.0 feeds for later use
- Fetches the latest feed items on demand
- Announces new feed items to configured channels
- Persists registered feeds and seen item IDs across reloads
- Rejects unsupported feed formats instead of pretending they worked

## Install

From your Limnoria plugins directory:

```bash
git clone https://github.com/Alcheri/Pulse.git
```

Install any runtime requirements:

```bash
pip install --upgrade -r requirements.txt
```

Load the plugin:

```text
/msg bot load Pulse
```

## Configuration

Enable Pulse for a channel:

```text
config channel #channel plugins.Pulse.enabled True
```

Add a feed:

```text
@pulse add xkcd https://xkcd.com/rss.xml
```

Note: the current implementation is intentionally **RSS 2.0-first**. Atom feeds are
not part of the supported surface yet.

Subscribe the current channel to announcements:

```text
@pulse announce add limnorianews
```

That command means "announce the registered feed `limnorianews` here". To
target another channel explicitly, use:

```text
@pulse announce add #channel limnorianews
```

Default first-subscribe behaviour is **no backfill**: existing items are marked as
seen without being announced.

## Commands

```text
@pulse add <name> <url>
@pulse remove <name>
@pulse list
@pulse show <name>
@pulse latest <name> [count]
@pulse refresh [name]
@pulse announce list [#channel]
@pulse announce add [#channel] <feed> [feed...]
@pulse announce remove [#channel] <feed> [feed...]
```

## Notes

- Feed output uses the channel `headlineFormat` template.
- Pulse stores feed state under Limnoria's `data/` directory.
- A later phase can add feed-specific formatting, keyword filters, and richer
  status reporting.

This project is licensed under the BSD 3-Clause Licence. See the [LICENCE](LICENCE.md) file for details.
