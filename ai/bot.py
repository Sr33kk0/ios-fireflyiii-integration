"""Enrich Apple Pay transactions in Firefly III with a local Ollama model; review flags on Discord.

Firefly webhook (STORE_TRANSACTION) -> classify with Ollama -> PUT back to Firefly -> ping Discord if unsure.
Replying to a ping corrects the transaction. See docs/superpowers/specs/2026-10-10-applepay-firefly-ai-design.md.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from string import Template

import aiohttp
import discord
from aiohttp import web

env = os.environ.get
FIREFLY_URL = env("FIREFLY_URL", "http://app:8080")
FIREFLY_PUBLIC_URL = env("FIREFLY_PUBLIC_URL", "http://localhost")
FIREFLY_TOKEN = env("FIREFLY_TOKEN", "")
WEBHOOK_SECRET = env("WEBHOOK_SECRET", "")
OLLAMA_URL = env("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen3:8b")
CONFIDENCE_MIN = float(env("CONFIDENCE_MIN", "0.7"))
DISCORD_TOKEN = env("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = int(env("DISCORD_CHANNEL_ID") or 0)
DISCORD_USER_ID = int(env("DISCORD_USER_ID") or 0)

TAG = "applepay"
PENDING = "Apple Pay pending"  # placeholder expense account the Shortcut books to
NO_BUDGET = "(none)"
CACHE_SECONDS = 3600
CLASSIFY, CORRECT = Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8").split("\n=== correct ===\n")
log = logging.getLogger("ai")


# ---------- pure logic (tested in test_bot.py) ----------

def verify_signature(secret, header, body):
    """Firefly signs `<t>.<body>` with HMAC-SHA3-256; header is `t=<t>,v1=<hex>`."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if not secret or "t" not in parts or "v1" not in parts:
        return False
    expected = hmac.new(secret.encode(), parts["t"].encode() + b"." + body, hashlib.sha3_256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def norm(s):
    return " ".join((s or "").replace("’", "'").casefold().split())


def match_name(name, names):
    """Existing spelling of `name` among `names`, ignoring case, spacing and curly apostrophes."""
    n = norm(name)
    return next((x for x in names if norm(x) == n), None) if n else None


def parse_raw(tx):
    m = re.search(r"raw: (.*?) \| card: ", tx.get("notes") or "")
    return m.group(1) if m else tx["description"]


def is_unprocessed(tx):
    return TAG in (tx.get("tags") or []) and not tx.get("category_name")


def when(tx, fmt="%a %H:%M"):
    # ponytail: uses the offset Firefly returns (its TZ), no tz conversion needed
    return datetime.fromisoformat(tx["date"]).strftime(fmt)


def money(tx):
    return f"{tx.get('currency_symbol') or ''}{abs(float(tx['amount'])):.2f}"


def meal_for(hour):
    # ponytail: fixed boundaries from this user's history; tune here if meal times shift
    return "Breakfast" if 5 <= hour < 11 else "Lunch" if 11 <= hour < 17 else "Dinner"


def kind(label):
    """Breakfast/Lunch/Dinner are one kind: the hour decides which."""
    return "Meal" if norm(label) in ("meal", "breakfast", "lunch", "dinner") else label


def build_context(merchants, categories, budgets, splits):
    """History -> label map (most common category/budget per description), each merchant's dominant
    label kind (>= 80% of its transactions), counts, few-shot examples."""
    done = [t for t in splits if not is_unprocessed(t)]
    combos, by_merchant = defaultdict(Counter), defaultdict(Counter)
    for t in done:
        combos[t["description"]][(t.get("category_name"), t.get("budget_name"))] += 1
        by_merchant[t["destination_name"]][kind(t["description"])] += 1
    merchant_kind = {m: c.most_common(1)[0][0] for m, c in by_merchant.items()
                     if c.most_common(1)[0][1] >= 0.8 * sum(c.values())}
    recent = sorted(done, key=lambda t: t["date"], reverse=True)[:30]
    examples = [f"{when(t)} {money(t)} -> {t['description']} @ {t['destination_name']}"
                + (f' (raw "{parse_raw(t)}")' if TAG in (t.get("tags") or []) else "") for t in recent]
    return {
        "merchants": [m for m in merchants if m != PENDING],
        "categories": categories,
        "budgets": budgets + [NO_BUDGET],
        "label_map": {label: c.most_common(1)[0][0] for label, c in combos.items()},
        "counts": {label: sum(c.values()) for label, c in combos.items()},
        "merchant_kind": merchant_kind,
        "seen": set(by_merchant),  # merchants with withdrawal history
        "examples": examples,
    }


def fields(description, merchant, category, budget):
    return {"description": description, "destination_name": merchant, "category_name": category,
            "budget_name": None if budget in (None, NO_BUDGET) else budget}


def finalize(result, ctx, hour):
    """Model output -> (Firefly fields, flag reasons).
    History beats the model: a known merchant keeps its usual label, meals are named by the hour,
    and known labels take category/budget from history."""
    if not result["description"].strip() or not result["merchant"].strip():
        raise ValueError(f"empty description or merchant: {result}")
    reasons = []
    merchant = match_name(result["merchant"], ctx["merchants"]) or result["merchant"].strip()
    if merchant not in ctx["seen"]:  # also an existing expense account with no history: nothing to check against
        reasons.append("new merchant")
    desc = kind(ctx["merchant_kind"].get(merchant) or result["description"].strip())
    if desc == "Meal":
        desc = meal_for(hour)
    label = match_name(desc, ctx["label_map"])
    if label:
        category, budget = ctx["label_map"][label]
    else:
        label, category, budget = desc, result["category"], result["budget"]
        reasons.append("new label")
    if category is not None and category not in ctx["categories"]:
        category = None
        reasons.append("unknown category")
    confidence = result["confidence"] / 100 if result["confidence"] > 1 else result["confidence"]
    if confidence < CONFIDENCE_MIN:
        reasons.append("low confidence")
    return fields(label, merchant, category, budget), reasons


def apply_correction(tx, patch, ctx):
    """Merge a reply-derived patch into the transaction's current values -> Firefly fields."""
    current = {"description": tx["description"], "merchant": tx["destination_name"],
               "category": tx.get("category_name"), "budget": tx.get("budget_name") or NO_BUDGET}
    # the model often echoes unchanged values; drop them so they don't block the label map
    patch = {k: v for k, v in patch.items() if k in current and v and norm(v) != norm(current[k])}
    desc = patch.get("description", current["description"])
    label = match_name(desc, ctx["label_map"]) or desc.strip()
    merchant = patch.get("merchant", current["merchant"])
    merchant = match_name(merchant, ctx["merchants"]) or merchant.strip()
    category, budget = current["category"], current["budget"]
    if "description" in patch and label in ctx["label_map"]:
        category, budget = ctx["label_map"][label]
    category = patch.get("category", category)
    budget = patch.get("budget", budget)
    return fields(label, merchant, category, budget)


def summary(group_id, tx, f, raw):
    lines = [f"{money(tx)} · {when(tx)} · {tx['source_name']}"]
    if f:
        lines.append(f"{f['description']} @ {f['destination_name']} ({f['category_name'] or '-'} / {f['budget_name'] or '-'})")
    lines += [f'raw: "{raw}"', f"{FIREFLY_PUBLIC_URL}/transactions/show/{group_id}"]
    return "\n".join(lines)


def txn_id(text):
    m = re.search(r"/transactions/show/(\d+)", text or "")
    return int(m.group(1)) if m else None


def labels_text(ctx):
    top = sorted(ctx["counts"], key=ctx["counts"].get, reverse=True)[:40]
    return "\n".join(f"- {l} -> {ctx['label_map'][l][0] or '-'} / {ctx['label_map'][l][1] or NO_BUDGET} "
                     f"({ctx['counts'][l]}x)" for l in top) or "(none yet)"


def classify_prompt(tx, raw, ctx):
    return Template(CLASSIFY).safe_substitute(
        raw=raw, amount=money(tx), when=when(tx, "%A %H:%M"), account=tx["source_name"],
        labels=labels_text(ctx), merchants=", ".join(sorted(ctx["merchants"])),
        examples="\n".join(ctx["examples"]) or "(none yet)")


def correct_prompt(tx, raw, reply, ctx):
    return Template(CORRECT).safe_substitute(
        description=tx["description"], merchant=tx["destination_name"], category=tx.get("category_name") or "-",
        budget=tx.get("budget_name") or NO_BUDGET, raw=raw, reply=reply,
        labels=labels_text(ctx), merchants=", ".join(sorted(ctx["merchants"])))


def schema(ctx, correction=False):
    props = {"description": {"type": "string"}, "merchant": {"type": "string"},
             "category": {"type": "string", "enum": ctx["categories"]},
             "budget": {"type": "string", "enum": ctx["budgets"]}}
    if correction:
        return {"type": "object", "properties": props}
    # `shop` first: naming what the business sells before labelling stops "every tap at 19:00 is Dinner"
    props = {"shop": {"type": "string"}, "merchant": props["merchant"], "description": props["description"],
             "category": props["category"], "budget": props["budget"], "confidence": {"type": "number"}}
    return {"type": "object", "properties": props, "required": list(props)}


# ---------- I/O ----------

class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.ctx, self.ctx_at, self.tasks = None, 0.0, set()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def setup_hook(self):
        self.web = aiohttp.ClientSession()  # not self.http: discord.Client owns that name
        app = web.Application()
        app.router.add_post("/hook", self.hook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8000).start()
        self.spawn(self.sweep())

    # --- Firefly ---

    async def ff(self, method, path, **kw):
        headers = {"Authorization": f"Bearer {FIREFLY_TOKEN}", "Accept": "application/json"}
        async with self.web.request(method, f"{FIREFLY_URL}/api/v1{path}", headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30), **kw) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Firefly {r.status}: {text[:300]}")
            return json.loads(text)

    async def ff_all(self, path, params):
        out, page = [], 1
        while True:
            d = await self.ff("GET", path, params={**params, "limit": 100, "page": page})
            out += d["data"]
            if page >= d["meta"]["pagination"]["total_pages"]:
                return out
            page += 1

    async def context(self):
        if self.ctx and time.monotonic() - self.ctx_at < CACHE_SECONDS:
            return self.ctx
        names = lambda items: [i["attributes"]["name"] for i in items]
        merchants = names(await self.ff_all("/accounts", {"type": "expense"}))
        categories = names(await self.ff_all("/categories", {}))
        budgets = names([b for b in await self.ff_all("/budgets", {}) if b["attributes"].get("active", True)])
        groups = await self.ff_all("/transactions", {"type": "withdrawal"})
        splits = [t for g in groups for t in g["attributes"]["transactions"]]
        self.ctx, self.ctx_at = build_context(merchants, categories, budgets, splits), time.monotonic()
        return self.ctx

    async def update(self, group_id, journal_id, f):
        await self.ff("PUT", f"/transactions/{group_id}", json={
            "apply_rules": False, "fire_webhooks": False,  # no rules on top, no webhook loop
            "transactions": [{"transaction_journal_id": journal_id, **f}]})
        self.ctx = None  # new merchant/label/example is now part of history

    # --- Ollama ---

    async def ollama(self, prompt, fmt):
        body = {"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False,
                "think": False, "format": fmt, "keep_alive": "30m", "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 300}}
        async with self.web.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 400:
                raise RuntimeError(f"Ollama {r.status}: {(await r.text())[:300]}")
            return json.loads((await r.json())["message"]["content"])

    # --- Discord ---

    async def post(self, text):
        try:
            await asyncio.wait_for(self.wait_until_ready(), 60)
            await self.get_channel(DISCORD_CHANNEL_ID).send(text[:2000])
        except Exception:
            log.exception("discord post failed:\n%s", text)

    # --- flows ---

    async def hook(self, request):
        body = await request.read()
        if not verify_signature(WEBHOOK_SECRET, request.headers.get("Signature", ""), body):
            log.warning("rejected webhook: bad signature")
            return web.Response(status=401)
        group = json.loads(body)["content"]
        txs = group["transactions"]
        # single split only: a PUT with one split would drop the others
        if len(txs) == 1 and TAG in (txs[0].get("tags") or []):
            self.spawn(self.enrich(group["id"], txs[0]))
        return web.Response(text="ok")  # answer now; Firefly gives up after 10s

    async def enrich(self, group_id, tx):
        raw = parse_raw(tx)
        try:
            ctx = await self.context()
            result = await self.ollama(classify_prompt(tx, raw, ctx), schema(ctx))
            f, reasons = finalize(result, ctx, datetime.fromisoformat(tx["date"]).hour)
        except Exception as e:
            log.exception("classify #%s failed", group_id)
            return await self.post(f"<@{DISCORD_USER_ID}> ❌ couldn't classify ({str(e)[:200]}), reply to fix\n"
                                   + summary(group_id, tx, None, raw))
        try:
            await self.update(group_id, tx["transaction_journal_id"], f)
        except Exception as e:
            log.exception("update #%s failed", group_id)
            return await self.post(f"<@{DISCORD_USER_ID}> ❌ failed to update #{group_id}: {str(e)[:300]}\n"
                                   + summary(group_id, tx, f, raw))
        log.info("#%s %r -> %s %s", group_id, raw, f, reasons)
        if reasons:
            conf = result["confidence"] / 100 if result["confidence"] > 1 else result["confidence"]
            await self.post(f"<@{DISCORD_USER_ID}> ⚠️ {', '.join(reasons)} · {conf:.0%}\n" + summary(group_id, tx, f, raw))

    async def sweep(self):
        """Catch Apple Pay transactions that arrived while this service was down."""
        since = (date.today() - timedelta(days=7)).isoformat()
        for _ in range(20):  # after a reboot Firefly may still be migrating; give it ~5 min
            try:
                groups = await self.ff_all("/search/transactions",
                                           {"query": f"tag_is:{TAG} has_no_category:true date_after:{since}"})
                break
            except Exception as e:
                log.warning("startup sweep: Firefly not ready (%s), retrying in 15s", e)
                await asyncio.sleep(15)
        else:
            return log.error("startup sweep gave up")
        log.info("startup sweep: %d unprocessed", len(groups))
        for g in groups:
            txs = g["attributes"]["transactions"]
            if len(txs) == 1:
                await self.enrich(int(g["id"]), txs[0])

    async def on_message(self, msg):
        ref = msg.reference
        if msg.author.id != DISCORD_USER_ID or msg.channel.id != DISCORD_CHANNEL_ID or not ref or not ref.message_id:
            return
        target = ref.resolved if isinstance(ref.resolved, discord.Message) else await msg.channel.fetch_message(ref.message_id)
        group_id = txn_id(target.content)
        if target.author.id != self.user.id or not group_id:
            return
        try:
            tx = (await self.ff("GET", f"/transactions/{group_id}"))["data"]["attributes"]["transactions"][0]
            raw, ctx = parse_raw(tx), await self.context()
            patch = await self.ollama(correct_prompt(tx, raw, msg.content, ctx), schema(ctx, correction=True))
            f = apply_correction(tx, patch, ctx)
            await self.update(group_id, tx["transaction_journal_id"], f)
            await target.edit(content="✅ fixed\n" + summary(group_id, tx, f, raw))
            await msg.add_reaction("✅")
            log.info("#%s corrected by reply %r -> %s", group_id, msg.content, f)
        except Exception as e:
            log.exception("correction #%s failed", group_id)
            await msg.reply(f"❌ {str(e)[:300]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Bot().run(DISCORD_TOKEN, log_handler=None)
