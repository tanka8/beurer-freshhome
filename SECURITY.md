# Security

## What this integration handles

Your Beurer account email and password are stored in Home Assistant's config entry,
in the same way every cloud integration stores credentials, and are sent only to
`sso.connect.beurer.com` and `freshhome.connect.beurer.com`. Nothing is sent
anywhere else, and nothing is collected by this project or its author.

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

Diagnostics downloads have the email, password, and device identifiers removed, and
the device record is filtered to the model and name rather than dumped whole. Log
lines are not filtered by anyone - check them for access tokens before posting.
