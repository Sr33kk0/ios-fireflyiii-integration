# Apple Pay → Firefly III, labelled by local AI

Tap Apple Pay → iOS Shortcut books a raw withdrawal in Firefly → Firefly webhook → `ai` asks Ollama for
label + merchant → transaction updated. Unsure or new? You get a Discord ping; reply to it to fix.
Design: `docs/superpowers/specs/2026-10-10-applepay-firefly-ai-design.md`.

## One-time setup

### 1. Discord bot

1. <https://discord.com/developers/applications> → **New Application** → **Bot**:
   **Reset Token** → copy into `.ai.env` as `DISCORD_TOKEN`; enable **Message Content Intent**.
2. **OAuth2 → URL Generator**: scope `bot`; permissions View Channels, Send Messages,
   Read Message History, Add Reactions. Open the URL, add the bot to your server.
3. Create a `#firefly` channel. Discord settings → Advanced → **Developer Mode** on, then right-click the
   channel → Copy Channel ID → `DISCORD_CHANNEL_ID`; right-click yourself → Copy User ID → `DISCORD_USER_ID`.

### 2. Firefly

1. <http://localhost/admin/configuration> → tick **Allow webhooks** → save.
2. <http://localhost/profile> → OAuth → **Personal Access Tokens**: create `ai` (→ `.ai.env`
   `FIREFLY_TOKEN`) and `iphone` (for the Shortcut). Two tokens so revoking the phone never breaks `ai`.
3. Start everything (also applies `TZ` from `.env`): `docker compose up -d --build`
4. Create the webhook (the `ai` container must be running: Firefly resolves the URL when saving).
   Either Automation → Webhooks in the UI (trigger *after transaction creation*, response *transaction
   details*, delivery JSON, URL `http://ai:8000/hook`) or:

   ```sh
   curl -s http://localhost/api/v1/webhooks -H "Authorization: Bearer $AI_TOKEN" \
     -H "Accept: application/json" -H "Content-Type: application/json" \
     -d '{"title":"Apple Pay AI","active":true,"triggers":["STORE_TRANSACTION"],
          "responses":["TRANSACTIONS"],"deliveries":["JSON"],"url":"http://ai:8000/hook"}'
   ```

   Copy the webhook's **secret** into `.ai.env` as `WEBHOOK_SECRET`, then `docker compose up -d ai`.

### 3. iPhone Shortcut

Phone must be on Tailscale (it reaches Firefly at `<firefly-host>`).
Shortcuts → Automation → **+** → **Transaction** → cards *My Visa*, *My Debit Card* → **Run Immediately**
→ New Blank Automation, actions:

1. **Dictionary**: `My Visa` → `Main Account`, `My Debit Card` → `Savings Account`
2. **Get Dictionary Value** for key *Shortcut Input → Card or Pass*
3. **Replace Text**: in *Shortcut Input → Amount*, regex `[^0-9.]` → empty (turns `$1,299.00` into `1299.00`)
4. **Format Date**: *Current Date*, ISO 8601, include time
5. **Text** (insert the variables where shown):

   ```json
   {"error_if_duplicate_hash": false, "apply_rules": false, "transactions": [{
     "type": "withdrawal", "date": "[Formatted Date]", "amount": "[Updated Text]",
     "description": "[Merchant]", "source_name": "[Dictionary Value]",
     "destination_name": "Apple Pay pending", "tags": ["applepay"],
     "notes": "raw: [Merchant] | card: [Card or Pass]"}]}
   ```

6. **Get Contents of URL**: `http://<firefly-host>/api/v1/transactions`, method POST, headers
   `Authorization: Bearer <iphone token>`, `Accept: application/json`, `Content-Type: application/json`,
   request body **File** → the Text from step 5.

## Test without a tap

```sh
curl -s http://localhost/api/v1/transactions -H "Authorization: Bearer $AI_TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{"apply_rules":false,"transactions":[{"type":"withdrawal","date":"2026-10-10T21:30:00+00:00",
       "amount":"16.00","description":"SWEET SCOOP LTD","source_name":"Main Account",
       "destination_name":"Apple Pay pending","tags":["applepay"],
       "notes":"raw: SWEET SCOOP LTD | card: My Visa"}]}'
docker logs -f firefly_iii_ai
```

Expect a Discord ping (new merchant). Reply `dessert` → message flips to ✅ fixed.

## Day to day

- Fix: reply to a ping in plain words: `dessert`, `drinks at Starbucks`, `merchant Sweet Scoop, budget (none)`.
  Corrections become history: next time that merchant gets the same label without asking.
- Meals are named by hour, not by the model: Breakfast 05–11, Lunch 11–17, Dinner otherwise (`meal_for` in `bot.py`).
- Tune wording in `prompt.txt`, threshold with `CONFIDENCE_MIN`, model with `OLLAMA_MODEL`; then
  `docker compose up -d --build ai`.
- Checks: `docker compose run --rm --no-deps ai python test_bot.py`
- Only taps in shops trigger the automation; online / in-app Apple Pay usually doesn't.
