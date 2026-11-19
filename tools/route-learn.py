#!/usr/bin/env python3
"""Learn how to handle model failures from magpie's usage log and feed the
learned actions to the magpie-route-rules gateway middleware.

Two learning channels:
1. Image capability (0.2.0): a model whose image request fails (406 /
   multimodal-flavoured errors) is marked bad for a TTL in
   options.learned.images — later image requests skip it. The TTL is the
   half-open probe: when it expires, traffic flows again and real outcomes
   re-teach the mark.
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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

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
DSH_ENV = os.path.join(os.path.expanduser("~"), ".dsh", ".env")
DEFAULT_PAUSE_BASE = 2 * 60 * 1000
DEFAULT_PAUSE_MAX = 60 * 60 * 1000
PAUSE_TTL_MS = 10 * 60 * 1000
IGNORE_TTL_MS = 30 * 24 * 3600 * 1000
CLAMP_TTL_MS = 24 * 3600 * 1000
SUCCESS_KEEP = 20


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


def perf_score(pstats):
    """体验综合分：稳定性（连败惩罚）× 速度（吞吐率 EWMA）。样本不足返回 None。"""
    if not pstats or (pstats.get("samples") or 0) < 2:
        return None
    cf = pstats.get("consecFails") or 0
    tps = pstats.get("ewmaTps") or 0
    return round(tps / (1 + cf), 1)


def time_bucket(now=None):
    """(周末?, 时段桶0-3)：上游负载有时段规律（晚高峰/工作时段），
    分桶统计让评分贴合'现在这个时间'的真实表现而非全天混合均值。"""
    t = time.localtime(time.time() if now is None else now)
    return (1 if t.tm_wday >= 5 else 0), t.tm_hour // 6


def bucket_key(now_ms=None):
    wk, hb = time_bucket(None if now_ms is None else now_ms / 1000)
    return "%d_%d" % (wk, hb)


def perf_score_bucket(pstats, bkey):
    """时段桶内评分：样本 ≥3 且有吞吐率数据时优先于全局分（更贴近当下）。"""
    b = (pstats.get("buckets") or {}).get(bkey)
    if b and (b.get("samples") or 0) >= 3 and b.get("ewmaTps"):
        return round(b["ewmaTps"] / (1 + (b.get("consecFails") or 0)), 1)
    return None


def perf_score_now(pstats, now_ms=None):
    """时段感知评分：桶内样本足用桶内分，否则退回全局分。"""
    if not pstats:
        return None
    return perf_score_bucket(pstats, bucket_key(now_ms)) or perf_score(pstats)


TINY_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def probe_model(key, model, images=False, timeout=90):
    """Tiny probe request: True = healthy (200). Used for early pause-recovery
    detection and proactive image-capability discovery."""
    if images:
        content = [{"type": "text", "text": "这张图是什么颜色"},
                   {"type": "image_url", "image_url": {"url": TINY_PNG}}]
    else:
        content = "ping"
    payload = {"model": model, "messages": [{"role": "user", "content": content}],
               "max_tokens": 16, "stream": False}
    req = urllib.request.Request(GATEWAY, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


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
    "- clampOutput：请求参数超限（如 max_tokens 超过模型实际上限），把输出预算钳制到 cap\n"
    "- ignore：客户端取消、一次性抖动或无需处理\n"
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
    return ai_chat(messages, args, candidates, max_tokens=2000)


def compute_impact(usage_path, now_ms, window_days=7):
    """从 usage 历史量化插件效果（7 天滚动窗口）：
    - saves: model≠served 且 200 = 透明切换挽救的请求（预算切换/流重试/链重试）
    - availability: 实际成功率；availabilityNoPlugin: 估计无插件成功率（把挽救算作会失败）
    - medianTtftS/medianDurationS: 200 请求的首字节/总耗时中位数（秒）。"""
    import datetime
    window_ms = window_days * 24 * 3600 * 1000

    def norm_model(s):
        return str(s).lower().replace(".", "-").replace("_", "-")

    def same_model(a, b):
        na, nb = norm_model(a), norm_model(b)
        return na == nb or na.startswith(nb) or nb.startswith(na)

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
            if "route-learn" in agent or agent == "Python-urllib":
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
    return {
        "windowDays": window_days,
        "requests": total,
        "saves": saves,
        "savesDetail": dict(sorted(switch_pairs.items(), key=lambda kv: -kv[1])[:5]),
        "availability": round(100.0 * ok / total, 1),
        "availabilityNoPlugin": round(100.0 * max(0, ok - saves) / total, 1),
        "medianTtftS": med(ttfts),
        "medianDurationS": med(durations),
    }


def build_learned_view(perf, images, actions, insights, last_reflect, now_ms):
    """人读视图：伴生写进 options.learnedView，magpie 插件页渲染。"""
    perf_summary = []
    for m, p in perf.items():
        s = perf_score_now(p, now_ms)
        ttf_ms0 = p.get("ewmaTtfbMs")
        e0 = {"model": m, "score": s, "tps": p.get("ewmaTps"),
              "ttfS": round(ttf_ms0 / 1000, 1) if isinstance(ttf_ms0, (int, float)) else None,
              "samples": p.get("samples"), "consecFails": p.get("consecFails")}
        if p.get("capabilityDegraded"):
            e0["degraded"] = p["capabilityDegraded"]
        perf_summary.append(e0)
    perf_summary.sort(key=lambda e: -(e["score"] if e["score"] is not None else -1))
    view = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
        "perfSummary": perf_summary[:8],
        "insights": insights[:10],
        "imagesView": {m: {"state": v.get("state"), "ageMin": round((now_ms - (v.get("at") or 0)) / 60000)}
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
                     and (now_ms - (images[m].get("at") or 0)) < image_ttl_ms]
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
            left_min = round(((entry0.get("at") or 0) + image_ttl_ms - now_ms) / 60000)
            if 0 < left_min <= 30:
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
    # register the learning scheduled task (Windows)
    task_note = ""
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    tr = '"%s" "%s" --quiet' % (pythonw, os.path.abspath(__file__))
    try:
        probe = subprocess.run(["schtasks", "/query", "/tn", "Magpie route learn"],
                               capture_output=True, text=True, timeout=20)
        if probe.returncode != 0:
            subprocess.run(["schtasks", "/create", "/tn", "Magpie route learn", "/tr", tr,
                            "/sc", "minute", "/mo", "5", "/f"], check=True, timeout=20)
            task_note = "；已注册计划任务（每 5 分钟学习）"
        else:
            task_note = "；计划任务已存在"
    except Exception as err:
        task_note = "；计划任务注册失败（%s），可手动注册" % err
    return summary + task_note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-ai", action="store_true", help="unknown signatures get the conservative default instead of an AI verdict")
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
            left_min = max(0, round(((entry0.get("at") or 0) + args.ttl - now) / 60000))
            age_min = round((now - (entry0.get("at") or 0)) / 60000)
            if state0 == "bad":
                print("  %s: 不可用（%d 分钟前失败，%d 分钟后过期恢复试探）→ 带图请求跳过" % (model, age_min, left_min))
            elif state0 == "good":
                print("  %s: 可用（正学习确认）" % model)
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
                options["learned"] = {"images": images0, "actions": actions0}
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
        model_key = "%s/%s" % (provider, model) if provider else model

        # 每个完成请求都进性能画像：吞吐率/首字节 EWMA + 连败计数（稳定性）。
        # 200 归零连败（自恢复），>=400 累加。
        pstats = perf.setdefault(model_key, {"samples": 0, "consecFails": 0, "ewmaTps": None, "ewmaTtfbMs": None, "lastAt": 0})
        pstats["lastAt"] = now_ms
        pstats["samples"] = int(pstats.get("samples") or 0) + 1
        bkey = bucket_key(now_ms)
        pbuckets = pstats.setdefault("buckets", {})
        pb = pbuckets.setdefault(bkey, {"samples": 0, "ewmaTps": None, "ewmaTtfbMs": None, "consecFails": 0})
        pb["samples"] = int(pb.get("samples") or 0) + 1
        if status == 200:
            pstats["consecFails"] = 0
            pb["consecFails"] = 0
            out_n = row.get("out")
            ms_n = row.get("ms")
            ttft = row.get("ttft_ms")
            if isinstance(out_n, (int, float)) and out_n > 0 and isinstance(ms_n, (int, float)) and ms_n > 0:
                tps = out_n / ms_n * 1000
                prev = pstats.get("ewmaTps")
                pstats["ewmaTps"] = round(tps, 1) if prev is None else round(prev * 0.7 + tps * 0.3, 1)
                prevb = pb.get("ewmaTps")
                pb["ewmaTps"] = round(tps, 1) if prevb is None else round(prevb * 0.7 + tps * 0.3, 1)
            if isinstance(ttft, (int, float)) and ttft > 0:
                prev = pstats.get("ewmaTtfbMs")
                pstats["ewmaTtfbMs"] = round(ttft) if prev is None else round(prev * 0.7 + ttft * 0.3)
                prevb = pb.get("ewmaTtfbMs")
                pb["ewmaTtfbMs"] = round(ttft) if prevb is None else round(prevb * 0.7 + ttft * 0.3)
        elif isinstance(status, int) and status >= 400:
            pstats["consecFails"] = int(pstats.get("consecFails") or 0) + 1
            pb["consecFails"] = int(pb.get("consecFails") or 0) + 1

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
                images[model_key] = {"state": "bad", "at": now_ms}
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
        if isinstance(cur, dict) and cur.get("state") == "bad" and (now_ms - (cur.get("at") or 0)) >= args.ttl:
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
                if elapsed > (a.get("ttlMs") or 0) * 0.5 and probe_model(key, a.get("model"), images=False):
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
            for m in [rule.get("target")] + list(rule.get("fallbacks") or []):
                if probes >= 3 or not m:
                    continue
                img = images.get(m)
                unknown = not img
                expired_bad = isinstance(img, dict) and img.get("state") == "bad" and (now_ms - (img.get("at") or 0)) >= args.ttl
                if unknown or expired_bad:
                    ok = probe_model(key, m, images=True)
                    images[m] = {"state": "good" if ok else "bad", "at": now_ms}
                    updates[m] = {"kind": "probe-images"}
                    log("probe: %s 图片能力=%s（主动试探）" % (m, "可用" if ok else "不可用"), args.quiet)
                    probes += 1

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
        degraded = isinstance(img, dict) and img.get("state") == "bad" and (now_ms - (img.get("at") or 0)) < args.ttl
        if degraded:
            pstats["capabilityDegraded"] = "images"
        else:
            pstats.pop("capabilityDegraded", None)

    state["success"] = success
    state["fails"] = fails
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
        scored = []
        for m in cands:
            pstats = perf.get(m)
            if pstats and (pstats.get("samples") or 0) >= 2:
                s = perf_score_now(pstats, now_ms)
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
            log("效果量化已更新：7d %d 请求 / 挽救 %d / 可用度 %.1f%%（估计无插件 %.1f%%）"
                % (impact["requests"], impact["saves"], impact["availability"], impact["availabilityNoPlugin"]), args.quiet)

    learned_view = build_learned_view(perf, images, actions, insights, state.get("lastReflect"), now_ms)
    learned_view = with_impact(learned_view, state)
    view_changed = learned_view != options.get("learnedView")
    changed = actions != (learned.get("actions") if isinstance(learned.get("actions"), list) else []) \
        or images != (learned.get("images") if isinstance(learned.get("images"), dict) else {}) \
        or view_changed
    if changed:
        backup(args.plugins)
        options["learned"] = {"images": images, "actions": actions}
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
