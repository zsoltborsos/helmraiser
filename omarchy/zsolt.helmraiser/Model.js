// Pure helpers for the Helmraiser bar widget: where status.json lives, how a
// status payload is read, and the strings the bar and panel show. Qt-free so
// `node --test tests/` can exercise it; the QML imports it as `Model`.

// nf-md-airplane_takeoff (U+F05D5) from the Nerd Font the bar already uses;
// the built-in Agents widget already shows a robot (U+F16A3).
var GLYPH = "󰗕"

var DEFAULT_REFRESH_SEC = 30
var MIN_REFRESH_SEC = 5
var DRAFT_DROP_REASON = "dropped from the Helmraiser widget"

// Mirrors helmraiser.loop.status.status_path: $XDG_RUNTIME_DIR/helmraiser, else
// $HELMRAISER_DATA_DIR (default ~/.helmraiser). Python also checks that the
// runtime dir exists; a set XDG_RUNTIME_DIR is taken on trust here, and the
// widget switches to the `status_path` the CLI reports once it has run.
function statusPath(env) {
  env = env || {}
  if (env.XDG_RUNTIME_DIR) return env.XDG_RUNTIME_DIR + "/helmraiser/status.json"
  var dataDir = env.HELMRAISER_DATA_DIR || "~/.helmraiser"
  if (dataDir === "~" || dataDir.indexOf("~/") === 0) dataDir = (env.HOME || "") + dataDir.slice(1)
  return dataDir + "/status.json"
}

function number(value) {
  var n = Number(value)
  return isFinite(n) ? n : 0
}

function list(value) {
  return Array.isArray(value) ? value : []
}

function text(value) {
  return value === undefined || value === null ? "" : String(value)
}

// Accepts both status.json (counts only) and `helmraiser status --json`
// (counts plus `awaiting` and `failed` rows). Returns null for anything that
// is not a status object, so a half-written or foreign file shows nothing.
function parseStatus(raw) {
  var data
  try {
    data = typeof raw === "string" ? JSON.parse(raw) : raw
  } catch (e) {
    return null
  }
  if (!data || typeof data !== "object" || Array.isArray(data) || !("updated_at" in data)) return null

  var running = list(data.running).map(function(run) {
    return {
      runId: text(run.run_id),
      task: text(run.task),
      stage: text(run.stage),
      iteration: number(run.iteration),
      costUsd: number(run.cost_usd),
      minutes: number(run.minutes)
    }
  })
  var hasDetails = Array.isArray(data.awaiting) || Array.isArray(data.failed)
  var awaiting = list(data.awaiting).map(function(row) {
    return {
      runId: text(row.run_id),
      task: text(row.task),
      pending: text(row.pending) || "merge",
      verdict: text(row.verdict),
      costUsd: number(row.cost_usd),
      // One arm of an experiment pair: only its blind label, and whether it awaits a pick.
      pairLabel: row.pair && typeof row.pair === "object" ? text(row.pair.label) : "",
      awaitingPick: !!(row.pair && row.pair.awaiting_pick)
    }
  })
  var failed = list(data.failed).map(function(row) {
    return { runId: text(row.run_id), task: text(row.task), reason: text(row.reason) }
  })
  var queueNext = list(data.queue_next).map(function(task) {
    return {
      taskId: text(task.task_id),
      id: text(task.id),
      priority: text(task.priority),
      project: text(task.project),
      heldBy: task.held_by && typeof task.held_by === "object" ? holder(task.held_by) : null
    }
  })
  var held = list(data.held).map(function(task) {
    var row = holder(task)
    row.taskId = text(task.task_id)
    row.id = text(task.id)
    row.project = text(task.project)
    return row
  })
  var draftCounts = {}
  if (data.drafts && typeof data.drafts === "object"
      && (Object.getPrototypeOf(data.drafts) === Object.prototype || Object.getPrototypeOf(data.drafts) === null)) {
    draftCounts = data.drafts
  }
  var draftCount = Object.keys(draftCounts).reduce(function(total, project) {
    return total + number(draftCounts[project])
  }, 0)
  var workerData = data.worker && typeof data.worker === "object" ? data.worker : {}
  var worker = {
    state: text(workerData.state) || "none",
    pid: workerData.pid === undefined || workerData.pid === null ? null : number(workerData.pid),
    startedAt: text(workerData.started_at),
    runs: list(workerData.children).length,
    maxParallel: workerData.max_parallel_runs === undefined || workerData.max_parallel_runs === null ? null : number(workerData.max_parallel_runs)
  }
  var spend = data.spend && typeof data.spend === "object" ? data.spend : {}
  var usage = data.usage && typeof data.usage === "object" ? data.usage : {}
  var paused = null
  if (data.paused && typeof data.paused === "object") {
    var until = data.paused.until
    paused = { reason: text(data.paused.reason), until: until === null || until === undefined ? null : number(until) }
  }

  return {
    updatedAt: text(data.updated_at),
    queuedCount: number(data.queued_count),
    queueNext: queueNext,
    held: held,
    draftCounts: draftCounts,
    draftCount: draftCount,
    worker: worker,
    running: running,
    awaitingCount: number(data.awaiting_count),
    awaiting: awaiting,
    failedCount: number(data.failed_24h_count),
    failed: failed,
    spend: spend,
    usage: usage,
    capsEnforced: data.caps_enforced === true,
    paused: paused,
    statusPath: text(data.status_path),
    hasDetails: hasDetails
  }
}

