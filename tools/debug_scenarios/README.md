# Debug scenarios

Scripted, deterministic inputs for debug mode (`FOREX_DEBUG_MODE=1`) and the
offline e2e tests. One scenario file describes the fake market's price path
(and, once the fake Telegram reader lands, the signal script that plays
against it).

Format (JSON):

```json
{
  "description": "what this scenario demonstrates",
  "market": {
    "anchors": [[0, 2400.0], [300, 2412.0], [600, 2398.0]]
  },
  "signals": []
}
```

- `market.anchors` — `[seconds_from_start, mid_price]` points; the fake
  market interpolates linearly between them and holds the last value after
  the script ends. Omit `market` entirely for the seeded synthetic stream.
- `signals` — reserved for the fake Telegram reader (stage2 phase5/020):
  `[{"at": seconds, "channel": "...", "text": "raw signal message"}]`.

Consumed by `backend/src/services/broker/fake_bridge.py`
(`FakeMT5Bridge(scenario=json["market"])`). Deterministic on purpose — same
file, same outcome, so an e2e test can assert an exact TP hit.
