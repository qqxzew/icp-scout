"""The one door every agent calls through - one client, many prompts.

Four jobs, and each exists for a specific reason found while building
the rest of the pipeline, not by default:

1. KEY AND MODEL IN ONE PLACE. `.env` is read by hand - five lines of
   stdlib, no dependency - per the project's own rule (CLAUDE.md 14):
   only stdlib until a dependency earns its place, and reading
   KEY=VALUE lines does not. Default model is one constant, so changing
   it is a one-line edit, not a grep across every prompt file.

2. STRUCTURED OUTPUT, ALWAYS. The log already observed that a model
   will quote verbatim without being asked, but does so at its own
   discretion - so the schema is enforced by the API
   (`response_format: json_schema`, `strict: true`), not requested in
   prose. Every prompt in llm/prompts/ must produce
   `{"value": ..., "quote": ...}` pairs, because that is exactly the
   shape verify.py's check_many() expects and nothing else is checkable.

3. CACHE BY CONTENT HASH. Key is (model, prompt name + version, sha256
   of the rendered input). A weekly run over an unchanged page then
   costs nothing and returns the same answer it returned last week -
   which is also what makes a run reproducible, not just cheap.
   Storage is a flat directory of files, not a database: lookups here
   are always an exact key match, never a query, so the two-file
   pattern archive.py needs (index + blobs) buys nothing here.

4. COST LOGGED, NOT GUESSED. Every call - cached or not - appends one
   line to data/llm_usage.jsonl: model, prompt, tokens, price, whether
   it was served from cache or the Batch API. "How much does a weekly
   run cost" is then a number read off a file, not an estimate.

Batch is the default path for anything that is not interactive
debugging: the run is weekly, nothing about it is urgent, and the
discount is real (roughly half of synchronous, more once cache hits are
subtracted). complete() stays available for prompt development, where
an answer in ten seconds matters more than an answer at half price.

Pricing (gpt-4.1-mini, checked 2026-08-28, USD per 1M tokens):
    sync              $0.40 in  / $1.60 out
    batch             $0.20 in  / $0.80 out
    cached input      $0.10 (75% off, sync only - batch does not cache)

Run:
    python -m pipeline.llm.client --ping
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_MODEL = "gpt-4.1-mini"
ENV_PATH = Path(".env")
CACHE_DIR = Path("data/llm_cache")
USAGE_LOG = Path("data/llm_usage.jsonl")

# USD per 1M tokens. Two price lists because batch and sync are
# genuinely different endpoints with different economics, not a
# discount flag on one number.
PRICES = {
    "gpt-4.1-mini": {
        "sync":  {"input": 0.40, "output": 1.60, "cached_input": 0.10},
        "batch": {"input": 0.20, "output": 0.80},
    },
}


# ---------------------------------------------------------------------------
# .env - five lines, not a dependency
# ---------------------------------------------------------------------------


def load_env(path=ENV_PATH):
    """Parse KEY=VALUE lines into os.environ, overriding whatever is there.

    .env wins over the ambient environment on purpose - the opposite of
    the usual dotenv default. Found the hard way: a Windows user-level
    OPENAI_API_KEY sat in the shell environment, invisible in this
    project's .env, and a first setdefault()-based version silently kept
    using that system-wide key instead of the project's own - two
    different keys, no error, no way to tell which one a call actually
    used. A per-project secret has to come from the per-project file,
    every time, or "which key did this call bill" becomes unanswerable.
    """
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ[key] = value


load_env()


# ---------------------------------------------------------------------------
# Cache - flat files keyed by exact hash, no index needed
# ---------------------------------------------------------------------------


def cache_key(model, prompt_name, prompt_version, rendered_input):
    payload = f"{model}|{prompt_name}|{prompt_version}|{rendered_input}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_get(key):
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cache_put(key, payload):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Cost logging
# ---------------------------------------------------------------------------


def price_for(model, usage, mode, cached=False):
    table = PRICES.get(model, {}).get(mode)
    if not table:
        return None
    input_rate = table["cached_input"] if (cached and "cached_input" in table) else table["input"]
    cost = (usage["prompt_tokens"] / 1_000_000 * input_rate
            + usage["completion_tokens"] / 1_000_000 * table["output"])
    return round(cost, 6)


def log_usage(model, prompt_name, mode, usage, cost, cached, from_cache):
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "prompt": prompt_name, "mode": mode,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cost_usd": cost, "cache_hit": from_cache, "cached_input": cached,
    }
    with open(USAGE_LOG, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLM:
    """Thin wrapper over the OpenAI SDK - every agent goes through this."""

    def __init__(self, model=DEFAULT_MODEL, api_key=None):
        from openai import OpenAI
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY not set - put it in .env (see .env.example)"
            )
        self.model = model
        self.client = OpenAI(api_key=key)

    # -- synchronous: prompt development, single lookups --------------

    def complete(self, prompt_name, prompt_version, system, user, schema,
                 use_cache=True):
        """One synchronous call, structured output, cached by content hash.

        `schema` is a JSON Schema dict; the API enforces it directly, so
        a malformed response is a request-level error, not something
        this code has to detect after the fact.
        """
        key = cache_key(self.model, prompt_name, prompt_version, system + "\n" + user)
        if use_cache:
            hit = cache_get(key)
            if hit is not None:
                log_usage(self.model, prompt_name, "sync", hit["usage"], 0.0,
                          cached=False, from_cache=True)
                return hit["result"]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": prompt_name, "schema": schema, "strict": True},
            },
        )
        result = json.loads(response.choices[0].message.content)
        usage = {"prompt_tokens": response.usage.prompt_tokens,
                  "completion_tokens": response.usage.completion_tokens}
        cached_tokens = getattr(getattr(response.usage, "prompt_tokens_details", None),
                                "cached_tokens", 0) or 0
        cost = price_for(self.model, usage, "sync", cached=cached_tokens > 0)
        log_usage(self.model, prompt_name, "sync", usage, cost,
                  cached=cached_tokens > 0, from_cache=False)

        if use_cache:
            cache_put(key, {"result": result, "usage": usage})
        return result

    # -- batch: the weekly run -----------------------------------------

    def batch_submit(self, prompt_name, prompt_version, requests):
        """Submit many (custom_id, system, user, schema) requests as one batch.

        `requests` items: dicts with keys custom_id, system, user, schema.
        Returns the batch id to poll. Costs half of complete() per token
        and has no per-item network round trip - the right default for
        anything that is not interactive.
        """
        lines = []
        for item in requests:
            lines.append(json.dumps({
                "custom_id": item["custom_id"],
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": self.model,
                    "messages": [{"role": "system", "content": item["system"]},
                                 {"role": "user", "content": item["user"]}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": prompt_name, "schema": item["schema"],
                                        "strict": True},
                    },
                },
            }, ensure_ascii=False))

        batch_input = "\n".join(lines).encode("utf-8")
        uploaded = self.client.files.create(
            file=(f"{prompt_name}_{date.today().isoformat()}.jsonl", batch_input),
            purpose="batch",
        )
        batch = self.client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            metadata={"prompt": prompt_name, "version": str(prompt_version)},
        )
        return batch.id

    def batch_status(self, batch_id):
        return self.client.batches.retrieve(batch_id)

    def batch_wait(self, batch_id, poll_seconds=30, timeout_seconds=24 * 3600):
        """Block until a batch finishes. Only for scripts, never for a UI path."""
        start = time.monotonic()
        while True:
            batch = self.batch_status(batch_id)
            if batch.status in ("completed", "failed", "expired", "cancelled"):
                return batch
            if time.monotonic() - start > timeout_seconds:
                raise TimeoutError(f"batch {batch_id} still {batch.status} after timeout")
            time.sleep(poll_seconds)

    def batch_results(self, batch, prompt_name):
        """custom_id -> parsed result dict, for a completed batch.

        Cost is logged per line here rather than once for the whole
        batch, so data/llm_usage.jsonl stays one row per unit of work
        regardless of whether it went through complete() or a batch -
        the file answers "cost so far" the same way either path.
        """
        if not batch.output_file_id:
            return {}
        content = self.client.files.content(batch.output_file_id).text
        out = {}
        for line in content.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            custom_id = row["custom_id"]
            body = row.get("response", {}).get("body", {})
            usage = {"prompt_tokens": body.get("usage", {}).get("prompt_tokens", 0),
                     "completion_tokens": body.get("usage", {}).get("completion_tokens", 0)}
            cost = price_for(self.model, usage, "batch")
            log_usage(self.model, prompt_name, "batch", usage, cost,
                      cached=False, from_cache=False)
            try:
                out[custom_id] = json.loads(body["choices"][0]["message"]["content"])
            except (KeyError, IndexError, json.JSONDecodeError):
                out[custom_id] = None
        return out


def usage_summary(path=USAGE_LOG):
    """Total spend and call count from the log - the number for the defence."""
    if not Path(path).exists():
        return {"calls": 0, "cost_usd": 0.0}
    calls = cost = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            calls += 1
            cost += row.get("cost_usd") or 0.0
    return {"calls": calls, "cost_usd": round(cost, 4)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM client hub.")
    parser.add_argument("--ping", action="store_true", help="one cheap call to check the key works")
    parser.add_argument("--usage", action="store_true", help="print total spend so far")
    args = parser.parse_args()

    if args.usage:
        print(json.dumps(usage_summary(), indent=2))
    elif args.ping:
        llm = LLM()
        result = llm.complete(
            "ping", "1", "Reply with exactly the requested field.",
            "Say hello.",
            {"type": "object", "properties": {"reply": {"type": "string"}},
             "required": ["reply"], "additionalProperties": False},
        )
        print(result)
        print(usage_summary())
    else:
        parser.error("nothing to do - pass --ping or --usage")