// The run that holds a queued task's project: {run_id, status, pending}.
function holder(row) {
  return { runId: text(row.run_id), status: text(row.status), pending: text(row.pending) }
}

// A status.json reload has counts but no rows. Keep the rows from the last
// CLI read, dropping them once the counts say there is nothing left.
function mergeStatus(previous, incoming) {
  if (!incoming) return null
  if (incoming.hasDetails || !previous) return incoming
  var merged = {}
  for (var key in incoming) merged[key] = incoming[key]
  merged.awaiting = incoming.awaitingCount > 0 ? list(previous.awaiting) : []
  merged.failed = incoming.failedCount > 0 ? list(previous.failed) : []
  merged.statusPath = incoming.statusPath || previous.statusPath
  return merged
}

function isUrgent(status) {
  return !!status && (!!status.paused || status.awaitingCount > 0)
}

// "<glyph> 1▶ 2? 3⊘ 4✎": running, awaiting approval, held tasks, drafts, then "‖" when
// paused. Zero counts are left out so an idle queue is just the glyph.
function labelText(status) {
  if (!status) return ""
  var parts = [GLYPH]
  if (status.running.length > 0) parts.push(status.running.length + "▶")
  if (status.awaitingCount > 0) parts.push(status.awaitingCount + "?")
  if (list(status.held).length > 0) parts.push(status.held.length + "⊘")
  if (status.draftCount > 0) parts.push(status.draftCount + "✎")
  if (status.paused) parts.push("‖")
  return parts.join(" ")
}

function tooltipText(status) {
  if (!status) return "Helmraiser"
  var held = list(status.held)
  var lines = ["Helmraiser: " + status.running.length + " running, " + status.awaitingCount + " awaiting approval, " + status.queuedCount + " queued" + (held.length ? " (" + held.length + " held)" : "")]
  if (status.paused) lines.push(pausedText(status.paused))
  held.forEach(function(row) { lines.push(heldLine(row)) })
  if (status.draftCount > 0) lines.push(status.draftCount + (status.draftCount === 1 ? " draft" : " drafts") + " awaiting intake")
  if (status.failedCount > 0) lines.push(status.failedCount + " failed in the last 24 h")
  return lines.join("\n")
}

function formatAge(iso, nowMs) {
  var then = Date.parse(iso)
  if (!isFinite(then)) return "unknown"
  var seconds = Math.max(0, Math.floor((nowMs - then) / 1000))
  if (seconds < 10) return "just now"
  if (seconds < 60) return seconds + "s ago"
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago"
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago"
  return Math.floor(seconds / 86400) + "d ago"
}

