#!/usr/bin/env python3
"""Learn how to handle model failures from magpie's usage log and feed the
learned actions to the magpie-route-rules gateway middleware.

Two learning channels:
1. Image capability (0.2.0; native-vs-describe since 0.3.44): a model
   whose image request fails (406 / multimodal-flavoured errors) is marked
   bad for a TTL in options.learned.images — later image requests skip it.
   The TTL is the half-open probe: when it expires, traffic flows again and
   real outcomes re-teach the mark. A plain 200 proves nothing, though:
   magpie silently describes images for vision-less models via the Image
   recognition pool, and the describe fallback writes each described image
   into vision-cache.jsonl. Probes send a fresh random image per run; a new
   cache entry (or a 502 naming the model in "can't see images") means the
   model saw a description, not the image → mark via='describe' (natively
   vision-less, re-verified weekly, never preferred for image routing).
2. Failure actions (0.3.0): every failure signature (model + status +
   normalised error) gets a handling action in options.learned.actions:
     {model, kind: 'pause'|'clampOutput'|'ignore', cap?, ttlMs, at, by}
   Rules classify the common cases deterministically (499 client cancel →
   ignore; "expected a value <= N" → clampOutput; 429/5xx → pause). Unknown
   signatures go to an AI classifier through magpie itself, whose verdict is
   cached in the same list.

Adaptive actions (0.4) — actions are never fixed, they keep adjusting so the
impact on usage stays minimal:
  * pause uses exponential backoff: 2min for the first failure, doubling per
    consecutive failure (cap 1h), tracked in the state file's `fails` map;
  * any 200 for a model clears its fresh pause immediately and resets the
    backoff counter — recovery is adopted the moment real traffic proves it;
  * a new "expected a value <= N" on an already-clamped model updates the
    existing clampOutput in place (cap = the error's stated ceiling), never
    stacks duplicates; clamps only ever move to the authoritative ceiling, no
    speculative raising;
  * the companion rewrites/removes actions in plugins.json on every pass, so
    the middleware always executes the current belief, and every change is
    backed up and atomic.
The middleware (route-rules 0.3.0+) just executes whatever the learner
currently believes — enforcement reads fresh actions dynamically.

Usage:
  python route-learn.py [--once] [--dry-run] [--quiet] [--no-ai]
      [--usage <usage.jsonl>] [--plugins <plugins.json>] [--state <state.json>]
      [--ttl <ms>] [--status 406] [--keywords multimodal,image,modality]
      [--ai-model <model|*>] [--ai-cap 3] [--clamp-skip-providers cannbot]
      [--pause-base <ms>] [--pause-max <ms>]
  python route-learn.py --classify "<error text>" [--status 502]   # AI verdict

Exit codes: 0 ok, 1 error, 2 AI classifier unavailable.
"""
import argparse
import base64
import binascii
import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from collections import deque

DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "magpie")
DEFAULT_USAGE = os.path.join(DEFAULT_CONFIG_DIR, "usage.jsonl")
DEFAULT_PLUGINS = os.path.join(DEFAULT_CONFIG_DIR, "plugins.json")
DEFAULT_STATE = os.path.join(DEFAULT_CONFIG_DIR, "route-learn-state.json")
DEFAULT_BACKUPS = os.path.join(DEFAULT_CONFIG_DIR, "route-learn", "backups")
DEFAULT_LOG = os.path.join(DEFAULT_CONFIG_DIR, "route-learn", "route-learn.log")
DEFAULT_TTL_MS = 6 * 3600 * 1000
DEFAULT_STATUS = "406"
DEFAULT_KEYWORDS = "multimodal,image,modality"
PLUGIN_SPEC_FRAGMENT = "route-rules"
KEEP_BACKUPS = 5
USER_AGENT = "magpie-route-learn/0.2"
GATEWAY = "http://127.0.0.1:3425/v1/chat/completions"
GATEWAY_MODELS = "http://127.0.0.1:3425/v1/models"
VISION_CACHE = os.path.join(DEFAULT_CONFIG_DIR, "vision-cache.jsonl")
DSH_ENV = os.path.join(os.path.expanduser("~"), ".dsh", ".env")
DEFAULT_PAUSE_BASE = 2 * 60 * 1000
DEFAULT_PAUSE_MAX = 60 * 60 * 1000
PAUSE_TTL_MS = 10 * 60 * 1000
IGNORE_TTL_MS = 30 * 24 * 3600 * 1000
REQCFG_TTL_MS = 24 * 3600 * 1000  # 请求侧问题（输入超长/空回复预算）的签名记忆：短 TTL 保证误判可自愈
CLAMP_TTL_MS = 24 * 3600 * 1000
SUCCESS_KEEP = 20
DESCRIBE_REPROBE_MS = 7 * 24 * 3600 * 1000  # via='describe' marks: re-verify weekly


