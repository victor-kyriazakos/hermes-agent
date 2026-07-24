---
title: Automatic Home Adoption
description: "Opt in to adopting the first authenticated Relay direct message as the gateway home channel"
---

# Automatic Home Adoption

Hermes normally asks you to run `/sethome` before it delivers cron results and cross-platform messages to a chat. Relay-backed single-owner deployments can opt in to adopting the first authenticated direct message automatically:

```yaml
gateway:
  auto_home: true
```

The default is `false`.

When enabled, Hermes adopts a chat only when all of these conditions hold:

- the message was delivered through Relay;
- Relay authenticated the sender and provided a user ID;
- the message is a direct message;
- Relay explicitly advertises support for the logical platform, such as Slack;
- no home is already configured for that platform.

Hermes stores the logical platform and authenticated owner provenance in `config.yaml`, then confirms the adoption in the chat. Run `/sethome` in another chat to replace the home.

This is a behavioral setting, not a credential. Configure it in `config.yaml`; do not add it to `.env`.