function formatCost(usd) {
  var n = number(usd)
  return "$" + n.toFixed(n !== 0 && Math.abs(n) < 1 ? 4 : 2)
}

function shortProvider(provider) {
  if (provider === "openai-codex") return "codex"
  if (provider === "opencode-go") return "go"
  return provider
}

var WINDOW_LABELS = { "5h": "5h", "week": "wk", "month": "mo" }

var PROVIDER_ORDER = ["opencode-go", "openai-codex"]

// Known providers first (go before codex), the rest alphabetical.
function orderedProviders(usage) {
  var names = Object.keys(usage || {}).sort()
  return PROVIDER_ORDER.filter(function(name) { return names.indexOf(name) >= 0 })
    .concat(names.filter(function(name) { return PROVIDER_ORDER.indexOf(name) < 0 }))
}

// "go 5h 1% · wk 2% · mo 1%  ·  codex wk 61%": subscription usage per window
// ("!" after a window at or over its warning; values are the effective ones,
// live where available), then codex dollar spend — only when codex has no
// live usage windows, which is when the published allowance is unknown.
function usageText(usage, spend) {
  var parts = []
  var codexUsageWindows = 0
  orderedProviders(usage).forEach(function(name) {
    var sub = usage[name] && typeof usage[name] === "object" ? usage[name] : {}
    var windowParts = []
    var first = true
    ;["5h", "week", "month"].forEach(function(window) {
      var data = sub[window]
      if (!data || typeof data !== "object") return
      var fraction = number(data.fraction)
      var warn = data.warn_at
      var mark = warn !== undefined && warn !== null && fraction >= number(warn) ? "!" : ""
      var label = WINDOW_LABELS[window] || window
      var prefix = first ? shortProvider(name) + " " : ""
      first = false
      windowParts.push(prefix + label + " " + Math.round(fraction * 100) + "%" + mark)
    })
    if (name === "openai-codex") codexUsageWindows = windowParts.length
    if (windowParts.length > 0) parts.push(windowParts.join(" · "))
  })
  if (!codexUsageWindows) {
    var codexSpend = spend && spend["openai-codex"] ? spend["openai-codex"]["5h"] : undefined
    parts.push("codex " + formatCost(codexSpend) + "/5h")
  }
  return parts.join("  ·  ")
}

function queueLine(task) {
  task = task || {}
  var priority = text(task.priority) || "normal"
  while (priority.length < 7) priority += " "
  if (!/ $/.test(priority)) priority += " "
  var project = text(task.project).replace(/\/+$/, "").split("/").pop()
  var id = text(task.id)
  return priority + (project ? project + ":" : "") + id + (task.heldBy ? "  (held)" : "")
}

// "held: helmraiser:fix-bug by run 1234abcd (awaiting merge approval)", as
// `helmraiser status` prints it.
function heldLine(row) {
  row = row || {}
  var project = text(row.project).replace(/\/+$/, "").split("/").pop()
  var wait = row.pending ? "awaiting " + row.pending + " approval" : text(row.status)
  return "held: " + (project ? project + ":" : "") + text(row.id) + " by run " + text(row.runId).slice(0, 8) + (wait ? " (" + wait + ")" : "")
}

function workerText(worker, nowMs) {
  worker = worker || {}
  if (worker.state !== "running") return "idle"
  var details = []
  if (worker.pid !== undefined && worker.pid !== null) details.push("pid " + worker.pid)
  var started = Date.parse(worker.startedAt)
  if (isFinite(started)) details.push(Math.max(0, Math.floor((nowMs - started) / 60000)) + " min")
  if (worker.maxParallel !== undefined && worker.maxParallel !== null) details.push(number(worker.runs) + "/" + worker.maxParallel + " runs")
  return "running" + (details.length ? " (" + details.join(", ") + ")" : "")
}