def log(msg, quiet):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if not quiet:
        print(line)
    try:
        os.makedirs(os.path.dirname(DEFAULT_LOG), exist_ok=True)
        with open(DEFAULT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def backup(path):
    os.makedirs(DEFAULT_BACKUPS, exist_ok=True)
    dst = os.path.join(DEFAULT_BACKUPS, "%s.%s.bak" % (os.path.basename(path), time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(path, dst)
    olds = sorted(f for f in os.listdir(DEFAULT_BACKUPS) if os.path.basename(path) + "." in f)
    for old in olds[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(DEFAULT_BACKUPS, old))
        except OSError:
            pass


def gateway_key():
    key = os.environ.get("MAGPIE_GATEWAY_KEY")
    if key:
        return key
    try:
        with open(DSH_ENV, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("MAGPIE_GATEWAY_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def normalize_err(text):
    norm = re.sub(r"\d+", "N", str(text or "").lower())
    return re.sub(r"\s+", " ", norm)[:200]


def signature(model_key, row):
    raw = "%s|%s|%s" % (model_key, row.get("status"), normalize_err(row.get("err")))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def rule_classify(row, clamp_skip_providers):
    """Deterministic verdict, or None when the AI classifier should decide."""
    status = row.get("status")
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    err = str(row.get("err") or "").lower()
    provider = str(row.get("provider") or "")
    # 499: the client canceled — normal, nothing to handle.
    if status == 499:
        return {"kind": "ignore", "ttlMs": IGNORE_TTL_MS}
    # 413: payload too large — deterministic content failure. The same body
    # always fails; pausing would wrongly block small requests too, and
    # clamping output is irrelevant. The error propagates so the caller can
    # shrink the input (routing can't fix a shared gateway size limit).
    if status == 413 or "payload too large" in err or "request entity too large" in err or "request too large" in err:
        return {"kind": "ignore", "ttlMs": IGNORE_TTL_MS}
    # Output budget over the model's real ceiling: the provider plugin for
    # clamp-skip-providers (e.g. cannbot) already clamps and switches along
    # its chain — clamping here would defeat that switch.
    m = re.search(r"expected a value\s*<=\s*(\d+)", err)
    if m and provider not in clamp_skip_providers:
        return {"kind": "clampOutput", "cap": int(m.group(1)), "ttlMs": CLAMP_TTL_MS}
    if m:
        return {"kind": "ignore", "ttlMs": CLAMP_TTL_MS}
    # 输入长度超限 ≠ 输出钳制（另一机器误触发实证：错误说 input 超过模型上限
    # 344064，被当成 clampOutput——钳 max_tokens 治不了输入超长）。输入超限
    # 归 payload 学习（payloadCaps）与用户自行缩减，学习侧只 ignore。
    # 模式容错拼写变体（exc[ce]ed）与 max/maximum 变体——正则多容错无成本。
    if re.search(r"input too long|exc[ce]ed max input|max input length|prompt is too long|context length|input length.{0,60}\d{4,}", err):
        return {"kind": "ignore", "ttlMs": REQCFG_TTL_MS}
    # 空回复且输出全花在 reasoning（"cut at its length ... raise max_tokens"）：
    # 请求预算问题而非模型故障——同一模型在更大预算下服务正常（AI 反思自己的
    # 调用就会踩这个，曾致 deepseek 每轮 18s 502 且反复被 pause）。不 pause。
    if "empty reply" in err and "reasoning" in err:
        return {"kind": "ignore", "ttlMs": REQCFG_TTL_MS}
    # Rate limiting / upstream down: pause the model for a while. The exact
    # duration comes from the exponential backoff (the caller decides).
    if status == 429 or "rate limit" in err or "too many requests" in err:
        return {"kind": "pause", "ttlMs": 0}
    if (isinstance(status, int) and status >= 500) or any(
        kw in err for kw in ("timeout", "timed out", "connection refused", "reset by peer", "broken pipe", "unreachable")
    ):
        return {"kind": "pause", "ttlMs": 0}
    return None


def fetch_available_models(key):
    # /v1/models answers without auth on the local gateway; send the key when
    # we have one anyway.
    headers = {"User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        req = urllib.request.Request(GATEWAY_MODELS, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return ids or None
    except Exception:
        return None


def _ttfb_penalty(pstats):
    """首字节时延惩罚因子：10s TTFB 减半分数（体感 = 吞吐 × 响应及时性）。
    TTFB 样本 <3 不惩罚（数据不足时不臆断——反思建议的样本门槛）。"""
    if (pstats.get("ttfbSamples") or 0) < 3:
        return 1.0
    ttfb = pstats.get("ewmaTtfbMs")
    if not isinstance(ttfb, (int, float)) or ttfb <= 0:
        return 1.0
    return 1.0 / (1.0 + (ttfb / 1000.0) / 10.0)


def perf_score(pstats):
    """体验综合分：吞吐率 ÷ (1+连败) × 首字节惩罚。样本不足返回 None。"""
    if not pstats or (pstats.get("samples") or 0) < 2:
        return None
    cf = pstats.get("consecFails") or 0
    tps = pstats.get("ewmaTps") or 0
    return round(tps / (1 + cf) * _ttfb_penalty(pstats), 1)


def time_bucket(now=None):
    """(周末?, 时段桶0-3)：上游负载有时段规律（晚高峰/工作时段），
    分桶统计让评分贴合'现在这个时间'的真实表现而非全天混合均值。"""
    t = time.localtime(time.time() if now is None else now)
    return (1 if t.tm_wday >= 5 else 0), t.tm_hour // 6


def bucket_key(now_ms=None):
    wk, hb = time_bucket(None if now_ms is None else now_ms / 1000)
    return "%d_%d" % (wk, hb)


BUCKET_NAMES = ["凌晨(0-6)", "上午(6-12)", "下午(12-18)", "晚间(18-24)"]


def bucket_label(bkey):
    """'0_3' → '工作日·晚间(18-24)'——反思/展示用可读桶名。"""
    try:
        wk, hb = str(bkey).split("_")
        return ("周末" if wk == "1" else "工作日") + "·" + BUCKET_NAMES[int(hb)]
    except Exception:
        return str(bkey)


def perf_score_bucket(pstats, bkey):
    """时段桶内评分：样本 ≥3 且有吞吐率数据时优先于全局分（更贴近当下）。
    失败率惩罚：rate>0 直接除 (1+2*rate)——小样本高失败桶（另一机器实证：
    2/3 失败率被旧规则忽略）也能实际压低评分驱动降级。"""
    b = (pstats.get("buckets") or {}).get(bkey)
    if b and (b.get("samples") or 0) >= 3 and b.get("ewmaTps"):
        ttfb = b.get("ewmaTtfbMs")
        # 桶内首字节惩罚同样要样本门槛（与全局 _ttfb_penalty 对齐）：ttfbSamples<3
        # 不惩罚——数据不足不臆断，否则小样本桶的偶发慢首字节会虚假压分。
        pen = 1.0 / (1.0 + (ttfb / 1000.0) / 10.0) if isinstance(ttfb, (int, float)) and ttfb > 0 and (b.get("ttfbSamples") or 0) >= 3 else 1.0
        bf, bs = b.get("fails") or 0, b.get("samples") or 0
        fail_pen = 1.0 / (1.0 + 2.0 * (bf / bs)) if bs > 0 else 1.0
        return round(b["ewmaTps"] / (1 + (b.get("consecFails") or 0)) * pen * fail_pen, 1)
    return None


def perf_score_now(pstats, now_ms=None):
    """时段感知评分：桶内样本足用桶内分，否则退回全局分。"""
    if not pstats:
        return None
    return perf_score_bucket(pstats, bucket_key(now_ms)) or perf_score(pstats)


def unique_probe_png():
    """8x8 random-RGB PNG data URL — fresh content on every probe so magpie's
    vision cache (content-addressed) can never serve a stale description and
    hide whether this request went through the describe fallback."""
    w = h = 8
    rnd = os.urandom(w * h * 3)
    raw = b"".join(b"\x00" + rnd[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def vision_cache_keys():
    """Keys currently in magpie's describe-fallback cache (vision-cache.jsonl).
    Every image magpie describes lands there keyed by content."""
    keys = set()
    try:
        with open(VISION_CACHE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    key = json.loads(line).get("key")
                except ValueError:
                    continue
                if key:
                    keys.add(key)
    except OSError:
        pass
    return keys


def probe_call(key, model, content, timeout=90):
    """One tiny request; (status, body text). status < 0 = network error."""
    payload = {"model": model, "messages": [{"role": "user", "content": content}],
               "max_tokens": 16, "stream": False}
    req = urllib.request.Request(GATEWAY, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read().decode("utf-8", "replace")
        except Exception:
            return err.code, ""
    except Exception:
        return -1, ""


def probe_model(key, model, content="ping", timeout=90):
    """Tiny probe: True = healthy (200). Used for early pause-recovery."""
    status, _ = probe_call(key, model, content, timeout)
    return status == 200


def _usage_rows(limit=24):
    rows = []
    try:
        with open(DEFAULT_USAGE, encoding="utf-8", errors="replace") as fh:
            for line in deque(fh, maxlen=limit):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _usage_epoch(text):
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        return datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def usage_resolved_model(started_at, known, attempts=3):
    """Which model actually served the request we just sent. The middleware
    masks the served name in response bodies, so read the usage tail: probe
    rows carry agent='magpie'; only rows naming a known route candidate are
    accepted, so the describe-call row (a describer model) is skipped."""
    known = set(known or ())
    for _ in range(attempts):
        served = None
        for row in _usage_rows():
            if row.get("agent") != "magpie":
                continue
            at = _usage_epoch(row.get("t"))
            if at is None or at < started_at - 5 or at > time.time() + 2:
                continue
            name = row.get("model")
            if not isinstance(name, str) or not name:
                continue
            for cand in known:
                if name == cand or cand.endswith("/" + name):
                    served = cand
                    break
        if served:
            return served
        time.sleep(0.2)
    return None


def probe_image_capability(key, model, known, timeout=90):
    """Image capability, separating native vision from magpie's describe
    fallback (Settings → Models → Image recognition).

    A plain 200 proves nothing: for a vision-less model magpie silently has a
    describer turn the image into text and serves that (this used to mark such
    models 'good', so image traffic kept flowing into the fallback until the
    describer pool ran dry and every image request 502'd "image not
    described"). The fallback writes each described image into
    vision-cache.jsonl keyed by content, so a fresh random probe image that
    produces a new entry means the model saw a description, not the image.
    When the describer pool is dry magpie names the model ("model X can't see
    images", or the 400 fast-fail "does not support image input") — also a
    direct mark.

    Returns {'ok', 'via': 'native'|'describe'|None, 'who', 'served'}.
    'who' is the model to attribute: the 502 names the real culprit; a 200 is
    attributed to the model the usage log says served it. 'who' may differ
    from the asked model because the middleware rewrites image requests to the
    rule's own candidates.
    """
    before = vision_cache_keys()
    started = time.time() - 1
    content = [{"type": "text", "text": "这张图是什么颜色"},
               {"type": "image_url", "image_url": {"url": unique_probe_png()}}]
    status, body = probe_call(key, model, content, timeout)
    if status != 200:
        named = re.search(r'model\s+\\?"([A-Za-z0-9@._:/-]+)\\?"\s+(?:can\'?t see images|does not support image input)', body or "")
        if named:
            return {"ok": False, "via": "describe", "who": named.group(1), "served": named.group(1)}
        return {"ok": False, "via": None, "who": model, "served": None}
    seen = before
    for _ in range(4):  # the cache write rides the same request; allow a settle
        time.sleep(0.4)
        seen = vision_cache_keys()
        if seen - before:
            break
    described = bool(seen - before)
    served = usage_resolved_model(started, known) or model
    if described:
        return {"ok": True, "via": "describe", "who": served, "served": served}
    if served != model:
        # rewritten to another candidate — this probe says nothing about the
        # asked model's own vision; leave its mark alone.
        return {"ok": True, "via": None, "who": None, "served": served}
    return {"ok": True, "via": "native", "who": model, "served": served}


def image_bad_window(entry, ttl_ms):
    """How long a 'bad' image mark stays meaningful. via='describe' marks
    encode a catalog-level property (no native vision), not a transient
    failure: re-verify them weekly instead of on every TTL."""
    if isinstance(entry, dict) and entry.get("via") == "describe":
        return DESCRIBE_REPROBE_MS
    return ttl_ms


def ai_candidates(args, success, available, perf=None):
    """Ordered classification-model candidates: explicit override first, then
    体验分排序（稳定且快者优先），再按最近成功，最后任意可用。"""
    candidates = []

    def push(model):
        if model and model not in candidates:
            candidates.append(model)

    recent = []
    if isinstance(success, dict):
        recent = sorted(success.items(), key=lambda kv: -(kv[1].get("lastAt") or 0))
        recent = [k for k, v in recent if isinstance(v, dict) and (v.get("lastAt") or 0) > 0]
    if available:
        recent = [m for m in recent if m in available]
    if args.ai_model and args.ai_model != "*":
        if not available or args.ai_model in available:
            push(args.ai_model)
        elif args.ai_model in recent:
            push(args.ai_model)
    # 体验分排序（≥2 采样才有分数），未采样保持原顺序
    scored = []
    unscored = []
    for m in recent:
        s = perf_score(perf.get(m)) if perf else None
        if s is None:
            unscored.append(m)
        else:
            scored.append((m, s))
    scored.sort(key=lambda t: -t[1])
    for m, _ in scored:
        push(m)
    for m in unscored:
        push(m)
    for m in available or []:
        push(m)
    return candidates


def ai_chat(messages, args, candidates, max_tokens=800):
    """Generic gateway chat call, tried across candidates in order.
    Returns (content, used_model, error)."""
    key = gateway_key()
    if not key:
        return None, None, "no gateway key"
    if not candidates:
        return None, None, "no candidates"
    last_err = "no candidates"
    for model in candidates[:3]:
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
        if model.startswith("cannbot/"):
            payload["reasoning_effort"] = "minimal"
        req = urllib.request.Request(GATEWAY, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not content.strip():
                last_err = "%s: 空回复" % model
                continue
            return content.strip(), model, None
        except Exception as err:
            last_err = "%s: %s" % (model, err)
            continue
    return None, None, last_err


def build_explain_digest(args, perf, now_ms):
    """Read current learning state + recent usage rows; return (digest_text, rows_text)."""
    state = load_json(args.state) if os.path.exists(args.state) else {"perf": {}, "diag": {}, "success": {}, "fails": {}}
    perf_map = state.get("perf") or {}
    fails = state.get("fails") or {}
    diag = state.get("diag") or {}
    try:
        plugins = load_json(args.plugins)
        entry = find_entry(plugins)
        options = (entry or {}).get("options") or {}
    except Exception:
        options = {}
    learned = options.get("learned") or {}
    images = learned.get("images") or {}
    actions = learned.get("actions") or []

    out_lines = []
    out_lines.append("【当前生效规则】")
    for r in options.get("rules") or []:
        out_lines.append("  %s → %s（fallbacks: %s）" % (r.get("type"), r.get("target"), ", ".join(r.get("fallbacks") or [])))
    out_lines.append("【图片能力标记】")
    if images:
        for model, entry0 in images.items():
            state0 = entry0.get("state")
            age_min = round((now_ms - (entry0.get("at") or 0)) / 60000)
            tag = "不可用（带图请求会跳过）" if state0 == "bad" else ("可用" if state0 == "good" else state0)
            out_lines.append("  %s: %s（%d 分钟前标记）" % (model, tag, age_min))
    else:
        out_lines.append("  （无标记）")
    out_lines.append("【失败处理动作（执行中）】")
    if actions:
        for a in actions:
            left = max(0, round(((a.get("at") or 0) + (a.get("ttlMs") or 0) - now_ms) / 60000))
            out_lines.append("  %s: %s（剩 %d 分钟，by=%s%s）" % (a.get("model"), a.get("kind"), left, a.get("by"), (", cap=%s" % a["cap"]) if "cap" in a else ""))
    else:
        out_lines.append("  （无）")
    out_lines.append("【性能画像（吞吐率 tokens/s，EWMA）】")
    for m, p in sorted(perf_map.items(), key=lambda kv: -(kv[1].get("samples") or 0))[:10]:
        out_lines.append("  %s: %.1f tok/s（采样 %d，连败 %d）" % (m, p.get("ewmaTps") or 0, p.get("samples") or 0, p.get("consecFails") or 0))
    out_lines.append("【连败计数】 " + (json.dumps(fails, ensure_ascii=False) if fails else "（无）"))
    digest_text = "\n".join(out_lines)

    # recent usage rows (tail)
    rows_text = "（无法读取 usage 日志）"
    try:
        if os.path.exists(args.usage) and os.path.getsize(args.usage) > 0:
            with open(args.usage, "rb") as fh:
                fh.seek(max(0, os.path.getsize(args.usage) - 512 * 1024))
                text = fh.read().decode("utf-8", "replace")
            complete = text.split("\n")
            complete = complete[:-1] if text.endswith("\n") else complete
            picked = []
            for line in reversed(complete):
                if len(picked) >= args.explain_rows:
                    break
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                picked.append(json.dumps({
                    "time": row.get("t"), "agent": row.get("agent"), "req": row.get("req"),
                    "model": row.get("model"), "served": row.get("served"), "status": row.get("status"),
                    "ms": row.get("ms"), "ttft_ms": row.get("ttft_ms"), "out": row.get("out"),
                }, ensure_ascii=False))
            rows_text = "\n".join(picked) if picked else "（无记录）"
    except Exception as err:
        rows_text = "（读取失败：%s）" % err
    return digest_text, rows_text


def explain_mode(args, key):
    now_ms = int(time.time() * 1000)
    args.explain_rows = getattr(args, "explain_rows", 20) or 20
    try:
        state = load_json(args.state) if os.path.exists(args.state) else {"perf": {}, "diag": {}, "success": {}, "fails": {}}
    except Exception:
        state = {"perf": {}, "diag": {}, "success": {}, "fails": {}}
    perf = state.get("perf") or {}
    digest_text, rows_text = build_explain_digest(args, perf, now_ms)
    print(digest_text)
    print("== 最近请求记录（新→旧，最多 %d 条）==" % args.explain_rows)
    print(rows_text)
    if args.no_ai:
        return 0
    available = fetch_available_models(key)
    success = {}
    try:
        state = load_json(args.state)
        success = state.get("success") or {}
    except Exception:
        pass
    candidates = ai_candidates(args, success, available, perf)
    messages = [{"role": "user", "content":
        "你是 LLM 网关路由的学习分析师。以下是路由学习器的当前状态和最近的请求记录。\n"
        "请用中文分点输出：\n"
        "1) 最近路由行为的主要模式\n"
        "2) 值得注意的异常或风险\n"
        "3) 学习系统已经做了哪些调整\n"
        "简洁；只依据给出的数据，不要编造数据里没有的信息。\n\n"
        "【当前状态】\n" + digest_text + "\n\n【最近请求记录（新→旧）】\n" + rows_text}]
    print("== AI 解读（分类模型自动选择）==")
    content, used, err = ai_chat(messages, args, candidates, max_tokens=800)
    if content is None:
        print("（AI 解读不可用：%s）" % err)
        return 0
    print("（分类模型：%s）" % used)
    print(content)
    return 0


def ai_classify(row, model_key, args, candidates):
    """Try the candidates in order; return (verdict, used_model, error)."""
    key = gateway_key()
    if not key:
        return None, None, "no gateway key"
    record = json.dumps(
        {"provider": row.get("provider"), "model": model_key, "status": row.get("status"),
         "err": str(row.get("err") or "")[:500], "agent": row.get("agent")},
        ensure_ascii=False,
    )
    last_err = "no candidates"
    for model in candidates[:3]:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": AI_PROMPT + record}],
            "max_tokens": 300,
            "stream": False,
        }
        if model.startswith("cannbot/"):
            payload["reasoning_effort"] = "minimal"
        req = urllib.request.Request(GATEWAY, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            verdict = json.loads(re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip())
        except Exception as err:
            last_err = "%s: %s" % (model, err)
            continue
        if not isinstance(verdict, dict) or verdict.get("kind") not in ("pause", "clampOutput", "ignore"):
            last_err = "%s: verdict kind invalid: %s" % (model, str(verdict.get("kind")))
            continue
        ttl = verdict.get("ttlMs")
        if not (isinstance(ttl, (int, float)) and 60000 <= ttl <= 86400000):
            ttl = PAUSE_TTL_MS  # 10min default when the AI omits an out-of-range TTL
        out = {"kind": verdict["kind"], "ttlMs": int(ttl), "reason": str(verdict.get("reason") or "")[:120]}
        if verdict["kind"] == "clampOutput":
            cap = verdict.get("cap")
            out["cap"] = int(cap) if isinstance(cap, (int, float)) and 256 <= cap <= 2097152 else 4096
        return out, model, None
    return None, None, last_err


AI_PROMPT = (
    "你是 LLM 网关的故障处理分类器。下面是一条失败请求的记录。请只输出一个 JSON 对象"
    "（不要多余文字、不要代码块标记）：\n"
    '{"kind":"pause|clampOutput|ignore","ttlMs":<数字>,"cap":<数字或省略>,"reason":"一句话中文"}\n'
    "kind 含义：\n"
    "- pause：上游暂时不可用/限流/超时，暂停该模型一段时间（ttlMs 取 60000~86400000）\n"
    "- clampOutput：仅限输出参数超限（max_tokens/output 超过模型实际上限）。"
    "**输入长度超限（input/prompt/context 超长）绝不能 clampOutput**——钳输出治不了输入超长，"
    "那种情况选 ignore（payload 学习另行处理）。\n"
    "- ignore：客户端取消、一次性抖动、输入超长或无需处理\n"
    "记录："
)


def row_matches(row, statuses, keywords):
    status = row.get("status")
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if status in statuses:
        return True
    err = " ".join(str(row.get(k) or "") for k in ("err", "err_type")).lower()
    return any(kw in err for kw in keywords)


def rule_targets(options):
    targets = set()
    for rule in options.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        target = rule.get("target")
        if isinstance(target, str) and target:
            targets.add(target)
        for fb in rule.get("fallbacks") or []:
            if isinstance(fb, str) and fb:
                targets.add(fb)
    return targets


def find_entry(plugins, fragment=PLUGIN_SPEC_FRAGMENT):
    for entry in plugins.get("plugins") or []:
        if fragment in (entry.get("spec") or ""):
            return entry
    return None


def auto_tune_provider(provider_id, err_text, quiet=False):
    """从上游错误文本提取 magpie 可配置约束并自动调整。返回调整描述列表（空 = 无可调项）。"""
    err = str(err_text or "").lower()
    adjustments = []
    # 图片数量限制: "At most N image(s)"
    m = re.search(r'at most (\d+) image', err)
    if m:
        cap = int(m.group(1))
        if cap > 0:
            cmd = ["magpie", "provider", "set", provider_id, f"max-images={cap}"]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode == 0:
                    adjustments.append(f"max-images={cap}")
                    log(f"auto-tune: {provider_id} max-images={cap}（从 400 约束自动调整）", quiet)
            except Exception as e:
                log(f"auto-tune 异常: {e}", quiet)
    return adjustments


def backoff_ms(fails_count, pause_base, pause_max):
    return min(int(pause_base) * (2 ** max(0, int(fails_count))), int(pause_max))


REFLECT_PROMPT = (
    "你是 LLM 网关路由学习系统的反思审查员。以下是学习器的当前状态摘要、"
    "规则反思器已发现的洞察、以及最近的请求记录。请做一次深度反思：\n"
    "1) 验证已有洞察是否成立，指出其中可能误判的\n"
    "2) 发现规则反思器没覆盖到的问题（如趋势恶化、能力退化、配置矛盾）\n"
    "3) 给出 1-3 条具体可执行的改进建议\n"
    "用中文简洁分点；只依据给出的数据，不要编造。\n\n"
)


def ai_deep_reflect(args, perf, success, images, actions, insights, recent_rows_text, candidates):
    record = (
        "【性能画像】\n" + json.dumps(perf, ensure_ascii=False) +
        "\n【图片能力标记】\n" + json.dumps(images, ensure_ascii=False) +
        "\n【失败动作】\n" + json.dumps(actions, ensure_ascii=False) +
        "\n【规则反思器的洞察】\n" + json.dumps([i.get("text") for i in insights], ensure_ascii=False) +
        "\n【最近请求记录（新→旧）】\n" + recent_rows_text
    )
    messages = [{"role": "user", "content": REFLECT_PROMPT + record}]
    return ai_chat(messages, args, candidates, max_tokens=6000)


def norm_model(s):
    return str(s).lower().replace(".", "-").replace("_", "-")


def same_model(a, b):
    na, nb = norm_model(a), norm_model(b)
    return na == nb or na.startswith(nb) or nb.startswith(na)


def resolve_attribution(row, provider, model, known_ids):
    """归因到实际服务模型（AI 反思建议：按 req/served 分开归因）：
    served（上游回显）优先——cannbot 链内切换的请求性能/失败/能力
    应记到真正服务的模型头上，而不是代理所见的 model 字段。
    匹配规则：归一化后与已知候选前缀匹配，多条命中取最长（防
    glm-5.3 与 glm-5.3-flash 歧义）；无 served 或匹配不上退回 model。"""
    served = str(row.get("served") or "")
    if served and known_ids:
        base = norm_model(served.split("/")[-1])
        best = None
        for cand in known_ids:
            nb = norm_model(str(cand).split("/")[-1])
            if base == nb or base.startswith(nb):
                if best is None or len(nb) > len(norm_model(best.split("/")[-1])):
                    best = cand
        if best:
            return best
    return "%s/%s" % (provider, model) if provider else model


def compute_impact(usage_path, now_ms, window_days=7):
    """从 usage 历史量化插件效果（7 天滚动窗口）：
    - saves: model≠served 且 200 = 透明切换挽救的请求（预算切换/流重试/链重试）
    - availability: 实际成功率；switchGuaranteed: 依赖透明切换的请求数
      （预算/流/链切换保障了完整交付——其中预算切换防的是截断/空回复这类
      质量失败而非 HTTP 失败，不做不可辩护的"无插件可用度"反事实推断）
    - medianTtftS/medianDurationS: 200 请求的首字节/总耗时中位数（秒）。"""
    import datetime
    window_ms = window_days * 24 * 3600 * 1000

    saves = 0
    ok = 0
    fail = 0
    ttfts = []
    durations = []
    switch_pairs = {}
    try:
        size = os.path.getsize(usage_path) if os.path.exists(usage_path) else 0
        if size <= 0:
            return None
        with open(usage_path, "rb") as fh:
            fh.seek(max(0, size - 30 * 1024 * 1024))
            text = fh.read().decode("utf-8", "replace")
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("t")
            try:
                epoch_ms = int(datetime.datetime.fromisoformat(str(ts)).timestamp() * 1000)
            except Exception:
                continue
            if now_ms - epoch_ms > window_ms:
                continue
            agent = str(row.get("agent") or "")
            # 内部调用（magpie 自身的代看/分类/反思、伴生探针）不是用户请求：
            # 不进可用度/错误率/切换保障任何分子分母（与空回复率口径对齐，0.3.57）。
            if "route-learn" in agent or agent in ("Python-urllib", "magpie"):
                continue
            status = row.get("status")
            if isinstance(status, str) and status.isdigit():
                status = int(status)
            model = str(row.get("model") or "")
            served = str(row.get("served") or "")
            if status == 200:
                ok += 1
                if model and served and not same_model(model, served):
                    saves += 1
                    key = "%s→%s" % (model.split("/")[-1], served.split("/")[-1])
                    switch_pairs[key] = switch_pairs.get(key, 0) + 1
                ms = row.get("ms")
                if isinstance(ms, (int, float)) and ms > 0:
                    durations.append(ms)
                ttft = row.get("ttft_ms")
                if isinstance(ttft, (int, float)) and ttft > 0:
                    ttfts.append(ttft)
            elif isinstance(status, int) and status >= 400:
                if status != 499:  # 客户端取消 ≠ 服务失败（可用度同口径）
                    fail += 1
    except Exception:
        return None

    def med(arr):
        if not arr:
            return None
        arr = sorted(arr)
        return round(arr[len(arr) // 2] / 1000, 1)

    total = ok + fail
    if not total:
        return None

    # ── 装插件前后对比（用户要求看到"相对没有使用前的效果"）──
    # 边界 = 全历史中首次出现切换（model≠served 且 200）的行时间——之前 = 无插件期。
    # 两遍扫：先找边界，再分era累计（错误率 + 空回复率）。
    before_after = None
    try:
        first_switch_ms = None
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("status") != 200:
                continue
            model = str(row.get("model") or "")
            served = str(row.get("served") or "")
            if model and served and not same_model(model, served):
                try:
                    ts_ms = int(datetime.datetime.fromisoformat(str(row.get("t"))).timestamp() * 1000)
                    if first_switch_ms is None or ts_ms < first_switch_ms:
                        first_switch_ms = ts_ms
                except Exception:
                    continue
        if first_switch_ms is not None:
            eras = {"pre": {"ok": 0, "fail": 0, "empty": 0}, "post": {"ok": 0, "fail": 0, "empty": 0}}
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                agent = str(row.get("agent") or "")
                if "route-learn" in agent or agent in ("Python-urllib", "magpie"):
                    continue
                status = row.get("status")
                if isinstance(status, str) and status.isdigit():
                    status = int(status)
                try:
                    ts_ms = int(datetime.datetime.fromisoformat(str(row.get("t"))).timestamp() * 1000)
                except Exception:
                    continue
                era = "pre" if ts_ms < first_switch_ms else "post"
                if status == 200:
                    eras[era]["ok"] += 1
                    # 空回复口径（7d 实测拆解修正）：只算真实用户请求——
                    # 剔 agent=magpie 内部调用（classify 等 out=0 正常）、
                    # 剔 in=0 行（超长操作未记录输入，跨时代不可比）。
                    out_v = row.get("out")
                    in_v = row.get("in")
                    if (isinstance(out_v, (int, float)) and out_v == 0
                            and agent != "magpie"
                            and isinstance(in_v, (int, float)) and in_v > 0):
                        eras[era]["empty"] += 1
                elif isinstance(status, int) and status >= 400:
                    # 499 = 客户端主动取消（实测 7d 的 0.1% 错误率 15/16 是它），
                    # 不是服务失败——剔出错误率。
                    if status != 499:
                        eras[era]["fail"] += 1
            def era_stats(e):
                t = e["ok"] + e["fail"]
                if not t:
                    return None
                return {
                    "requests": t,
                    "errorRatePct": round(100.0 * e["fail"] / t, 1),
                    "emptyRatePct": round(100.0 * e["empty"] / t, 1),
                }
            pre_s, post_s = era_stats(eras["pre"]), era_stats(eras["post"])
            if pre_s and post_s:
                before_after = {
                    "boundary": time.strftime("%m-%d %H:%M", time.localtime(first_switch_ms / 1000)),
                    "pre": pre_s,
                    "post": post_s,
                }
    except Exception:
        before_after = None

    return {
        "windowDays": window_days,
        "requests": total,
        "saves": saves,
        "savesDetail": dict(sorted(switch_pairs.items(), key=lambda kv: -kv[1])[:5]),
        "availability": round(100.0 * ok / total, 1),
        "switchGuaranteedPct": round(100.0 * saves / total, 1),
        "medianTtftS": med(ttfts),
        "medianDurationS": med(durations),
        # 反事实估计（用户明确要求"预计没有用的时候会怎么样"）：若现在移除
        # 插件（当前预算与路由不变），切换保障的请求预计以掐流/超限失败——
        # 预算超上限→推理烧尽→掐流有 session-87db83cd 实证（重试 5 次全败）。
        # 与"装前历史"区分：装前是 4000 预算时代且路由 glm 优先，本就健康。
        "estimatedNoPlugin": {
            "availabilityPct": round(100.0 * max(0, ok - saves) / total, 1),
            "wouldFail": saves,
        },
        "beforeAfter": before_after,
    }


def build_learned_view(perf, images, actions, insights, last_reflect, now_ms):
    """人读视图：伴生写进 options.learnedView，magpie 插件页渲染。"""
    cur_bkey = bucket_key(now_ms)
    perf_summary = []
    for m, p in perf.items():
        s = perf_score_now(p, now_ms)
        ttf_ms0 = p.get("ewmaTtfbMs")
        e0 = {"model": m, "score": s, "tps": p.get("ewmaTps"),
              "ttfS": round(ttf_ms0 / 1000, 1) if isinstance(ttf_ms0, (int, float)) else None,
              "samples": p.get("samples"), "consecFails": p.get("consecFails")}
        # 时段桶可见性（AI 反思建议：桶键不自解释是断言障碍）
        bs = perf_score_bucket(p, cur_bkey)
        bkt = (p.get("buckets") or {}).get(cur_bkey)
        if bs is not None:
            e0["bucketScore"] = bs
        if bkt and (bkt.get("samples") or 0) > 0:
            e0["bucketSamples"] = bkt.get("samples")
        ft = p.get("fastTps")
        if isinstance(ft, (int, float)):
            e0["fastTps"] = ft
        if p.get("capabilityDegraded"):
            e0["degraded"] = p["capabilityDegraded"]
        perf_summary.append(e0)
    perf_summary.sort(key=lambda e: -(e["score"] if e["score"] is not None else -1))
    view = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
        "bucket": {"key": cur_bkey, "label": bucket_label(cur_bkey)},
        # 评分口径透明化（AI 反思建议）：公式/窗口/门槛全公开，可独立核验
        "scoring": {
            "score": "tps ÷ (1+连败) × 首字节惩罚 ÷ (1+ttfb秒/10)；TTFB 需 ttfbSamples≥3（桶内同门槛）；499/输入超长/空回复预算不计失败（服务失败口径）",
            "buckets": "周末×时段4桶共8桶，各自 EWMA(0.7/0.3)；桶内样本≥3 优先于全局分；桶归属按行时间戳（非扫描时间）",
            "reorder": "评分降序换头，挑战者需超现任 15%（迟滞防抖）；样本≥5 参与；capabilityDegraded 匹配规则类型 ×0.1；桶失败率再除 (1+2×rate)；ttfb 样本不足者不参与换头（现任不可信则本轮暂缓）；慢+失败联合（桶失败率≥30% 且快窗<0.6×慢窗）→ 10 分钟短熔断",
            "fastTps": "快窗 EWMA(0.3/0.7≈最近3样本) vs 慢窗(0.7/0.3)；<0.6 且样本≥5 → 趋势恶化告警",
            "attribution": "perf/失败/能力按 served（上游回显，归一化最长前缀匹配已知候选）归因；无回显退回 model 字段",
        },
        "perfSummary": perf_summary[:8],
        "insights": insights[:10],
        "imagesView": {m: {"state": v.get("state"), "via": v.get("via"),
                           "ageMin": round((now_ms - (v.get("at") or 0)) / 60000)}
                       for m, v in images.items()},
        "actionsView": [{"model": a.get("model"), "kind": a.get("kind"),
                         "leftMin": max(0, round(((a.get("at") or 0) + (a.get("ttlMs") or 0) - now_ms) / 60000)),
                         "by": a.get("by")} for a in actions],
    }
    if last_reflect:
        view["lastReflect"] = {"at": last_reflect.get("at"), "model": last_reflect.get("model"),
                               "text": str(last_reflect.get("text") or "")[:8000]}
    return view


def with_impact(view, state):
    """把效果量化（state.impact，每小时由主循环重算）挂进视图。"""
    impact = state.get("impact")
    if impact:
        view["impact"] = impact
    return view


def reflect_on_state(rules, images, actions, perf, fails, ignore_sigs, now_ms, image_ttl_ms):
    """周期性自省：审视学到的状态，发现过时、矛盾、覆盖缺口。
    返回 insights 列表 [{level: warn|info|suggest, text}]。"""
    insights = []
    # 1. 图片候选全灭检测：所有候选都被拉黑（fresh bad）→ 带图请求将必败
    image_targets = []
    for rule in rules or []:
        if rule.get("type") != "images":
            continue
        cands = [rule.get("target")] + list(rule.get("fallbacks") or [])
        for m in cands:
            if m and m not in image_targets:
                image_targets.append(m)
    if image_targets:
        fresh_bad = [m for m in image_targets
                     if isinstance(images.get(m), dict) and images[m].get("state") == "bad"
                     and (now_ms - (images[m].get("at") or 0)) < image_bad_window(images[m], image_ttl_ms)]
        if len(fresh_bad) == len(image_targets):
            insights.append({"level": "warn",
                             "text": "图片候选全部不可用（%s）——带图请求将失败；检查网关或增补候选"
                                     % ", ".join(fresh_bad)})
    # 2. 连败 ≥2 的模型：熔断在自动处理，但持续连败值得人工关注
    for m, p in perf.items():
        cf = int(p.get("consecFails") or 0)
        if cf >= 2:
            insights.append({"level": "warn",
                             "text": "%s 连败 %d 次（吞吐率 %s tok/s）——若非流量特性变化，建议人工检查该模型"
                                     % (m, cf, p.get("ewmaTps"))})
    # 2b. 趋势恶化（AI 反思建议）：近期快窗 TPS 显著低于长期 EWMA 且不是连败——
    #     渐进变慢/间歇劣化是 consecFails 的盲区，快慢双 EWMA 对比才能看见。
    for m, p in perf.items():
        ft, et = p.get("fastTps"), p.get("ewmaTps")
        if isinstance(ft, (int, float)) and isinstance(et, (int, float)) and et >= 5 \
                and (p.get("samples") or 0) >= 5 and ft < et * 0.6:
            insights.append({"level": "warn",
                             "text": "%s 趋势恶化：近期吞吐 %.1f 显著低于历史 %.1f tok/s（非连败型劣化）"
                                     % (m, ft, et)})
    # 2c. 样本陈旧（AI 反思建议）：lastAt 超过 24h 无更新——流量消失或采集停滞
    for m, p in perf.items():
        age_h = (now_ms - (p.get("lastAt") or 0)) / 3600000.0
        if (p.get("samples") or 0) > 0 and age_h > 24:
            insights.append({"level": "info",
                             "text": "%s 性能样本已 %.0f 小时未更新（无流量或采集停滞，评分可能不反映现状）"
                                     % (m, age_h)})
    # 2d. 桶级失败集中：绝对量（fails≥3 且样本≥5 且失败率≥10%）或小样本高失败率
    #     （fails≥2 且 rate≥50%）。率下限 10% 是反思纠错：大桶 3-5% 的失败率属正常
    #     基线噪声，曾把最佳时段桶误判降权（本机实证 16/321 与 37/1379 两例）。
    for m, p in perf.items():
        for bkey, b in (p.get("buckets") or {}).items():
            bf, bs = b.get("fails") or 0, b.get("samples") or 0
            rate = (bf / bs) if bs > 0 else 0
            if (bf >= 3 and bs >= 5 and rate >= 0.10) or (bf >= 2 and rate >= 0.5):
                insights.append({"level": "warn",
                                 "text": "%s 在 %s 时段失败集中（%d/%d，%.0f%%）——该时段应自动降权，持续则人工检查"
                                         % (m, bucket_label(bkey), bf, bs, rate * 100)})
    # 2e. 首字节样本不足（0.3.56）：评分不含时延惩罚 → 指标不可信，换头已暂缓
    for m, p in perf.items():
        if (p.get("samples") or 0) >= 5 and (p.get("ttfbSamples") or 0) < 3:
            insights.append({"level": "info",
                             "text": "%s 首字节样本不足（%d）——评分不含时延惩罚，指标不可信，已暂缓据此换头"
                                     % (m, p.get("ttfbSamples") or 0)})
    # 2f. 桶吞吐分化（反思发现）：某桶 tps 显著低于全局——流量特性或该时段退化，
    #     与失败率合看（只看失败率曾漏掉 28.9 vs 78.9 tps 的极端分化）
    for m, p in perf.items():
        g = p.get("ewmaTps")
        for bkey, b in (p.get("buckets") or {}).items():
            bt, bs = b.get("ewmaTps"), b.get("samples") or 0
            if isinstance(g, (int, float)) and g >= 5 and isinstance(bt, (int, float)) and bs >= 5 and bt < g * 0.5:
                insights.append({"level": "warn",
                                 "text": "%s 在 %s 时段吞吐显著低于全局（%.1f vs %.1f tok/s）——流量特性或该时段退化，结合失败率判断"
                                         % (m, bucket_label(bkey), bt, g)})
    # 3. 从未被试探的候选：能力未知（可能是从未有过带图/该类流量的模型）
    unsampled = [m for m, p in perf.items() if int(p.get("samples") or 0) == 0]
    if unsampled:
        insights.append({"level": "info",
                         "text": "候选 %s 尚无采样数据——能力未知，建议用小请求主动试探一次" % ", ".join(unsampled)})
    # 4. clampOutput 长期未验证：模型上限可能已变化（半开试探依据）
    for a in actions:
        if a.get("kind") == "clampOutput":
            age_min = round((now_ms - (a.get("at") or 0)) / 60000)
            if age_min > 7 * 24 * 60:
                insights.append({"level": "info",
                                 "text": "%s 的 clampOutput cap=%s 已 %d 天未验证——模型上限可能已变化"
                                         % (a.get("model"), a.get("cap"), age_min // 1440)})
    # 5. images 拉黑即将到期：提示恢复试探即将开始
    for m, entry0 in images.items():
        if entry0.get("state") == "bad":
            window = image_bad_window(entry0, image_ttl_ms)
            left_min = round(((entry0.get("at") or 0) + window - now_ms) / 60000)
            if 0 < left_min <= 30:
                if entry0.get("via") == "describe":
                    insights.append({"level": "info",
                                     "text": "%s 的原生无视觉复核还有 %d 分钟——周度复核后将恢复带图试探" % (m, left_min)})
                else:
                    insights.append({"level": "info",
                                     "text": "%s 的图片拉黑还剩 %d 分钟到期——到期后将恢复带图试探" % (m, left_min)})
                break
    # 6. 评分排名与候选顺序的差距（自适应因防抖未换头的状态可见化）
    for rule in rules or []:
        if rule.get("type") != "images":
            continue
        cands = [rule.get("target")] + list(rule.get("fallbacks") or [])
        scored = [(m, perf_score(perf.get(m))) for m in cands if m]
        sampled = [x for x in scored if x[1] is not None]
        if len(sampled) < 2:
            continue
        sampled.sort(key=lambda t: -t[1])
        if sampled[0][0] != cands[0]:
            insights.append({"level": "info",
                             "text": "按当前评分 %s 应排首位（现任 %s 差距不足防抖阈值，暂不换头）"
                                     % (sampled[0][0], cands[0])})
    return insights


def bootstrap(entry, args, key):
    """Self-discovery on a fresh machine: seed image-routing rules from the
    gateway's own model list, ordered by models that recently served 200;
    register the learning scheduled task. Returns a summary string."""
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    seeded = False
    if options.get("rules"):
        summary = "已有规则，跳过播种"
    else:
        available = fetch_available_models(key) or []
        # self-discovery from the usage log: rows tell us which concrete
        # members actually served (for group requests, row.model is the
        # member) and which ids are groups themselves (never rewrite X to
        # the same group).
        recent = []
        group_ids = set()
        try:
            size = os.path.getsize(args.usage)
            with open(args.usage, "rb") as fh:
                fh.seek(max(0, size - 512 * 1024))  # recent tail only
                text = fh.read().decode("utf-8", "replace")
            for line in text.split("\n")[:-1]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("status") != 200:
                    continue
                req = str(row.get("req") or "")
                if req.startswith("group/"):
                    group_ids.add(req)
                provider = str(row.get("provider") or "")
                model = row.get("model")
                if provider and model:
                    recent.append("%s/%s" % (provider, model))
        except OSError:
            pass
        seen = set()
        concrete = []
        groups_last = []
        for m in recent + available:  # recent-success first, then list order
            if not m or m in seen or m in group_ids:
                continue
            seen.add(m)
            if m.startswith("group/"):
                groups_last.append(m)
            else:
                concrete.append(m)
        candidates = (concrete + groups_last)[:5]
        if len(candidates) < 2:
            return "候选模型不足（%d 个），不播种；请确认 magpie 网关在运行" % len(candidates)
        options["rules"] = [{
            "type": "images",
            "target": candidates[0],
            "fallbacks": candidates[1:],
        }]
        options["clamp"] = True
        options.setdefault("learningTtlMs", DEFAULT_TTL_MS)
        options.setdefault("learned", {"images": {}, "actions": []})
        entry["options"] = options
        seeded = True
        summary = "已播种 images 规则：%s（fallbacks: %s）" % (candidates[0], ", ".join(candidates[1:]))
    # register the learning scheduled task (Windows). The task prefers the
    # INSTALLED package's copy of this script (plugins/node_modules/...), so
    # `magpie plugin update` — GUI included — flows straight into the next
    # run; 0.3.49's index.mjs repoints the task on every plugin-host restart.
    task_note = ""
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    target = os.path.abspath(__file__)
    installed = os.path.join(DEFAULT_CONFIG_DIR, "plugins", "node_modules",
                             "magpie-route-rules", "tools", "route-learn.py")
    if os.path.isfile(installed) and os.path.abspath(installed).lower() != target.lower():
        target = os.path.abspath(installed)
    tr = '"%s" "%s" --quiet' % (pythonw, target)
    try:
        probe = subprocess.run(["schtasks", "/query", "/tn", "Magpie route learn", "/v", "/fo", "LIST"],
                               capture_output=True, text=True, timeout=20)
        run_line = next((l for l in (probe.stdout or "").splitlines()
                         if "Task To Run" in l or "要运行的任务" in l), "")
        run_cmd = run_line.split(":", 1)[-1] if run_line else ""
        if probe.returncode != 0 or target.lower() not in run_cmd.lower():
            subprocess.run(["schtasks", "/create", "/tn", "Magpie route learn", "/tr", tr,
                            "/sc", "minute", "/mo", "5", "/f"], check=True, timeout=20)
            fired = subprocess.run(["schtasks", "/run", "/tn", "Magpie route learn"],
                                   capture_output=True, timeout=20)
            task_note = "；已注册/重指向计划任务（每 5 分钟，指向 %s）%s" % (
                target, "，已立即触发首轮学习" if fired.returncode == 0 else "；立即触发失败，等下一节拍")
        else:
            task_note = "；计划任务已存在且指向包内脚本"
    except Exception as err:
        task_note = "；计划任务注册失败（%s），可手动注册" % err
    return summary + task_note


# ── DSH 容量回写（0.3.52）：让自动 compact 用真实窗口 ──
# DSH 的自动 compact 阈值 = min(0.8×contextWindow, window−maxTokens−65536)，
# 而 magpie /v1/models 对所有模型都报假 1M（二进制无输入窗口旋钮），DSH 的
# settings.yaml/patch 配置条目优先级又最高——所以把 payload 夹逼学到的真实
# 容量回写进 DSH 配置。magpie dsh 同步会重置回 1M，每轮（5 分钟）回写自愈。
# 只写证据值（maxOkTok 优先：实测放行过）；低于 maxTokens+headroom+余量下限
# 的跳过（防 compaction 配置崩）；非默认 1M 也非我方旧值的数字视作用户手设，
# 尊重不动。

DSH_SETTINGS = os.path.join(os.path.expanduser("~"), ".dsh", "settings.yaml")
DSH_PROFILES = os.path.join(os.path.expanduser("~"), ".dsh", "profiles")
MAGPIE_DEFAULT_WINDOW = 1048576  # magpie 目录缺报时的默认假窗口
WRITEBACK_FLOOR_EXTRA = 32768    # window 至少比 maxTokens+65536 多这么多


def _atomic_write_text(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _model_blocks(text):
    """每个 '- id: <x>' 列表项的 (model_id, 起始行, 结束行)。文本级、缩进感知、
    保留原格式；只关心块内含 contextWindow 字段的（嵌套模型条目）。"""
    lines = text.split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-\s+id:\s*(\S+)\s*$", line)
        if not m:
            continue
        indent, model_id = m.group(1), m.group(2)
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and not nxt.startswith(indent + " "):
                break
            j += 1
        if any(re.match(r"^\s+contextWindow:\s*\d+\s*$", b) for b in lines[i:j]):
            hits.append((model_id, i, j))
    return hits


def writeback_dsh_windows(payload_caps, state, quiet, dry_run=False, files=None):
    """把学到的真实容量回写 DSH 配置的 contextWindow，返回是否有修改。"""
    if not payload_caps or not isinstance(state, dict):
        return False
    if files is None:
        files = []
        if os.path.isfile(DSH_SETTINGS):
            files.append(DSH_SETTINGS)
        if os.path.isdir(DSH_PROFILES):
            for name in sorted(os.listdir(DSH_PROFILES)):
                p = os.path.join(DSH_PROFILES, name, "cordis.patch.yml")
                if os.path.isfile(p):
                    files.append(p)
    wrote = state.setdefault("dshWindows", {})
    user_set = state.setdefault("dshWindowsUserSet", {})
    any_change = False
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        lines = text.split("\n")
        notes = []
        dirty = False
        for model_id, start, end in _model_blocks(text):
            pc = payload_caps.get(model_id)
            if not isinstance(pc, dict):
                continue
            ok_tok = pc.get("maxOkTok")
            fail_tok = pc.get("minFailTok")
            target = int(ok_tok) if isinstance(ok_tok, (int, float)) and ok_tok > 0 \
                else (int(fail_tok) if isinstance(fail_tok, (int, float)) and fail_tok > 0 else None)
            if target is None:
                continue
            cw_idx = cw_indent = None
            cw_val = mt_val = 0
            for k in range(start, end):
                m = re.match(r"^(\s+)contextWindow:\s*(\d+)\s*$", lines[k])
                if m:
                    cw_idx, cw_indent, cw_val = k, m.group(1), int(m.group(2))
                    break
            if cw_idx is None:
                continue
            for k in range(start, end):
                m = re.match(r"^\s+maxTokens:\s*(\d+)\s*$", lines[k])
                if m:
                    mt_val = int(m.group(1))
                    break
            key = "%s|%s" % (os.path.relpath(path, os.path.expanduser("~")), model_id)
            floor = mt_val + 65536 + WRITEBACK_FLOOR_EXTRA
            if target < floor:
                if cw_val == MAGPIE_DEFAULT_WINDOW and not user_set.get(key):
                    notes.append("%s 跳过（证据 %d < 下限 %d）" % (model_id, target, floor))
                continue
            if cw_val == target:
                wrote[key] = target
                continue
            if cw_val != MAGPIE_DEFAULT_WINDOW and wrote.get(key) != cw_val:
                if not user_set.get(key):
                    user_set[key] = cw_val
                    notes.append("%s 尊重手设 %d（证据 %d）" % (model_id, cw_val, target))
                continue
            lines[cw_idx] = "%scontextWindow: %d" % (cw_indent, target)
            wrote[key] = target
            user_set.pop(key, None)
            dirty = True
            notes.append("%s %d→%d" % (model_id, cw_val, target))
        if not dirty:
            continue
        any_change = True
        if not dry_run:
            backup(path)
            _atomic_write_text(path, "\n".join(lines))
        log("容量回写 %s：%s" % (os.path.basename(path), "；".join(notes)), quiet)
    return any_change


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-ai", action="store_true", help="unknown signatures get the conservative default instead of an AI verdict")
    ap.add_argument("--no-dsh-writeback", action="store_true", help="skip writing learned payload capacities into DSH contextWindow configs")
    ap.add_argument("--usage", default=DEFAULT_USAGE)
    ap.add_argument("--plugins", default=DEFAULT_PLUGINS)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL_MS, help="image-mark TTL (ms)")
    ap.add_argument("--status", default=DEFAULT_STATUS, help="image-failure HTTP statuses (comma list)")
    ap.add_argument("--keywords", default=DEFAULT_KEYWORDS)
    ap.add_argument("--ai-model", default=None, help="explicit classifier model (default: auto from history + /v1/models)")
    ap.add_argument("--ai-cap", type=int, default=3, help="max AI classification attempts per run")
    ap.add_argument("--clamp-skip-providers", default="cannbot", help="providers whose output clamping is handled by their own plugin")
    ap.add_argument("--pause-base", type=int, default=DEFAULT_PAUSE_BASE, help="first-failure pause duration (ms)")
    ap.add_argument("--pause-max", type=int, default=DEFAULT_PAUSE_MAX, help="backoff cap (ms)")
    ap.add_argument("--auto-tune", action="store_true", default=True, help="auto-adjust magpie provider config from error constraints")
    ap.add_argument("--no-auto-tune", dest="auto_tune", action="store_false", help="disable auto-adjusting magpie config from error constraints")
    ap.add_argument("--classify", metavar="TEXT", help="classify one error text with the AI and print the verdict")
    ap.add_argument("--explain", action="store_true", help="print learning digest + AI narrative of recent routing behavior")
    ap.add_argument("--explain-rows", type=int, default=20, help="rows of recent usage fed to the AI narrative")
    ap.add_argument("--bootstrap", action="store_true", help="fresh machine: seed rules from /v1/models + register the learning task")
    ap.add_argument("--learned", action="store_true", help="print what has been learned (images / actions / counters), change nothing")
    ap.add_argument("--reflect", action="store_true", help="scan + rule insights + AI deep reflection (verifies learned state, finds problems)")
    args = ap.parse_args()

    key = gateway_key()

    if args.learned:
        try:
            plugins = load_json(args.plugins)
            entry = find_entry(plugins)
            options = (entry or {}).get("options") or {}
            learned = options.get("learned") or {}
            images = learned.get("images") or {}
            actions = learned.get("actions") or []
            state = load_json(args.state) if os.path.exists(args.state) else {}
            fails = state.get("fails") or {}
            success = state.get("success") or {}
            perf = state.get("perf") or {}
            diag = state.get("diag") or {}
        except Exception as err:
            print("读取失败：%s" % err)
            return 1
        now = int(time.time() * 1000)
        usage_exists = os.path.exists(args.usage)
        usage_size = os.path.getsize(args.usage) if usage_exists else 0
        print("== 运行诊断 ==")
        print("  usage 文件: %s（%s，%d 字节）" % (args.usage, "存在" if usage_exists else "不存在", usage_size))
        print("  读取位置 offset: %s" % state.get("offset"))
        last_at = diag.get("lastRunAt") or 0
        print("  上次扫描: %s | 扫描 %s 行 | 成功200: %s | 学到失败动作: %s | 自身流量跳过: %s" % (
            time.strftime("%m-%d %H:%M", time.localtime(last_at / 1000)) if last_at else "从未运行",
            diag.get("scanned"), diag.get("success200"), diag.get("learnedFailures"), diag.get("ownSkipped")))
        agents = diag.get("agents") or {}
        if agents:
            top = sorted(agents.items(), key=lambda kv: -kv[1])[:5]
            print("  流量来源(agent): " + ", ".join("%s×%d" % (k, v) for k, v in top))
        if diag.get("reason"):
            print("  上次提前退出原因: %s" % diag["reason"])
        # 计划任务自诊断（Windows）
        try:
            q = subprocess.run(["schtasks", "/query", "/tn", "Magpie route learn", "/v", "/fo", "LIST"],
                               capture_output=True, timeout=20)
            out = q.stdout.decode("gbk", "replace") if isinstance(q.stdout, bytes) else str(q.stdout)
            fields = {}
            for line in out.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip()
            status = fields.get("状态") or fields.get("Status")
            lastrun = fields.get("上次运行时间") or fields.get("Last Run Time")
            lastres = fields.get("上次结果") or fields.get("Last Result")
            torun = fields.get("要运行的任务") or fields.get("Task To Run")
            print("== 计划任务「Magpie route learn」==")
            print("  状态: %s | 上次运行: %s | 上次结果: %s" % (status or "未知", lastrun or "从未", lastres))
            if torun:
                print("  命令: %s" % torun)
        except Exception as err:
            print("== 计划任务查询失败（%s）==" % err)
        print("== 图片能力标记（learned.images，TTL %dh）==" % (args.ttl // 3600000))
        if not images:
            print("  （无——尚无带图失败样本；所有候选按未知能力对待，允许试探）")
        for model, entry0 in images.items():
            state0 = entry0.get("state")
            window = image_bad_window(entry0, args.ttl)
            left_min = max(0, round(((entry0.get("at") or 0) + window - now) / 60000))
            age_min = round((now - (entry0.get("at") or 0)) / 60000)
            if state0 == "bad":
                if entry0.get("via") == "describe":
                    print("  %s: 不可用（代看链路确认原生无视觉，%d 分钟前标记，%d 分钟后周度复核）→ 带图请求跳过" % (model, age_min, left_min))
                else:
                    print("  %s: 不可用（%d 分钟前失败，%d 分钟后过期恢复试探）→ 带图请求跳过" % (model, age_min, left_min))
            elif state0 == "good":
                print("  %s: 可用（原生视觉，正学习确认）" % model)
            else:
                print("  %s: %s" % (model, state0))
        print("== 失败处理动作（learned.actions，执行中）==")
        live = [a for a in actions if (now - (a.get("at") or 0)) < (a.get("ttlMs") or 0)]
        expired = len(actions) - len(live)
        if not actions:
            print("  （无——尚无失败签名；常见失败由规则分类，未知签名由 AI 分类）")
        for a in actions:
            left = max(0, round(((a.get("at") or 0) + (a.get("ttlMs") or 0) - now) / 60000))
            state1 = "生效中" if a in live else "已过期"
            extra = " cap=%s" % a["cap"] if "cap" in a else ""
            reason = " | %s" % a["reason"] if a.get("reason") else ""
            print("  %s: %s%s [%s，剩 %d 分钟，by=%s]%s" % (a.get("model"), a.get("kind"), extra, state1, left, a.get("by"), reason))
        if expired:
            print("  （另有 %d 条已过期动作，会随下次写入清理）" % expired)
        print("== 连败计数（指数退避用）==")
        print("  " + (json.dumps(fails, ensure_ascii=False) if fails else "（无）"))
        print("== 反思洞察（周期自省产出）==")
        insights_state = state.get("insights") or []
        if not insights_state:
            print("  （无——当前状态健康，无待关注事项）")
        for ins in insights_state:
            print("  [%s] %s" % (ins.get("level"), ins.get("text")))
        last_reflect = state.get("lastReflect") or {}
        if last_reflect.get("at"):
            print("  上次 AI 深度反思: %s（%s）" % (
                time.strftime("%m-%d %H:%M", time.localtime((last_reflect.get("at") or 0) / 1000)),
                str(last_reflect.get("model") or "")))
        print("== 性能画像与评分（体验自适应依据）==")
        perf_rows = []
        for m, p in perf.items():
            samples = int(p.get("samples") or 0)
            s = perf_score(p) if samples >= 2 else None
            img = images.get(m) or {}
            img_bad = isinstance(img, dict) and img.get("state") == "bad"
            perf_rows.append((m, p, s, img_bad))
        if not perf_rows:
            print("  （无采样——伴生扫描尚未积累）")
        else:
            # 评分降序（None 末尾），与中间件的自适应优先级一致
            perf_rows.sort(key=lambda t: (-(t[2] if t[2] is not None else -1)))
            for m, p, s, img_bad in perf_rows:
                tps = "%.1f" % p["ewmaTps"] if p.get("ewmaTps") is not None else "-"
                ttfb = "%.1fs" % (p["ewmaTtfbMs"] / 1000) if p.get("ewmaTtfbMs") is not None else "-"
                score = "%.1f" % s if s is not None else "采样不足"
                extra = " | 图片暂不可用" if img_bad else ""
                print("  %s: 评分 %s | %.1f tok/s | 首字节 %s | 连败 %d | 采样 %d%s" % (
                    m, score, p.get("ewmaTps") or 0, ttfb, p.get("consecFails") or 0, p.get("samples") or 0, extra))
        print("== 观察到的健康模型（最近成功 top%d）==" % SUCCESS_KEEP)
        for m, v in sorted(success.items(), key=lambda kv: -(kv[1].get("lastAt") or 0))[:8]:
            print("  %s: 成功 %s 次，最近 %s" % (m, v.get("count"), time.strftime("%m-%d %H:%M", time.localtime((v.get("lastAt") or 0) / 1000))))
        if not success:
            print("  （无）")
        print("== 当前生效的规则（中间件正在执行的）==")
        for r in options.get("rules") or []:
            print("  %s → %s（fallbacks: %s）" % (r.get("type"), r.get("target"), ", ".join(r.get("fallbacks") or [])))
        print("== 读取位置 ==")
        print("  usage.jsonl offset=%s" % state.get("offset"))
        return 0

    if args.explain:
        return explain_mode(args, key)

    if args.bootstrap:
        try:
            plugins = load_json(args.plugins)
        except Exception as err:
            log("读取 plugins.json 失败：%s" % err, args.quiet)
            return 1
        entry = find_entry(plugins)
        if entry is None:
            log("plugins.json 中没有 route-rules 插件条目——请先 magpie plugin add github:wheelBL/magpie-route-rules", args.quiet)
            return 1
        summary = bootstrap(entry, args, key)
        if "已播种" in summary:
            backup(args.plugins)
            atomic_write_json(args.plugins, plugins)
        log("bootstrap：%s" % summary, args.quiet)
        return 0

    statuses = {int(s) for s in str(args.status).split(",") if str(s).strip().isdigit()}
    keywords = [k.strip().lower() for k in str(args.keywords).split(",") if k.strip()]
    clamp_skip = {p.strip() for p in str(args.clamp_skip_providers).split(",") if p.strip()}

    state = {"usage": args.usage, "offset": 0, "success": {}, "fails": {}, "diag": {}, "perf": {}}
    try:
        raw = load_json(args.state)
        if isinstance(raw, dict) and raw.get("usage") == args.usage:
            state["offset"] = int(raw.get("offset") or 0)
            if isinstance(raw.get("success"), dict):
                state["success"] = raw["success"]
            if isinstance(raw.get("fails"), dict):
                state["fails"] = raw["fails"]
            if isinstance(raw.get("perf"), dict):
                state["perf"] = raw["perf"]
            if isinstance(raw.get("diag"), dict):
                state["diag"] = raw["diag"]
            # 持久字段一并带过（0.3.47 修：曾遗漏——lastAiReflectAt 丢失 → 24h 门读到 0，
            # AI 反思每 5 分钟重跑；ignore_sigs 丢失 → 已忽略签名反复进 AI 分类）
            for k in ("lastAiReflectAt", "lastReflect", "insights", "ignore_sigs"):
                if raw.get(k) is not None:
                    state[k] = raw[k]
    except Exception:
        pass
    success = state["success"]
    fails = state["fails"]
    perf = state["perf"]

    def save_state_diag(reason):
        state["diag"] = {"reason": reason, "at": int(time.time() * 1000)}
        atomic_write_json(args.state, state)

    try:
        plugins = load_json(args.plugins)
    except Exception as err:
        reason = "读取 plugins.json 失败：%s" % err
        save_state_diag(reason)
        log(reason, args.quiet)
        return 1
    entry = find_entry(plugins)
    if entry is None:
        reason = "plugins.json 中没有 route-rules 插件条目（插件未安装或 spec 变化）"
        save_state_diag(reason)
        log(reason, args.quiet)
        return 1
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    targets = rule_targets(options)

    def save_state():
        state["diag"]["lastRunAt"] = int(time.time() * 1000)
        atomic_write_json(args.state, state)

    def refresh_quiet_view(now0):
        """无新行/无 usage 文件也要刷新 learnedView：历史数据已在 state 与
        options.learned 里，静默机器（offset 已到末尾、暂无新流量）同样
        能在 GUI 看到学习全景——首装 0.3.16 后"尚无 learnedView"的根因。"""
        try:
            learned0 = options.get("learned") or {}
            images0 = learned0.get("images") or {}
            actions0 = learned0.get("actions") or []
            perf0 = state.get("perf") or {}
            insights0 = reflect_on_state(options.get("rules") or [], images0, actions0, perf0,
                                         state.get("fails") or {}, state.get("ignore_sigs") or {}, now0, args.ttl)
            view0 = build_learned_view(perf0, images0, actions0, insights0, state.get("lastReflect"), now0)
            view0 = with_impact(view0, state)
            if view0 != options.get("learnedView"):
                backup(args.plugins)
                options["learned"] = {"images": images0, "actions": actions0,
                                      "payloadCaps": learned0.get("payloadCaps") or {}}
                options["learnedView"] = view0
                entry["options"] = options
                atomic_write_json(args.plugins, plugins)
                log("learnedView 已刷新（静默路径，无新行）", args.quiet)
        except Exception as err:
            log("learnedView 静默刷新失败：%s" % err, args.quiet)

    now_ms = int(time.time() * 1000)
    if not os.path.exists(args.usage):
        reason = "usage 文件不存在：%s（检查 magpie 配置目录是否在 ~/.config/magpie）" % args.usage
        log(reason, args.quiet)
        state["diag"] = {"reason": reason, "at": now_ms}
        save_state()
        refresh_quiet_view(now_ms)
        return 0
    size = os.path.getsize(args.usage)
    offset = state["offset"] if size >= state["offset"] else 0
    with open(args.usage, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    new_offset = offset + len(chunk)
    text = chunk.decode("utf-8", "replace")
    if "\n" not in text:
        state["diag"] = {"reason": "无完整新行（offset=%d, size=%d）" % (offset, size), "at": now_ms}
        save_state()
        refresh_quiet_view(now_ms)
        log("无完整新行（offset=%d）" % offset, args.quiet)
        return 0
    lines = text.split("\n")
    tail = lines.pop()
    new_offset = offset + len(chunk) - len(tail.encode("utf-8"))
    scan_diag = {"scanned": 0, "success200": 0, "learnedFailures": 0, "ownSkipped": 0, "noModel": 0, "parseSkipped": 0, "agents": {}}

    learned = options.get("learned") if isinstance(options.get("learned"), dict) else {}
    images = dict(learned.get("images") if isinstance(learned.get("images"), dict) else {})
    # payload 上限学习（route-rules 侧"前置限流"）：413 行的输入 tokens 是该模型
    # 上限的上界证据；200 行的大输入是下界证据。中间件按估算输入量过滤候选。
    payload_caps = dict(learned.get("payloadCaps") if isinstance(learned.get("payloadCaps"), dict) else {})
    actions = list(learned.get("actions") if isinstance(learned.get("actions"), list) else [])
    # ignore-kind records are dedup bookkeeping, not enforcement: keep them in
    # the learner's own state, out of the middleware-visible options.
    ignore_sigs = dict(state.get("ignore_sigs") or {})
    for a in actions:
        if isinstance(a, dict) and a.get("kind") == "ignore" and a.get("signature"):
            ignore_sigs.setdefault(a["signature"], {"model": a.get("model"), "at": a.get("at") or int(time.time() * 1000)})
    actions = [a for a in actions if isinstance(a, dict) and a.get("kind") != "ignore"]
    known_sigs = {a.get("signature") for a in actions if isinstance(a, dict)}
    known_sigs.update(ignore_sigs.keys())
    ai_used = 0
    ai_errors = []
    updates = {}

    def find_action(model_key, kind):
        for a in actions:
            if isinstance(a, dict) and a.get("model") == model_key and a.get("kind") == kind:
                return a
        return None

    window_fails = {}
    # 已知模型全集（归因候选）：规则候选 + 既有画像键
    known_ids = set(targets) | set(perf.keys())
    for r in options.get("rules") or []:
        for c in [r.get("target")] + list(r.get("fallbacks") or []):
            if c:
                known_ids.add(c)

    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            scan_diag["parseSkipped"] += 1
            continue
        scan_diag["scanned"] += 1
        agent = str(row.get("agent") or "")
        if agent:
            scan_diag["agents"][agent] = scan_diag["agents"].get(agent, 0) + 1
        if USER_AGENT.split("/")[0] in agent or agent == "Python-urllib":
            scan_diag["ownSkipped"] += 1
            continue  # never learn from our own traffic
        status = row.get("status")
        if isinstance(status, str) and status.isdigit():
            status = int(status)
        provider = str(row.get("provider") or "")
        model = row.get("model")
        if not model:
            scan_diag["noModel"] += 1
            continue
        # 归因到实际服务模型：served 回显优先匹配已知候选（规则候选/已有画像），
        # 链内切换的请求性能记到真正干活的模型头上。
        model_key = resolve_attribution(row, provider, model, known_ids)

        # 499 = 客户端主动取消：不是服务失败，不进任何画像/失败计数（口径卫生，0.3.56）。
        if status == 499:
            scan_diag["canceled499"] = scan_diag.get("canceled499", 0) + 1
            continue

        # 桶归属用行时间戳而非扫描时间：扫描滞后（宕机/积压/offset 回退）时两者可差
        # 数小时，按扫描时间记桶会把旧行灌进当前桶（实测 16:49 的行进了上午桶）。
        row_ms = _usage_epoch(row.get("t"))
        row_ms = int(row_ms * 1000) if row_ms else now_ms

        # 内容依赖失败（请求侧问题）：输入超长 / 空回复且全耗在 reasoning（请求预算）。
        # 不算服务失败——只计样本量，不进连败/桶失败/重试风暴，避免误伤时段降权与熔断。
        err_l = str(row.get("err") or "").lower()
        empty_reply_budget = "empty reply" in err_l and "reasoning" in err_l
        input_too_long = status == 413 or bool(re.search(
            r"input too long|exc[ce]ed max input|max input length|input length.{0,40}\d{5,}|exceeds? the context (?:limit|length)", err_l))
        content_dependent = empty_reply_budget or input_too_long

        # 每个完成请求都进性能画像：吞吐率/首字节 EWMA + 连败计数（稳定性）。
        # consecFails = 服务失败口径（剔 499/内容依赖）；fails[] = pause 退避计数（仅
        # pause 判定累加）——两者都由 200 归零，但计数范围不同，勿混用（0.3.56 统一）。
        pstats = perf.setdefault(model_key, {"samples": 0, "consecFails": 0, "ewmaTps": None, "ewmaTtfbMs": None, "lastAt": 0})
        pstats["lastAt"] = row_ms
        pstats["samples"] = int(pstats.get("samples") or 0) + 1
        bkey = bucket_key(row_ms)
        pbuckets = pstats.setdefault("buckets", {})
        pb = pbuckets.setdefault(bkey, {"samples": 0, "ewmaTps": None, "ewmaTtfbMs": None, "consecFails": 0})
        pb["samples"] = int(pb.get("samples") or 0) + 1
        if status == 200:
            pstats["consecFails"] = 0
            pb["consecFails"] = 0
            out_n = row.get("out")
            ms_n = row.get("ms")
            ttft = row.get("ttft_ms")
            # TTFB 画像补齐（反思建议）：非流式行无 ttft_ms 时用总耗时当首字节样本——
            # 非流式下用户等的正是全程，ms 即体感知等待。
            if not (isinstance(ttft, (int, float)) and ttft > 0) and isinstance(ms_n, (int, float)) and ms_n > 0:
                ttft = ms_n
            if isinstance(out_n, (int, float)) and out_n > 0 and isinstance(ms_n, (int, float)) and ms_n > 0:
                tps = out_n / ms_n * 1000
                prev = pstats.get("ewmaTps")
                pstats["ewmaTps"] = round(tps, 1) if prev is None else round(prev * 0.7 + tps * 0.3, 1)
                # 快窗 EWMA（0.3/0.7 ≈ 最近 3 个样本）——与慢窗对比做趋势恶化检测
                prevf = pstats.get("fastTps")
                pstats["fastTps"] = round(tps, 1) if prevf is None else round(prevf * 0.3 + tps * 0.7, 1)
                prevb = pb.get("ewmaTps")
                pb["ewmaTps"] = round(tps, 1) if prevb is None else round(prevb * 0.7 + tps * 0.3, 1)
            if isinstance(ttft, (int, float)) and ttft > 0:
                prev = pstats.get("ewmaTtfbMs")
                pstats["ewmaTtfbMs"] = round(ttft) if prev is None else round(prev * 0.7 + ttft * 0.3)
                pstats["ttfbSamples"] = int(pstats.get("ttfbSamples") or 0) + 1
                prevb = pb.get("ewmaTtfbMs")
                pb["ewmaTtfbMs"] = round(ttft) if prevb is None else round(prevb * 0.7 + ttft * 0.3)
                pb["ttfbSamples"] = int(pb.get("ttfbSamples") or 0) + 1
        elif isinstance(status, int) and status >= 400 and not content_dependent:
            pstats["consecFails"] = int(pstats.get("consecFails") or 0) + 1
            pb["consecFails"] = int(pb.get("consecFails") or 0) + 1
            # 桶级失败上卷（反思建议）：失败按时段桶计数，供"失败集中"洞察
            pb["fails"] = int(pb.get("fails") or 0) + 1
            # 重试风暴感知（反思建议）：DSH 端一次失败会连发 5 次重试——
            # 同模型同窗口 ≥3 次失败 = 一次用户疼痛事件，应比散布失败更重。
            window_fails[model_key] = int(window_fails.get(model_key) or 0) + 1

        # payload 上限学习：HTTP 413 或错误文本里的输入超长（另一机器实证：
        # inferSrv 错误把 413 藏在 error_code 文本里，HTTP 状态不是 413）。
        # 内容依赖行不算服务失败（不进连败/桶失败），但仍是容量上界证据，必须学。
        if isinstance(status, int) and status >= 400 and input_too_long:
            # 优先从错误文本解析实际上超限的输入量（"current input length is N"），
            # 退回 usage 行的 in 字段
            in_tok = None
            mtok = re.search(r"current input length is (\d+)", err_l) or re.search(r"input length.{0,30}?(\d{5,})", err_l) or re.search(r"context (?:limit|length)[^0-9]{0,10}(\d{5,})", err_l)
            if mtok:
                in_tok = int(mtok.group(1))
            if not (isinstance(in_tok, (int, float)) and in_tok > 0):
                in_tok = row.get("in")
            if isinstance(in_tok, (int, float)) and in_tok > 0:
                pc = payload_caps.setdefault(model_key, {})
                mf = pc.get("minFailTok")
                pc["minFailTok"] = int(in_tok) if mf is None else min(mf, int(in_tok))
                pc["at"] = now_ms

        # 200 行的大输入 = 承载证据下界（与 413 上界夹逼真实上限）。
        # in 只记 cache-miss tokens：前缀缓存命中的大请求 in 会缩成几百，但完整
        # prompt 仍证明了模型装得下——证据必须用 in + cache_read（0.3.54）。
        in_tok_ok = (row.get("in") or 0) + (row.get("cache_read") or 0)
        if status == 200 and isinstance(in_tok_ok, (int, float)) and in_tok_ok > 1000:
            pc2 = payload_caps.setdefault(model_key, {})
            pc2["maxOkTok"] = max(pc2.get("maxOkTok") or 0, int(in_tok_ok))
            pc2["at"] = now_ms

        # A 200 from real traffic is the strongest recovery signal: clear any
        # fresh pause right away and reset the backoff counter (minimal
        # impact — a recovered model serves again the moment it can).
        if status == 200:
            scan_diag["success200"] += 1
            if fails.get(model_key):
                fails[model_key] = 0
            paused = find_action(model_key, "pause")
            if paused is not None:
                actions.remove(paused)
                updates[model_key] = {"kind": "resume"}
            success.setdefault(model_key, {"count": 0, "lastAt": 0})
            success[model_key]["count"] = int(success[model_key].get("count") or 0) + 1
            success[model_key]["lastAt"] = now_ms
            continue

        # ── auto-tune：从上游 400 错误约束自动调整 magpie 配置 ──
        if args.auto_tune and provider and status is not None and isinstance(status, int) and status >= 400:
            str_err = str(row.get("err") or "")
            if str_err and re.search(r'at most \d+ image', str_err, re.I):
                auto_tune_provider(provider, str_err, args.quiet)

        # Clamp and pause failures bypass the signature dedup: a repeated
        # clamp names a new ceiling (update in place), a repeated pause is
        # exactly the escalation signal the backoff needs. Other kinds dedup
        # by signature so the AI classifier is consulted once per novelty.
        # Everything below is wrapped: one bad row logs and skips, it must
        # never kill the whole scan (a crash here froze offset forever).
        try:
            clamp_err = re.search(r"expected a value\s*<=\s*(\d+)", str(row.get("err") or "").lower())
            sig = signature(model_key, row)
            image_failed = row_matches(row, statuses, keywords)

            verdict = None
            by = None
            if image_failed:
                if targets and model_key not in targets and model not in targets:
                    continue
                mark = {"state": "bad", "at": now_ms}
                if isinstance(images.get(model_key), dict) and images[model_key].get("via") == "describe":
                    mark["via"] = "describe"
                images[model_key] = mark
                updates[model_key] = {"kind": "skipImages"}
                by = "rule"
            else:
                verdict = rule_classify(row, clamp_skip)
                known = sig in known_sigs
                if verdict is None and known:
                    continue  # seen this novelty before; its action is still fresh
                if verdict is None and not args.no_ai and ai_used < args.ai_cap:
                    available = fetch_available_models(key)
                    candidates = ai_candidates(args, success, available, perf)
                    if candidates:
                        verdict, used, why = ai_classify(row, model_key, args, candidates)
                        ai_used += 1
                        if verdict is None:
                            ai_errors.append(why)
                            log("AI 分类失败（%s）：%s" % (model_key, why), args.quiet)
                        else:
                            log("AI 分类（%s 用 %s）：%s" % (model_key, used, json.dumps(verdict, ensure_ascii=False)), args.quiet)
                if verdict is None:
                    verdict = {"kind": "pause", "ttlMs": 0} if (
                        isinstance(status, int) and status >= 500) else {"kind": "ignore", "ttlMs": IGNORE_TTL_MS}
                    by = "default"
                else:
                    by = "rule"
            if verdict is None:
                continue
            if by is None:
                by = "ai"

            kind = verdict["kind"]
            if kind == "pause":
                # exponential backoff: 2min, 4min, 8min … capped; the counter
                # resets the moment a 200 proves recovery.
                duration = backoff_ms(fails.get(model_key, 0), args.pause_base, args.pause_max)
                fails[model_key] = int(fails.get(model_key, 0)) + 1
                existing = find_action(model_key, "pause")
                if existing is not None:
                    existing["at"] = now_ms
                    existing["ttlMs"] = duration
                    existing["by"] = by
                else:
                    action = {"model": model_key, "kind": "pause", "ttlMs": duration,
                              "at": now_ms, "by": by, "signature": sig}
                    if verdict.get("reason"):
                        action["reason"] = str(verdict["reason"])[:120]
                    actions.append(action)
                    known_sigs.add(sig)
                updates[model_key] = {"kind": "pause", "ttlMs": duration}
            elif kind == "clampOutput":
                cap = int(verdict.get("cap") or 0)
                existing = find_action(model_key, "clampOutput")
                if existing is not None:
                    # the newest error's ceiling wins; refresh so it stays fresh
                    existing["cap"] = cap
                    existing["at"] = now_ms
                    existing["ttlMs"] = max(int(verdict.get("ttlMs") or CLAMP_TTL_MS), existing.get("ttlMs") or 0)
                    existing["by"] = by
                else:
                    action = {"model": model_key, "kind": "clampOutput", "cap": cap,
                              "ttlMs": int(verdict.get("ttlMs") or CLAMP_TTL_MS),
                              "at": now_ms, "by": by, "signature": sig}
                    if verdict.get("reason"):
                        action["reason"] = str(verdict["reason"])[:120]
                    actions.append(action)
                    known_sigs.add(sig)
                updates[model_key] = {"kind": "clampOutput", "cap": cap}
            else:
                scan_diag["learnedFailures"] += 1
                action = {"model": model_key, "kind": "ignore", "ttlMs": int(verdict.get("ttlMs") or IGNORE_TTL_MS),
                          "at": now_ms, "by": by, "signature": sig}
                if verdict.get("reason"):
                    action["reason"] = str(verdict["reason"])[:120]
                actions.append(action)
                known_sigs.add(sig)
        except Exception as err:
            scan_diag["rowErrors"] = scan_diag.get("rowErrors", 0) + 1
            log("行学习失败（%s）：%s" % (model_key, err), args.quiet)
            continue

    # ── 重试风暴感知（反思建议）：同模型本轮 ≥3 次失败 = DSH 重试爆发 ──
    # 一次用户疼痛事件；额外 +1 退避计数加速熔断，避免散布失败同权重。
    burst_models = [m for m, n in window_fails.items() if n >= 3]
    for m in burst_models:
        fails[m] = int(fails.get(m, 0)) + 1

    # ── 慢+失败联合阈值自动触发（0.3.56）：单一时段降权之外，联合证据直接短熔断 ──
    # 当前桶服务失败率 ≥30%（样本 ≥5）且吞吐趋势恶化（快窗 < 0.6×慢窗，样本 ≥5）
    # → 10 分钟 pause（自动过期，任何 200 立即解除）。单独信号都不足以触发。
    joint_bkey = bucket_key(now_ms)
    for m, p in perf.items():
        b = (p.get("buckets") or {}).get(joint_bkey) or {}
        bf, bs = b.get("fails") or 0, b.get("samples") or 0
        rate = (bf / bs) if bs > 0 else 0
        ft, et = p.get("fastTps"), p.get("ewmaTps")
        trend_bad = isinstance(ft, (int, float)) and isinstance(et, (int, float)) and et >= 5 \
            and (p.get("samples") or 0) >= 5 and ft < et * 0.6
        if bs >= 5 and rate >= 0.30 and trend_bad and find_action(m, "pause") is None:
            actions.append({"model": m, "kind": "pause", "ttlMs": 10 * 60 * 1000, "at": now_ms,
                            "by": "joint",
                            "signature": "joint_%s_%s" % (m, joint_bkey),
                            "reason": "慢+失败联合：桶失败率 %.0f%%（%d/%d）且快窗吞吐 %.1f < 0.6×慢窗 %.1f"
                                      % (rate * 100, bf, bs, ft, et)})
            updates[m] = {"kind": "joint-pause"}
            log("联合熔断：%s 当前桶失败率 %.0f%%（%d/%d）且吞吐趋势恶化 → 暂停 10 分钟" % (m, rate * 100, bf, bs), args.quiet)
    # keep the success map bounded: most recent first
    success = dict(sorted(success.items(), key=lambda kv: -(kv[1].get("lastAt") or 0))[:SUCCESS_KEEP])
    # prune: expired actions, oversized list, stale image marks
    actions = [a for a in actions if isinstance(a, dict) and (now_ms - (a.get("at") or 0)) < (a.get("ttlMs") or 600000)]
    actions = sorted(actions, key=lambda a: -(a.get("at") or 0))[:200]
    for key in list(images.keys()):
        cur = images[key]
        if targets and key not in targets:
            del images[key]
            continue
        if isinstance(cur, dict) and cur.get("state") == "bad" and (now_ms - (cur.get("at") or 0)) >= image_bad_window(cur, args.ttl):
            del images[key]

    # ── 主动探测（probes）：早恢复发现 + 能力主动试探，替代被动等流量 ──
    # 每轮最多 3 个探针（真实小请求，成本可控）。
    if args.auto_tune and not args.dry_run and key:
        probes = 0
        # (a) pause 早恢复探测：熔断过半即主动试探，200 则提前解除
        for a in list(actions):
            if probes >= 3:
                break
            if a.get("kind") == "pause":
                elapsed = now_ms - (a.get("at") or 0)
                if elapsed > (a.get("ttlMs") or 0) * 0.5 and probe_model(key, a.get("model")):
                    actions.remove(a)
                    fails[a.get("model")] = 0
                    perf_model = perf.get(a.get("model"))
                    if perf_model:
                        perf_model["consecFails"] = 0
                    updates[a.get("model")] = {"kind": "resume-probe"}
                    log("probe: %s 提前恢复（熔断解除）" % a.get("model"), args.quiet)
                    probes += 1
        # (b) 图片能力主动试探：未知能力的候选主动发小图确认，
        #     把"未知→试错等真实流量"缩短为"本轮即知"。
        for rule in options.get("rules") or []:
            if probes >= 3:
                break
            if rule.get("type") != "images":
                continue
            cands = [rule.get("target")] + list(rule.get("fallbacks") or [])
            for m in cands:
                if probes >= 3 or not m:
                    continue
                img = images.get(m)
                unknown = not img
                expired_bad = (isinstance(img, dict) and img.get("state") == "bad"
                               and (now_ms - (img.get("at") or 0)) >= image_bad_window(img, args.ttl))
                if not (unknown or expired_bad):
                    continue
                res = probe_image_capability(key, m, cands)
                if res["via"] == "describe":
                    images[res["who"]] = {"state": "bad", "via": "describe", "at": now_ms}
                    updates[res["who"]] = {"kind": "probe-images"}
                    log("probe: %s 图片能力=不可用（主动试探：%s，原生无视觉）" % (
                        res["who"], "200 走代看链路" if res["ok"] else "上游 can't-see-images"), args.quiet)
                elif res["ok"] and res["who"] == m:
                    images[m] = {"state": "good", "at": now_ms}
                    updates[m] = {"kind": "probe-images"}
                    log("probe: %s 图片能力=可用（主动试探：原生视觉）" % m, args.quiet)
                elif res["ok"]:
                    log("probe: %s 被改写至 %s，能力未判定（跳过）" % (m, res["served"]), args.quiet)
                else:
                    images[res["who"]] = {"state": "bad", "at": now_ms}
                    updates[res["who"]] = {"kind": "probe-images"}
                    log("probe: %s 图片能力=不可用（主动试探：请求失败）" % m, args.quiet)
                probes += 1

    # ── (c) 冷死探活（0.3.56）：有画像但 >24h 无流量的模型每天一次小 ping ──
    # 防"无流量→无样本→永不参与重排"的路由冷死自我强化；每轮至多 1 个。
    if args.auto_tune and not args.dry_run and key:
        stale_probes = state.setdefault("staleProbes", {})
        for m, p in list(perf.items()):
            age_h = (now_ms - (p.get("lastAt") or 0)) / 3600000.0
            if age_h > 24 and now_ms - (stale_probes.get(m) or 0) >= 24 * 3600 * 1000:
                ok = probe_model(key, m)
                stale_probes[m] = now_ms
                log("probe: %s 冷死探活%s（%.0fh 无流量）" % (m, "成功" if ok else "失败", age_h), args.quiet)
                break
    # ── 周期反思：审视学到的状态，发现过时/矛盾/覆盖缺口 ──
    insights = reflect_on_state(options.get("rules") or [], images, actions, perf, fails,
                                state.get("ignore_sigs") or {}, now_ms, args.ttl)
    state["insights"] = insights[:10]

    # ── AI 深度反思（--reflect）：验证规则洞察 + 发现未覆盖的问题 ──
    if args.reflect and not args.no_ai:
        available = fetch_available_models(key)
        candidates = ai_candidates(args, success, available, perf)
        recent_rows_text = "\n".join(lines[-max(1, args.explain_rows):])
        print("== AI 深度反思 ==")
        narrative, used, err = ai_deep_reflect(args, perf, success, images, actions,
                                               insights, recent_rows_text, candidates)
        if narrative is None:
            print("（AI 深度反思不可用：%s）" % err)
        else:
            print("（反思模型：%s）" % used)
            print(narrative)
            state["lastReflect"] = {"at": now_ms, "model": used, "text": narrative[:8000]}
        state["lastAiReflectAt"] = now_ms

    # ── AI 深度反思自动触发：距上次 ≥24h 且本轮有新内容 ──
    # 触发条件=洞察 or 学习更新 or 本轮扫过新流量行（AI 反思的价值包括
    # 从数据发现规则没覆盖的趋势，不等规则洞察非空）；完全无流量才跳过。
    elif not args.no_ai and not args.dry_run and key:
        last_ar = state.get("lastAiReflectAt") or 0
        if (now_ms - last_ar) >= 24 * 3600 * 1000 and (insights or updates or scan_diag.get("scanned")):
            try:
                available = fetch_available_models(key)
                candidates = ai_candidates(args, success, available, perf)
                recent_rows_text = "\n".join(lines[-max(1, args.explain_rows):])
                narrative, used, err = ai_deep_reflect(args, perf, success, images, actions,
                                                       insights, recent_rows_text, candidates)
                if narrative:
                    state["lastReflect"] = {"at": now_ms, "model": used, "text": narrative[:8000]}
                    log("AI 深度反思自动完成（%s）——洞察 %d 条" % (used, len(insights)), args.quiet)
                state["lastAiReflectAt"] = now_ms
            except Exception as err:
                log("AI 深度反思自动触发失败：%s" % err, args.quiet)

    # ── 能力标记 → 性能画像联动（capabilityDegraded）──
    # images bad（新鲜）→ 该模型能力降级。文本流量会持续清零 consecFails，
    # 只看连败计数会把图片能力缺陷藏在健康假象后面——独立字段让降级在
    # 画像中显式可见，供 --learned 展示、AI 反思与重排联动消费。
    for m, pstats in perf.items():
        img = images.get(m)
        degraded = isinstance(img, dict) and img.get("state") == "bad" and (now_ms - (img.get("at") or 0)) < image_bad_window(img, args.ttl)
        if degraded:
            pstats["capabilityDegraded"] = "images"
        else:
            pstats.pop("capabilityDegraded", None)

    state["success"] = success
    state["fails"] = fails
    # ── 死模型修剪（用户要求：无法使用的模型不再出现在页面/分析中）──
    # 修剪条件（保守）：既不是任何规则的候选，也（已不在 /v1/models 或 lastAt 超 7 天）。
    # 规则候选永远保留（无流量也仍是路由对象）；目录里还提供的也保留。
    try:
        available_ids = set(fetch_available_models(key) or []) if key else None
    except Exception:
        available_ids = None
    rule_cand_ids = set()
    for r in options.get("rules") or []:
        for m in [r.get("target")] + list(r.get("fallbacks") or []):
            if m:
                rule_cand_ids.add(m)
    for m in list(perf.keys()):
        if m in rule_cand_ids:
            continue
        p = perf[m]
        if (p.get("samples") or 0) == 0 and not p.get("lastAt"):
            del perf[m]
            continue
        in_catalog = available_ids is None or m in available_ids
        age_ms = now_ms - (p.get("lastAt") or 0)
        if not in_catalog or age_ms > 7 * 24 * 3600 * 1000:
            del perf[m]
            log("修剪不可用模型的画像：%s（不在目录=%s，%.0f 天无流量）"
                % (m, "是" if not in_catalog else "否", age_ms / 86400000), args.quiet)

    perf_bounded = dict(sorted(perf.items(), key=lambda kv: -(kv[1].get("lastAt") or 0))[:30])
    state["perf"] = perf_bounded
    state["diag"] = dict(scan_diag)
    state["diag"]["lastRunAt"] = now_ms

    # ── 体验自适应：按实测评分重排 images 规则的候选顺序（迟滞防抖）──
    # 稳定（无连败）且吞吐率高者优先；现任与挑战者差距 <15% 不换（防抖）。
    options_dirty = False
    cur_bkey = bucket_key(now_ms)
    for rule in options.get("rules") or []:
        if rule.get("type") != "images":
            continue
        cands = [rule.get("target")] + list(rule.get("fallbacks") or [])
        cands = [m for m in cands if m]
        # images 专属：图片标记仍有效的模型不参与换头——文本吞吐再高，把盲模型
        # 换到 images 规则头部只会把带图流量推回代看链路（0.3.45）。
        if rule.get("type") == "images":
            eligible = [m for m in cands
                        if not (isinstance(images.get(m), dict)
                                and images[m].get("state") == "bad"
                                and (now_ms - (images[m].get("at") or 0)) < image_bad_window(images[m], args.ttl))]
            if len(eligible) < 2:
                continue  # 视觉健康候选不足两个：保持现状（含无分数的头名），不换头
            cands = eligible
        # 首字节样本不足的评分不含时延惩罚（偏高）——不可信者不参与换头（0.3.56）：
        # 现任不可信 → 本轮暂缓换头，等样本积累；挑战者不可信 → 不进 known_scored。
        head_p = perf.get(cands[0])
        if head_p and (head_p.get("samples") or 0) >= 5 and (head_p.get("ttfbSamples") or 0) < 3:
            continue
        scored = []
        for m in cands:
            pstats = perf.get(m)
            # 重排样本门槛 ≥5（AI 反思：18 样本与 1588 样本跨模型比较噪音大，
            # 原 ≥2 门槛太宽；桶内分另有 ≥3 门槛）
            if pstats and (pstats.get("samples") or 0) >= 5:
                s = perf_score_now(pstats, now_ms)
                # 首字节样本不足 → 评分不含时延惩罚（偏高）：不可信者不得挑战争位
                if s is not None and (pstats.get("ttfbSamples") or 0) < 3:
                    s = None
                # 能力标记 → 路由可用性联动：能力降级的成员在此类规则中大幅降权
                # （capabilityDegraded 类型与规则类型一致才降权——images 降级只影响 images 规则）
                if s is not None and pstats.get("capabilityDegraded") == rule.get("type"):
                    s = round(s * 0.1, 1)
            else:
                s = None
            scored.append((m, s))
        known_scored = [x for x in scored if x[1] is not None]
        if not known_scored or len(cands) < 2:
            continue
        known_scored.sort(key=lambda t: -t[1])
        best_m, best_s = known_scored[0]
        cur_m = cands[0]
        cur_s = next((s for m, s in known_scored if m == cur_m), None)
        if best_m == cur_m:
            continue
        if cur_s is not None and best_s < cur_s * 1.15:
            continue  # 挑战者优势不足 15%，不换（防抖）
        new_order = [best_m] + [m for m in cands if m != best_m]
        rule["target"] = best_m
        rule["fallbacks"] = new_order[1:]
        log("images 规则自适应调整：%s → %s（评分 %.1f，现任 %.1f）" % (cur_m, best_m, best_s, cur_s or 0), args.quiet)
        options_dirty = True
    if options_dirty:
        backup(args.plugins)
        entry["options"] = options
        atomic_write_json(args.plugins, plugins)
    # ── DSH 容量回写（0.3.52）：真实窗口写进 DSH 配置，自动 compact 在正确位置触发 ──
    if not args.no_dsh_writeback:
        writeback_dsh_windows(payload_caps, state, args.quiet, dry_run=args.dry_run)
    new_offset_state = dict(state)
    new_offset_state["offset"] = new_offset
    if args.dry_run:
        log("dry-run: updates=%s actions=%d images=%s offset=%d ai=%d fails=%s" % (
            json.dumps(updates), len(actions), json.dumps(images), new_offset, ai_used, json.dumps(fails)), args.quiet)
        return 0

    # ── GUI 可见性：人读视图写进 options.learnedView（magpie 插件页直接可看）──
    # learnedView 是功能性 learned 的兄弟键：中间件只读 learned.*，不受影响。
    # ── 效果量化（每小时重算 7 天窗口，避免每 5 分钟扫全量日志）──
    last_impact_at = state.get("lastImpactScan") or 0
    if now_ms - last_impact_at >= 3600 * 1000:
        impact = compute_impact(args.usage, now_ms)
        if impact:
            state["impact"] = impact
            state["lastImpactScan"] = now_ms
            log("效果量化已更新：7d %d 请求 / 切换保障 %d 次（%.1f%%）/ 可用度 %.1f%%"
                % (impact["requests"], impact["saves"], impact["switchGuaranteedPct"], impact["availability"]), args.quiet)

    learned_view = build_learned_view(perf, images, actions, insights, state.get("lastReflect"), now_ms)
    learned_view = with_impact(learned_view, state)
    view_changed = learned_view != options.get("learnedView")
    changed = actions != (learned.get("actions") if isinstance(learned.get("actions"), list) else []) \
        or images != (learned.get("images") if isinstance(learned.get("images"), dict) else {}) \
        or payload_caps != (learned.get("payloadCaps") if isinstance(learned.get("payloadCaps"), dict) else {}) \
        or view_changed
    if changed:
        backup(args.plugins)
        options["learned"] = {"images": images, "actions": actions, "payloadCaps": payload_caps}
        options["learnedView"] = learned_view
        entry["options"] = options
        atomic_write_json(args.plugins, plugins)
        log("已写入学习结果：actions=%d images=%s ai=%d%s" % (
            len(actions), json.dumps(images), ai_used,
            ("；AI 错误: " + "; ".join(ai_errors[:2])) if ai_errors else ""), args.quiet)
    atomic_write_json(args.state, new_offset_state)
    if not actions and not updates:
        log("无新的失败样本（offset=%d）" % new_offset, args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
