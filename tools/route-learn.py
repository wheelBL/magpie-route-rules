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


def ai_candidates(args, success, available):
    """Ordered classification-model candidates: explicit override first, then
    recently-successful models (most recent first), then anything listed."""
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
    for m in recent:
        push(m)
    for m in available or []:
        push(m)
    return candidates


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


def backoff_ms(fails_count, pause_base, pause_max):
    return min(int(pause_base) * (2 ** max(0, int(fails_count))), int(pause_max))


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
    ap.add_argument("--classify", metavar="TEXT", help="classify one error text with the AI and print the verdict")
    ap.add_argument("--bootstrap", action="store_true", help="fresh machine: seed rules from /v1/models + register the learning task")
    ap.add_argument("--learned", action="store_true", help="print what has been learned (images / actions / counters), change nothing")
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

    state = {"usage": args.usage, "offset": 0, "success": {}, "fails": {}, "diag": {}}
    try:
        raw = load_json(args.state)
        if isinstance(raw, dict) and raw.get("usage") == args.usage:
            state["offset"] = int(raw.get("offset") or 0)
            if isinstance(raw.get("success"), dict):
                state["success"] = raw["success"]
            if isinstance(raw.get("fails"), dict):
                state["fails"] = raw["fails"]
            if isinstance(raw.get("diag"), dict):
                state["diag"] = raw["diag"]
    except Exception:
        pass
    success = state["success"]
    fails = state["fails"]

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

    now_ms = int(time.time() * 1000)
    if not os.path.exists(args.usage):
        reason = "usage 文件不存在：%s（检查 magpie 配置目录是否在 ~/.config/magpie）" % args.usage
        log(reason, args.quiet)
        state["diag"] = {"reason": reason, "at": now_ms}
        save_state()
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
        log("无完整新行（offset=%d）" % offset, args.quiet)
        return 0
    lines = text.split("\n")
    tail = lines.pop()
    new_offset = offset + len(chunk) - len(tail.encode("utf-8"))
    scan_diag = {"scanned": 0, "success200": 0, "learnedFailures": 0, "ownSkipped": 0, "noModel": 0, "parseSkipped": 0, "agents": {}}

    learned = options.get("learned") if isinstance(options.get("learned"), dict) else {}
    images = dict(learned.get("images") if isinstance(learned.get("images"), dict) else {})
    actions = list(learned.get("actions") if isinstance(learned.get("actions"), list) else [])
    known_sigs = {a.get("signature") for a in actions if isinstance(a, dict)}
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
                    candidates = ai_candidates(args, success, available)
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

    state["success"] = success
    state["fails"] = fails
    state["diag"] = dict(scan_diag)
    state["diag"]["lastRunAt"] = now_ms
    new_offset_state = dict(state)
    new_offset_state["offset"] = new_offset
    if args.dry_run:
        log("dry-run: updates=%s actions=%d images=%s offset=%d ai=%d fails=%s" % (
            json.dumps(updates), len(actions), json.dumps(images), new_offset, ai_used, json.dumps(fails)), args.quiet)
        return 0

    changed = actions != (learned.get("actions") if isinstance(learned.get("actions"), list) else []) \
        or images != (learned.get("images") if isinstance(learned.get("images"), dict) else {})
    if changed:
        backup(args.plugins)
        options["learned"] = {"images": images, "actions": actions}
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
