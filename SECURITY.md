# Security

## What this integration handles

Your Beurer account email, and an OAuth **refresh token**, are stored in Home
Assistant's config entry, in the same way every cloud integration stores credentials.
Your password is used to sign in and is not stored once Beurer has issued a refresh
token and accepted it for a refresh. If Beurer issues none, or will not honour it,
the password is kept instead and used as before. See "What is stored" in the README,
including how existing installs are upgraded.

Whichever is held, it is sent only to `sso.connect.beurer.com` and
`freshhome.connect.beurer.com`. Nothing is sent anywhere else, and nothing is
collected by this project or its author.

The refresh token can sign in to your Beurer account until Beurer revokes it, so a
backup of Home Assistant's `.storage` should be protected as it would be with a
password in it.

The `client_secret` in `const.py` is not a secret of yours. It is a constant
embedded in the FreshHome app, identical for every user of it, and is published here
deliberately - see the README.

## Reporting a problem

Open a [GitHub issue](https://github.com/tanka8/beurer-freshhome/issues). If the
problem would expose someone's credentials by being described in public, use GitHub's
[private vulnerability reporting](https://github.com/tanka8/beurer-freshhome/security/advisories/new)
instead.

Please be aware of what the README says about support: this is a hobby project, and
there is no commitment to a fix or to a response within any particular time. There is
no security support beyond a best effort.

## Before pasting logs or diagnostics

Diagnostics downloads have the value of every stored credential removed - the email,
password and refresh token, by name and by default for anything else the entry gains -
and the device identifiers; the device record is filtered to the model and name rather
than dumped whole. Log lines are not filtered by anyone - check them for access tokens
before posting. This integration's own log lines and error messages leave out request
URLs, because Beurer's API carries the account email in one of them; but a device id
can still appear, and Home Assistant names the config entry (your email) in some of
its own lines about it.
