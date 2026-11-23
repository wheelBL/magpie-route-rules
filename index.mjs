// Bun-host entry for magpie: keeps the route-learn companion alive.
//
// magpie restarts the plugin host whenever a plugin is added, removed,
// updated or toggled, and loads this file as the package's server side —
// so this is the one place that runs right after `magpie plugin update`
// (the GUI's Update all included). The companion scheduled task is what
// actually learns; without this, a machine that installed the plugin but
// never ran `--bootstrap` silently never learns, and GUI updates never
// reach a task that points at a stale copy elsewhere.
//
// What it does, idempotently, in well under a second:
//   1. finds the installed package's own tools/route-learn.py;
//   2. reads the existing "Magpie route learn" task's command;
//   3. if the task is missing, or does not run that script, (re)creates it
//      every 5 minutes pointing at the package copy — so plugin updates
//      flow straight into the next run.
//
// No provider hooks are returned: this package's gateway behaviour lives
// in route-rules.middleware.js; this entry exists only for its load-time
// side effect. Everything fails soft — a broken check must never disturb
// the host that loads this module.

import { join } from "node:path"

const TASK_NAME = "Magpie route learn"

function run(args) {
  const proc = Bun.spawnSync(args, { stdout: "pipe", stderr: "pipe" })
  return {
    code: proc.exitCode,
    out: (proc.stdout?.toString() ?? "") + (proc.stderr?.toString() ?? ""),
  }
}

function currentTaskCommand() {
  const q = run(["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "LIST"])
  if (q.code !== 0) return null
  const line = q.out.split(/\r?\n/).find((l) => /Task To Run|要运行的任务/i.test(l))
  // split at the FIRST colon only (JS split(sep, limit) discards the rest,
  // which bit us once: the drive colon in C:\ truncated the command to `"C`)
  if (!line) return ""
  const cut = line.indexOf(":")
  return cut === -1 ? "" : line.slice(cut + 1).trim()
}

function ensureCompanion() {
  if (process.platform !== "win32") return // schtasks is Windows-only; cron/launchd welcome
  const script = join(import.meta.dir, "tools", "route-learn.py")

  const existing = currentTaskCommand()
  if (existing && existing.toLowerCase().includes(script.toLowerCase())) return // already ours and current

  // Keep the interpreter the task already used (it was discovered at
  // bootstrap time); otherwise settle for pythonw/python from PATH.
  let python = null
  if (existing) {
    const m = existing.match(/"([^"]+\.exe)"/i)
    if (m) python = m[1]
  }
  python ??= Bun.which("pythonw") ?? Bun.which("python")
  if (!python) {
    console.log("route-rules companion: no python found on PATH; task left as is")
    return
  }
  const tr = `"${python}" "${script}" --quiet`
  const created = run(["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr, "/sc", "minute", "/mo", "5", "/f"])
  if (created.code === 0) {
    // a fresh install or repoint must not sit out the scheduler's up-to-
    // 5-minute silence: fire the first learning pass right now
    const fired = run(["schtasks", "/run", "/tn", TASK_NAME])
    console.log(`route-rules companion: task ${existing ? "repointed" : "registered"} -> ${script}`
      + (fired.code === 0 ? "；已立即触发首轮学习" : "；立即触发失败，等下一 5 分钟节拍"))
  } else {
    console.log(`route-rules companion: schtasks failed (${created.code}): ${created.out.trim().slice(0, 160)}`)
  }
}

export const RouteRulesCompanion = async () => {
  try {
    ensureCompanion()
  } catch (error) {
    console.log("route-rules companion check failed:", error?.message ?? error)
  }
  return {}
}
