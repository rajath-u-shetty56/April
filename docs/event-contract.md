# Event Contract

The current event envelope follows a flexible PostHog-style structure:

```json
{
  "event": "call_completed",
  "distinct_id": "acme",
  "timestamp": "2026-09-08T10:30:00Z",
  "properties": {
    "account_id": "acme",
    "product": "contact_center",
    "version": "1",
    "agent_id": 41,
    "call_id": "C-100"
  }
}
```

## Envelope fields

- `event`: required business-event name;
- `distinct_id`: required subject identifier;
- `timestamp`: optional event occurrence time;
- `properties`: optional JSON object containing product-specific fields.

Unknown properties are accepted. The event catalog does not publish strict property schemas or validate the optional producer `version`.

HappyFox-specific identity and product conventions live inside `properties`; they are not hard-coded database columns.
