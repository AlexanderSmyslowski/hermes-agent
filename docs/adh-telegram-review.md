# ADH Telegram Review

Hermes can expose Agent Data Hub draft cards in Telegram for trusted internal
review. This is a human review channel, not an authentication or permission
system.

Required environment:

```bash
TELEGRAM_BOT_TOKEN=...
DATABASE_URL=postgresql://...
HERMES_ADH_REVIEWERS_JSON='{"123456789":"alice"}'
```

Agent Data Hub must be installed in the same Python environment, or otherwise
available on `PYTHONPATH`, so Hermes can import `agent_hub.review_api`. If that
facade is missing, Hermes reports ADH Review as unavailable and does not fall
back to ADH internals, CLI commands, or shell calls.

Optional:

```bash
HERMES_ADH_REVIEW_MAX_CARDS=5
AGENT_HUB_REVIEWERS=alice,bob
```

`HERMES_ADH_REVIEWERS_JSON` maps trusted Telegram chat or user IDs to ADH
reviewer handles. Handles are validated by Agent Data Hub through
`agent_hub.review_api`, so `AGENT_HUB_REVIEWERS` is still respected when set.

Flow:

1. The reviewer sends `/adh_inbox`.
2. Hermes maps the Telegram sender to a reviewer handle.
3. Hermes fetches only draft cards assigned to that reviewer.
4. The reviewer presses `Merken` or `Verwerfen`.
5. ADH records `reviewed_by=<handle>` and `review_source="telegram"` in its
   normal review metadata and audit trail.

Boundaries:

- unknown Telegram senders cannot read or write ADH review data
- invalid reviewer handles cannot read or write ADH review data
- every accept/reject requires an explicit button press
- there is no bulk accept, auto accept, role model, or tenancy model
- Telegram bot messages are not end-to-end encrypted and pass through Telegram
  servers, so only non-sensitive Hub-policy-safe card content may appear
- channel code lives in Hermes; ADH remains the reviewed-memory and audit core