function meterText(fraction, width) {
  var size = Math.max(0, Math.floor(number(width)))
  var filled = Math.round(Math.max(0, Math.min(1, number(fraction))) * size)
  return new Array(filled + 1).join("▓") + new Array(size - filled + 1).join("░")
}

function usageRows(usage) {
  var rows = []
  orderedProviders(usage).forEach(function(name) {
    var values = usage[name] && typeof usage[name] === "object" ? usage[name] : {}
    var windows = []
    ;["5h", "week", "month"].forEach(function(window) {
      var value = values[window]
      if (!value || typeof value !== "object") return
      windows.push({ label: WINDOW_LABELS[window] || window, fraction: number(value.fraction) })
    })
    if (windows.length) rows.push({ provider: shortProvider(name), windows: windows })
  })
  return rows
}

function formatMinutes(minutes) {
  var n = Math.max(0, Math.floor(number(minutes)))
  return n < 60 ? n + "m" : Math.floor(n / 60) + "h " + (n % 60) + "m"
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

// `until` is epoch seconds; shown as local HH:MM.
function pausedText(paused) {
  if (!paused) return ""
  var out = "Paused" + (paused.reason ? ": " + paused.reason : "")
  if (paused.until) {
    var d = new Date(paused.until * 1000)
    out += " (until " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ")"
  }
  return out
}

function runLine(run) {
  return [run.stage || "-", "iter " + run.iteration, formatCost(run.costUsd), formatMinutes(run.minutes)].join(" · ")
}

function awaitingLine(row) {
  var parts = [
    row.awaitingPick
      ? "pair awaiting pick (" + row.pairLabel + ")"
      : row.pending === "plan" ? "plan approval" : row.pending === "scope" ? "scope approval" : "merge approval"
  ]
  if (row.verdict) parts.push(row.verdict)
  parts.push(formatCost(row.costUsd))
  return parts.join(" · ")
}

function parseDrafts(raw) {
  var data
  try {
    data = JSON.parse(raw)
  } catch (e) {
    return []
  }
  return list(data).map(function(row) {
    row = row || {}
    return {
      taskId: text(row.task_id),
      id: text(row.id),
      title: text(row.title),
      project: text(row.project),
      size: text(row.size),
      ageSec: number(row.age),
      intakeStatus: text(row.intake_status),
      intakeError: text(row.intake_error)
    }
  })
}

function draftLine(row) {
  var project = text(row.project).replace(/\/+$/, "").split("/").pop()
  var seconds = number(row.ageSec)
  var age = seconds < 60 ? seconds + "s"
    : seconds < 3600 ? Math.floor(seconds / 60) + "m"
    : seconds < 86400 ? Math.floor(seconds / 3600) + "h"
    : Math.floor(seconds / 86400) + "d"
  return [project, text(row.size), age, "intake " + text(row.intakeStatus)].join(" · ")
    + (row.intakeError ? " (" + row.intakeError + ")" : "")
}

function refreshIntervalMs(value) {
  var n = Math.floor(Number(value))
  if (!isFinite(n) || n <= 0) n = DEFAULT_REFRESH_SEC
  return Math.max(MIN_REFRESH_SEC, n) * 1000
}

function binary(value) {
  var s = text(value).trim()
  return s === "" ? "helmraiser" : s
}

// Runs the CLI through a login shell so ~/.local/bin and friends are on PATH
// (the shell's own environment may not have them). `exec "$@"` keeps every
// argument literal.
function cliArgv(bin, args) {
  return ["bash", "-lc", "exec \"$@\"", "bash", binary(bin)].concat(list(args))
}

function draftsArgv(bin) {
  return cliArgv(bin, ["drafts", "list", "--json"])
}

function dropArgv(bin, taskId) {
  return cliArgv(bin, ["drafts", "drop", taskId, "--reason", DRAFT_DROP_REASON, "--json"])
}

function controlArgv(bin, control, state) {
  var action = control
  if (control === "queue") {
    action = typeof state === "string" ? state : (state && state.paused ? "resume" : "pause")
  } else if (control === "worker") {
    var worker = state && state.worker ? state.worker : state
    action = typeof state === "string" ? state : (worker && worker.state === "running" ? "stop" : "start")
  } else if (control === "caps") {
    if (typeof state === "string") action = state
    else {
      var enforced = state && typeof state === "object" && "capsEnforced" in state
        ? state.capsEnforced : state
      action = enforced ? "off" : "on"
    }
  }
  var commands = {
    pause: ["queue", "pause", "--json"],
    resume: ["queue", "resume", "--json"],
    start: ["worker", "start", "--json"],
    stop: ["worker", "stop", "--json"],
    on: ["caps", "on", "--json"],
    off: ["caps", "off", "--json"]
  }
  return cliArgv(bin, commands[action] || [])
}

function shellQuote(value) {
  return "'" + text(value).replace(/'/g, "'\\''") + "'"
}

// A command string for bar.run(): a floating Omarchy terminal running the
// CLI. The launcher re-joins its arguments and hands them to `bash -c`, so
// the inner command is quoted once for that shell and once for bar.run's.
function terminalCommand(bin, args) {
  var inner = [binary(bin)].concat(list(args)).map(shellQuote).join(" ")
  return "omarchy-launch-floating-terminal-with-presentation " + shellQuote(inner)
}

// Where a run handed back to the worker (a plan approval) goes next.
function handBackText(data) {
  if (data.worker_serves_project === true) return "; the worker picks it up"
  if (data.worker === "running") return "; the worker does not serve this project"
  return "; no worker is running"
}

// Action commands print one JSON object, {"error": ...} on failure.
function actionResult(verb, stdout, exitCode) {
  var data = null
  var lines = text(stdout).trim().split("\n")
  try {
    data = JSON.parse(lines[lines.length - 1])
  } catch (e) {
    data = null
  }
  if (exitCode === 0 && data && !data.error) {
    if (verb === "drop") return { ok: true, message: "Dropped " + text(data.task_id) }
    if (verb !== "approve" && verb !== "reject") return { ok: true, message: verb + " complete" }
    var task = data.task_id ? " " + data.task_id : ""
    var status = data.status ? text(data.status) : ""
    if (data.worker_serves_project !== undefined) status += handBackText(data)
    return { ok: true, message: (verb === "approve" ? "Approved" : "Rejected") + task + (status ? " (" + status + ")" : "") }
  }
  var reason = data && data.error ? data.error : "exit code " + exitCode
  return { ok: false, message: verb + " failed: " + reason }
}

function clampIndex(index, count) {
  if (count <= 0) return -1
  return Math.max(0, Math.min(count - 1, index))
}

function moveIndex(index, count, delta) {
  if (count <= 0) return -1
  if (index < 0) return delta < 0 ? count - 1 : 0
  return clampIndex(index + delta, count)
}

if (typeof module !== "undefined") {
  module.exports = {
    GLYPH: GLYPH,
    DRAFT_DROP_REASON: DRAFT_DROP_REASON,
    parseDrafts: parseDrafts,
    draftLine: draftLine,
    draftsArgv: draftsArgv,
    dropArgv: dropArgv,
    statusPath: statusPath,
    parseStatus: parseStatus,
    mergeStatus: mergeStatus,
    isUrgent: isUrgent,
    labelText: labelText,
    tooltipText: tooltipText,
    formatAge: formatAge,
    formatCost: formatCost,
    usageText: usageText,
    queueLine: queueLine,
    heldLine: heldLine,
    workerText: workerText,
    meterText: meterText,
    usageRows: usageRows,
    formatMinutes: formatMinutes,
    pausedText: pausedText,
    runLine: runLine,
    awaitingLine: awaitingLine,
    refreshIntervalMs: refreshIntervalMs,
    cliArgv: cliArgv,
    controlArgv: controlArgv,
    shellQuote: shellQuote,
    terminalCommand: terminalCommand,
    actionResult: actionResult,
    clampIndex: clampIndex,
    moveIndex: moveIndex
  }
}
