# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it through GitHub private vulnerability reporting: open the repository's **Security**
tab and choose **Report a vulnerability**. That sends the report privately to the maintainer.

Include what you found, how to reproduce it, and which version or commit you tested.

## Scope and known limits

netlea-ble is a hobby project maintained in spare time, with no guaranteed response time.

By design the HTTP API has no authentication, listens on all interfaces and allows any
browser origin. It is meant for a trusted local network behind a firewall. That design is
documented in the README and is not by itself a vulnerability report, but suggestions for
safer defaults are welcome.
