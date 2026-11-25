const test = require("node:test")
const assert = require("node:assert/strict")
const { execFileSync } = require("node:child_process")
const Model = require("../Model.js")

// `helmraiser status --json` as src/helmraiser/cli/monitor.py prints it.
const cliPayload = {
  updated_at: "2026-09-23T10:00:00+00:00",
  queued_count: 3,
  queue_next: [
    { task_id: "-home-user-gamma:report-rejected-split", id: "report-rejected-split", priority: "high", project: "/home/user/gamma", held_by: null },
    { task_id: "-home-user-beta:index", id: "index", priority: "normal", project: "/home/user/beta", held_by: null }
  ],
  held: [],
  worker: { state: "running", pid: 1234, started_at: "2026-09-23T09:32:00+00:00", argv: [], log: "/tmp/worker.log" },
  running: [{ run_id: "r1", task: "proj:fix-bug", stage: "implement", iteration: 2, cost_usd: 0.1234, minutes: 75 }],
  awaiting_count: 2,
  failed_24h_count: 1,
  spend: { "openai-codex": { "5h": 1.5, "7d": 2, "30d": 3 }, "opencode-go": { "5h": 0.5, "7d": 1, "30d": 1 } },
  usage: {
    "opencode-go": {
      "5h": { fraction: 0.08, spend_usd: 0.5, resets_at: 1760000000, warn_at: 0.8, stop_at: null, source: "live", live_fraction: 0.08, estimate_fraction: 0.012 },
      week: { fraction: 0.1, spend_usd: 1, resets_at: 1760000000, warn_at: 0.8, stop_at: null, source: "live", live_fraction: 0.1, estimate_fraction: 0.02 },
      month: { fraction: 0.05, spend_usd: 1, resets_at: 1760000000, warn_at: 0.8, stop_at: null, source: "live", live_fraction: 0.05, estimate_fraction: 0.0123 }
    },
    "openai-codex": {
      week: { fraction: 0.61, spend_usd: 0, resets_at: 1760000000, warn_at: 0.8, stop_at: null, source: "live", live_fraction: 0.61 }
    }
  },
  caps_enforced: true,
  paused: null,
  queue: { queued: 3 },
  awaiting: [
    { run_id: "a1", task: "proj:feature", pending: "merge", verdict: "approve", cost_usd: 0.5 },
    { run_id: "a2", task: "proj:plan-me", pending: "plan", verdict: null, cost_usd: 0 }
  ],
  failed: [{ run_id: "f1", task: "proj:broken", reason: "verify_failed", ended_at: "2026-09-23T09:00:00+00:00" }],
  status_path: "/run/user/1000/helmraiser/status.json"
}

// status.json as helmraiser.loop.status.build_status writes it.
const filePayload = {
  updated_at: "2026-09-23T10:05:00+00:00",
  queued_count: 2,
  queue_next: [],
  worker: { state: "none" },
  running: [],
  awaiting_count: 1,
  failed_24h_count: 1,
  spend: { "openai-codex": { "5h": 1.75, "7d": 2, "30d": 3 } },
  usage: { "opencode-go": {
    "5h": { fraction: 0.85, spend_usd: 2.55, resets_at: 1760000000, warn_at: 0.8, stop_at: null },
    week: { fraction: 0.1, spend_usd: 3, resets_at: 1760000000, warn_at: 0.8, stop_at: null },
    month: { fraction: 0.1, spend_usd: 3, resets_at: 1760000000, warn_at: 0.8, stop_at: null }
  } },
  caps_enforced: true,
  paused: { reason: "cap:opencode-go:5h", until: null }
}

test("statusPath follows XDG_RUNTIME_DIR, then HELMRAISER_DATA_DIR, then ~/.helmraiser", () => {
  assert.equal(Model.statusPath({ XDG_RUNTIME_DIR: "/run/user/1000", HOME: "/h" }), "/run/user/1000/helmraiser/status.json")
  assert.equal(Model.statusPath({ HELMRAISER_DATA_DIR: "/data/sq", HOME: "/h" }), "/data/sq/status.json")
  assert.equal(Model.statusPath({ HELMRAISER_DATA_DIR: "~/sq", HOME: "/h" }), "/h/sq/status.json")
  assert.equal(Model.statusPath({ HOME: "/h" }), "/h/.helmraiser/status.json")
})

test("parseStatus reads the CLI payload with its detail rows", () => {
  const s = Model.parseStatus(JSON.stringify(cliPayload))
  assert.equal(s.queuedCount, 3)
  assert.deepEqual(s.queueNext[0], {
    taskId: "-home-user-gamma:report-rejected-split",
    id: "report-rejected-split",
    priority: "high",
    project: "/home/user/gamma",
    heldBy: null
  })
  assert.deepEqual(s.held, [])
  assert.deepEqual(s.worker, { state: "running", pid: 1234, startedAt: "2026-09-23T09:32:00+00:00", runs: 0, maxParallel: null })
  assert.equal(s.running.length, 1)
  assert.deepEqual(s.running[0], { runId: "r1", task: "proj:fix-bug", stage: "implement", iteration: 2, costUsd: 0.1234, minutes: 75 })
  assert.equal(s.awaitingCount, 2)
  assert.equal(s.awaiting[1].pending, "plan")
  assert.equal(s.awaiting[1].verdict, "")
  assert.equal(s.failedCount, 1)
  assert.deepEqual(s.failed[0], { runId: "f1", task: "proj:broken", reason: "verify_failed" })
  assert.equal(s.paused, null)
  assert.equal(s.statusPath, "/run/user/1000/helmraiser/status.json")
  assert.equal(s.hasDetails, true)
})

test("parseStatus reads status.json, which has counts only", () => {
  const s = Model.parseStatus(JSON.stringify(filePayload))
  assert.equal(s.hasDetails, false)
  assert.deepEqual(s.awaiting, [])
  assert.equal(s.failedCount, 1)
  assert.deepEqual(s.paused, { reason: "cap:opencode-go:5h", until: null })
  assert.equal(s.spend["openai-codex"]["5h"], 1.75)
  assert.equal(s.usage["opencode-go"]["5h"].fraction, 0.85)
})

test("parseStatus rejects garbage, partial writes and foreign JSON", () => {
  assert.equal(Model.parseStatus(""), null)
  assert.equal(Model.parseStatus("{\"updated_at\": "), null)
  assert.equal(Model.parseStatus("[]"), null)
  assert.equal(Model.parseStatus("{\"error\": \"boom\"}"), null)
})

test("mergeStatus keeps CLI rows across a file reload until the counts drop to zero", () => {
  const cli = Model.parseStatus(cliPayload)
  const merged = Model.mergeStatus(cli, Model.parseStatus(filePayload))
  assert.equal(merged.queuedCount, 2)
  assert.equal(merged.awaiting.length, 2)
  assert.equal(merged.failed.length, 1)
  assert.equal(merged.statusPath, cli.statusPath)

  const cleared = Model.mergeStatus(cli, Model.parseStatus({ ...filePayload, awaiting_count: 0, failed_24h_count: 0 }))
  assert.deepEqual(cleared.awaiting, [])
  assert.deepEqual(cleared.failed, [])

  assert.equal(Model.mergeStatus(cli, null), null)
  const fresh = Model.parseStatus(cliPayload)
  assert.equal(Model.mergeStatus(merged, fresh), fresh)
})

test("labelText shows running and awaiting counts, and marks a pause", () => {
  const s = Model.parseStatus(cliPayload)
  assert.equal(Model.labelText(s), Model.GLYPH + " 1▶ 2?")
  assert.equal(Model.labelText({ ...s, running: [], awaitingCount: 0 }), Model.GLYPH)
  assert.equal(Model.labelText({ ...s, running: [], awaitingCount: 0, paused: { reason: "x" } }), Model.GLYPH + " ‖")
  assert.equal(Model.labelText(null), "")
})

test("isUrgent when paused or when a run awaits approval", () => {
  const s = Model.parseStatus(cliPayload)
  assert.equal(Model.isUrgent(s), true)
  assert.equal(Model.isUrgent({ ...s, awaitingCount: 0 }), false)
  assert.equal(Model.isUrgent({ ...s, awaitingCount: 0, paused: { reason: "manual" } }), true)
  assert.equal(Model.isUrgent(null), false)
})

test("tooltipText summarises counts, pause and failures", () => {
  const s = Model.parseStatus({ ...cliPayload, paused: { reason: "manual", until: null } })
  assert.equal(Model.tooltipText(s), "Helmraiser: 1 running, 2 awaiting approval, 3 queued\nPaused: manual\n1 failed in the last 24 h")
})

// Two queued tasks of a project whose run awaits merge approval, and one of a
// project with a running run, as helmraiser.loop.status.build_status reports them.
const heldPayload = {
  ...cliPayload,
  queue_next: [
    { task_id: "-home-user-alpha:ci-cache", id: "ci-cache", priority: "high", project: "/home/user/alpha", held_by: { run_id: "11111111-1111-4111-8111-111111111111", status: "awaiting_approval", pending: "merge" } },
    { task_id: "-home-user-beta:index", id: "index", priority: "normal", project: "/home/user/beta", held_by: null }
  ],
  held: [
    { task_id: "-home-user-alpha:ci-cache", id: "ci-cache", title: "Cache CI deps", project: "/home/user/alpha", run_id: "11111111-1111-4111-8111-111111111111", status: "awaiting_approval", pending: "merge" },
    { task_id: "-home-user-gamma:docs", id: "docs", title: "Docs", project: "/home/user/gamma/", run_id: "22222222-2222-4222-8222-222222222222", status: "running", pending: null }
  ]
}

test("held tasks: parsed rows, bar count, tooltip lines and queue marks", () => {
  const s = Model.parseStatus(JSON.stringify(heldPayload))
  assert.deepEqual(s.queueNext[0].heldBy, { runId: "11111111-1111-4111-8111-111111111111", status: "awaiting_approval", pending: "merge" })
  assert.equal(s.queueNext[1].heldBy, null)
  assert.equal(s.held.length, 2)
  assert.deepEqual(s.held[1], { runId: "22222222-2222-4222-8222-222222222222", status: "running", pending: "", taskId: "-home-user-gamma:docs", id: "docs", project: "/home/user/gamma/" })

  assert.equal(Model.heldLine(s.held[0]), "held: alpha:ci-cache by run 11111111 (awaiting merge approval)")
  assert.equal(Model.heldLine(s.held[1]), "held: gamma:docs by run 22222222 (running)")
  assert.equal(Model.queueLine(s.queueNext[0]), "high   alpha:ci-cache  (held)")
  assert.equal(Model.queueLine(s.queueNext[1]), "normal beta:index")

  assert.equal(Model.labelText(s), Model.GLYPH + " 1▶ 2? 2⊘")
  assert.equal(Model.tooltipText(s), [
    "Helmraiser: 1 running, 2 awaiting approval, 3 queued (2 held)",
    "held: alpha:ci-cache by run 11111111 (awaiting merge approval)",
    "held: gamma:docs by run 22222222 (running)",
    "1 failed in the last 24 h"
  ].join("\n"))

  // status.json carries `held` too, so a file reload replaces the rows.
  const reloaded = Model.mergeStatus(s, Model.parseStatus({ ...filePayload, held: [] }))
  assert.deepEqual(reloaded.held, [])
  // An older payload without `held` shows none.
  assert.deepEqual(Model.parseStatus(filePayload).held, [])
})

test("draft counts are read from plain objects and replaced on file reloads", () => {
  const drafts = { "/home/user/alpha": 2, "/home/user/beta": 1 }
  const s = Model.parseStatus({ ...cliPayload, drafts })
  assert.deepEqual(s.draftCounts, drafts)
  assert.equal(s.draftCount, 3)
  const reloaded = Model.mergeStatus(s, Model.parseStatus({ ...filePayload, drafts: { "/home/user/alpha": 1 } }))
  assert.deepEqual(reloaded.draftCounts, { "/home/user/alpha": 1 })
  assert.equal(reloaded.draftCount, 1)
  for (const drafts of [undefined, null, "junk", 2, [], new Date(), Object.create({ count: 4 })]) {
    const invalid = Model.parseStatus({ ...cliPayload, drafts })
    assert.deepEqual(invalid.draftCounts, {})
    assert.equal(invalid.draftCount, 0)
  }
})

test("drafts appear after held counts and lines, without adding urgency", () => {
  const s = Model.parseStatus({ ...heldPayload, drafts: { "/home/user/alpha": 3 }, paused: { reason: "manual" } })
  assert.equal(Model.labelText(s), Model.GLYPH + " 1▶ 2? 2⊘ 3✎ ‖")
  assert.equal(Model.tooltipText(s), [
    "Helmraiser: 1 running, 2 awaiting approval, 3 queued (2 held)",
    "Paused: manual",
    Model.heldLine(s.held[0]),
    Model.heldLine(s.held[1]),
    "3 drafts awaiting intake",
    "1 failed in the last 24 h"
  ].join("\n"))
  const one = Model.parseStatus({ ...cliPayload, running: [], awaiting_count: 0, drafts: { "/home/user/alpha": 1 } })
  assert.equal(Model.labelText(one), Model.GLYPH + " 1✎")
  assert.ok(Model.tooltipText(one).split("\n").includes("1 draft awaiting intake"))
  assert.equal(Model.isUrgent(one), false)
  assert.doesNotMatch(Model.tooltipText(Model.parseStatus(cliPayload)), /awaiting intake/)
})

test("parseDrafts maps CLI rows using text and numeric defaults", () => {
  const rows = Model.parseDrafts(JSON.stringify([
    { task_id: "-home-user-alpha:fix-x", id: "fix-x", title: "Fix X", project: "/home/user/alpha/", size: "S", age: "7200", intake_status: "proposed", intake_error: null },
    { task_id: "p:y", id: "y", title: "Y", project: "/home/user/beta", size: "M", age: 30, intake_status: "failed", intake_error: "no verify" }
  ]))
  assert.deepEqual(rows[0], {
    taskId: "-home-user-alpha:fix-x", id: "fix-x", title: "Fix X", project: "/home/user/alpha/", size: "S", ageSec: 7200, intakeStatus: "proposed", intakeError: ""
  })
  assert.equal(Model.draftLine(rows[0]), "alpha · S · 2h · intake proposed")
  assert.equal(Model.draftLine(rows[1]), "beta · M · 30s · intake failed (no verify)")
  assert.deepEqual(Model.parseDrafts("[{}]")[0], {
    taskId: "", id: "", title: "", project: "", size: "", ageSec: 0, intakeStatus: "", intakeError: ""
  })
  for (const raw of ["not json", "", "{}", "null", "42", '"text"']) {
    assert.deepEqual(Model.parseDrafts(raw), [])
  }
})

test("draftLine age uses CLI second, minute, hour and day buckets", () => {
  const row = { project: "/x/alpha", size: "S", intakeStatus: "pending", intakeError: "" }
  for (const [ageSec, age] of [[0, "0s"], [45, "45s"], [59, "59s"], [60, "1m"], [720, "12m"], [3599, "59m"], [3600, "1h"], [10800, "3h"], [86399, "23h"], [86400, "1d"], [172800, "2d"]]) {
    assert.equal(Model.draftLine({ ...row, ageSec }), "alpha · S · " + age + " · intake pending")
  }
})

test("draft commands use cliArgv and drop arguments survive a bash round trip", () => {
  assert.equal(Model.DRAFT_DROP_REASON, "dropped from the Helmraiser widget")
  assert.deepEqual(Model.draftsArgv("/opt/helmraiser"), Model.cliArgv("/opt/helmraiser", ["drafts", "list", "--json"]))
  const taskId = "-home-user-alpha:it's $(id); echo bad"
  assert.deepEqual(Model.dropArgv("/opt/helmraiser", taskId), Model.cliArgv("/opt/helmraiser", ["drafts", "drop", taskId, "--reason", Model.DRAFT_DROP_REASON, "--json"]))
  const argv = Model.dropArgv("printf", taskId)
  // printf is the stand-in CLI; add its format before the literal CLI args.
  argv.splice(5, 0, "[%s]")
  assert.equal(execFileSync(argv[0], argv.slice(1), { encoding: "utf8" }),
    "[drafts][drop][" + taskId + "][--reason][dropped from the Helmraiser widget][--json]")
})

test("drop results report the task or the CLI failure, leaving controls unchanged", () => {
  assert.deepEqual(Model.actionResult("drop", JSON.stringify({ task_id: "p:t", status: "dropped" }), 0), { ok: true, message: "Dropped p:t" })
  assert.deepEqual(Model.actionResult("drop", JSON.stringify({ error: "not a draft" }), 2), { ok: false, message: "drop failed: not a draft" })
  assert.deepEqual(Model.actionResult("drop", JSON.stringify({ error: "not a draft" }), 0), { ok: false, message: "drop failed: not a draft" })
  assert.deepEqual(Model.actionResult("drop", "", 127), { ok: false, message: "drop failed: exit code 127" })
  assert.deepEqual(Model.actionResult("queue pause", "{}", 0), { ok: true, message: "queue pause complete" })
})

test("formatAge buckets seconds, minutes, hours and days", () => {
  const t = Date.parse("2026-09-23T10:00:00+00:00")
  assert.equal(Model.formatAge("2026-09-23T10:00:00+00:00", t + 3000), "just now")
  assert.equal(Model.formatAge("2026-09-23T10:00:00+00:00", t + 42000), "42s ago")
  assert.equal(Model.formatAge("2026-09-23T10:00:00+00:00", t + 5 * 60000), "5m ago")
  assert.equal(Model.formatAge("2026-09-23T10:00:00+00:00", t + 3 * 3600000), "3h ago")
  assert.equal(Model.formatAge("2026-09-23T10:00:00+00:00", t + 2 * 86400000), "2d ago")
  assert.equal(Model.formatAge("2026-09-23T10:00:00+00:00", t - 5000), "just now")
  assert.equal(Model.formatAge("", t), "unknown")
})

test("queueLine uses the short project name and aligns priorities", () => {
  assert.equal(Model.queueLine(cliPayload.queue_next[0]), "high   gamma:report-rejected-split")
  assert.equal(Model.queueLine(cliPayload.queue_next[1]), "normal beta:index")
})

test("workerText reports running age and treats absent or exited workers as idle", () => {
  const now = Date.parse("2026-09-23T10:00:00+00:00")
  const worker = Model.parseStatus(cliPayload).worker
  assert.equal(Model.workerText(worker, now), "running (pid 1234, 28 min)")
  assert.equal(Model.workerText({ state: "exited", pid: 1234 }, now), "idle")
  assert.equal(Model.workerText({ state: "none" }, now), "idle")
})

test("workerText shows the worker's child runs against its limit", () => {
  const now = Date.parse("2026-09-23T10:00:00+00:00")
  const payload = Object.assign({}, cliPayload, {
    worker: Object.assign({}, cliPayload.worker, {
      max_parallel_runs: 2,
      children: [{ pid: 11, project: "/home/user/gamma", run_id: null, started_at: 1 }]
    })
  })
  const worker = Model.parseStatus(payload).worker
  assert.equal(worker.runs, 1)
  assert.equal(worker.maxParallel, 2)
  assert.equal(Model.workerText(worker, now), "running (pid 1234, 28 min, 1/2 runs)")
})

test("meterText clamps fractions and fills the requested width", () => {
  assert.equal(Model.meterText(0.25, 8), "▓▓░░░░░░")
  assert.equal(Model.meterText(2, 4), "▓▓▓▓")
  assert.equal(Model.meterText(-1, 3), "░░░")
})

test("usageRows orders subscriptions and exposes effective fractions", () => {
  assert.deepEqual(Model.usageRows(cliPayload.usage), [
    {
      provider: "go",
      windows: [
        { label: "5h", fraction: 0.08 },
        { label: "wk", fraction: 0.1 },
        { label: "mo", fraction: 0.05 }
      ]
    },
    { provider: "codex", windows: [{ label: "wk", fraction: 0.61 }] }
  ])
})

test("formatCost, usageText, formatMinutes, runLine and awaitingLine", () => {
  assert.equal(Model.formatCost(0), "$0.00")
  assert.equal(Model.formatCost(0.1234), "$0.1234")
  assert.equal(Model.formatCost(12.5), "$12.50")
  assert.equal(
    Model.usageText(cliPayload.usage, cliPayload.spend),
    "go 5h 8% · wk 10% · mo 5%  ·  codex wk 61%"
  )
  assert.equal(
    Model.usageText(filePayload.usage, {}),
    "go 5h 85%! · wk 10% · mo 10%  ·  codex $0.00/5h"
  )
  assert.equal(Model.usageText({}, cliPayload.spend), "codex $1.50/5h")
  assert.equal(
    Model.usageText(
      {
        "opencode-go": {
          "5h": { fraction: 0.07, spend_usd: 0.5, resets_at: 1760000000, warn_at: 0.8, stop_at: null },
          week: { fraction: 0.04, spend_usd: 1, resets_at: 1760000000, warn_at: 0.8, stop_at: null },
          month: { fraction: 0.02, spend_usd: 1, resets_at: 1760000000, warn_at: 0.8, stop_at: null }
        },
        "openai-codex": { week: { fraction: 0.61, spend_usd: 0, resets_at: 1760000000, warn_at: 0.8, stop_at: null } }
      },
      cliPayload.spend
    ),
    "go 5h 7% · wk 4% · mo 2%  ·  codex wk 61%"
  )
  assert.equal(Model.formatMinutes(7), "7m")
  assert.equal(Model.formatMinutes(75), "1h 15m")
  const s = Model.parseStatus(cliPayload)
  assert.equal(Model.runLine(s.running[0]), "implement · iter 2 · $0.1234 · 1h 15m")
  assert.equal(Model.awaitingLine(s.awaiting[0]), "merge approval · approve · $0.5000")
  assert.equal(Model.awaitingLine(s.awaiting[1]), "plan approval · $0.00")
})

test("awaitingLine labels an unpicked pair arm and a scope request", () => {
  const s = Model.parseStatus({
    ...cliPayload,
    awaiting: [
      { run_id: "r1", task: "t", pending: "merge", verdict: "pass", cost_usd: 1, pair: { task_id: "p:t", label: "X", picked: false, awaiting_pick: true } },
      { run_id: "r2", task: "t", pending: "merge", verdict: "pass", cost_usd: 1, pair: { task_id: "p:t", label: "Y", picked: true, awaiting_pick: false } },
      { run_id: "r3", task: "u", pending: "scope", verdict: null, cost_usd: 0 }
    ]
  })
  assert.equal(Model.awaitingLine(s.awaiting[0]), "pair awaiting pick (X) · pass · $1.00")
  assert.equal(Model.awaitingLine(s.awaiting[1]), "merge approval · pass · $1.00")
  assert.equal(Model.awaitingLine(s.awaiting[2]), "scope approval · $0.00")
})

test("pausedText includes the reason and a local until time", () => {
  assert.equal(Model.pausedText(null), "")
  assert.equal(Model.pausedText({ reason: "manual", until: null }), "Paused: manual")
  const until = new Date(2026, 8, 23, 7, 5).getTime() / 1000
  assert.equal(Model.pausedText({ reason: "cap:opencode-go:5h", until }), "Paused: cap:opencode-go:5h (until 07:05)")
})

test("refreshIntervalMs defaults to 30 s and never goes under 5 s", () => {
  assert.equal(Model.refreshIntervalMs(undefined), 30000)
  assert.equal(Model.refreshIntervalMs("x"), 30000)
  assert.equal(Model.refreshIntervalMs(1), 5000)
  assert.equal(Model.refreshIntervalMs(60), 60000)
})

test("cliArgv runs the binary through a login shell with literal arguments", () => {
  assert.deepEqual(Model.cliArgv("", ["status", "--json"]), ["bash", "-lc", "exec \"$@\"", "bash", "helmraiser", "status", "--json"])
  assert.deepEqual(Model.cliArgv(" /opt/sq/bin/helmraiser ", ["approve", "r1"]).slice(4), ["/opt/sq/bin/helmraiser", "approve", "r1"])
  const out = execFileSync(Model.cliArgv("printf", ["[%s]", "$(id) it's"])[0], Model.cliArgv("printf", ["[%s]", "$(id) it's"]).slice(1), { encoding: "utf8" })
  assert.equal(out, "[$(id) it's]")
})

test("controlArgv builds every queue, worker and caps command", () => {
  const tail = (control, state) => Model.controlArgv("helmraiser", control, state).slice(5)
  assert.deepEqual(tail("queue", "pause"), ["queue", "pause", "--json"])
  assert.deepEqual(tail("queue", "resume"), ["queue", "resume", "--json"])
  assert.deepEqual(tail("worker", "start"), ["worker", "start", "--json"])
  assert.deepEqual(tail("worker", "stop"), ["worker", "stop", "--json"])
  assert.deepEqual(tail("caps", "on"), ["caps", "on", "--json"])
  assert.deepEqual(tail("caps", "off"), ["caps", "off", "--json"])
  assert.deepEqual(tail("queue", { paused: null }), ["queue", "pause", "--json"])
  assert.deepEqual(tail("worker", { worker: { state: "running" } }), ["worker", "stop", "--json"])
  assert.deepEqual(tail("caps", { capsEnforced: true }), ["caps", "off", "--json"])
})

test("terminalCommand survives bar.run's shell and the launcher's bash -c", () => {
  const command = Model.terminalCommand("printf", ["[%s]", "it's $(id)"])
  assert.match(command, /^omarchy-launch-floating-terminal-with-presentation '/)
  // Stand-in for the launcher: it joins "$@" and runs the result with bash -c.
  const launcher = "omarchy-launch-floating-terminal-with-presentation() { bash -c \"$*\"; }; "
  const out = execFileSync("bash", ["-c", launcher + command], { encoding: "utf8" })
  assert.equal(out, "[it's $(id)]")
})

test("actionResult reads approve/reject --json output", () => {
  assert.deepEqual(Model.actionResult("approve", "{\"approved\": \"merge\", \"task_id\": \"p:t\", \"status\": \"landed\"}\n", 0), { ok: true, message: "Approved p:t (landed)" })
  assert.deepEqual(Model.actionResult("reject", "{\"rejected\": \"merge\", \"task_id\": \"p:t\", \"status\": \"failed\"}", 0), { ok: true, message: "Rejected p:t (failed)" })
  assert.deepEqual(Model.actionResult("approve", "{\"error\": \"unknown run: zz\"}", 1), { ok: false, message: "approve failed: unknown run: zz" })
  assert.deepEqual(Model.actionResult("reject", "", 127), { ok: false, message: "reject failed: exit code 127" })
})

test("actionResult says where an approved plan goes next", () => {
  const plan = { approved: "plan", run_id: "r1", task_id: "p:t", status: "resuming", stage: "implement" }
  const out = (extra) => JSON.stringify(Object.assign({}, plan, extra))
  assert.deepEqual(Model.actionResult("approve", out({ worker: "running", worker_serves_project: true }), 0), { ok: true, message: "Approved p:t (resuming; the worker picks it up)" })
  assert.deepEqual(Model.actionResult("approve", out({ worker: "running", worker_serves_project: false }), 0), { ok: true, message: "Approved p:t (resuming; the worker does not serve this project)" })
  assert.deepEqual(Model.actionResult("approve", out({ worker: "none", worker_serves_project: false }), 0), { ok: true, message: "Approved p:t (resuming; no worker is running)" })
})

test("moveIndex steps and clamps the selection", () => {
  assert.equal(Model.moveIndex(-1, 3, 1), 0)
  assert.equal(Model.moveIndex(-1, 3, -1), 2)
  assert.equal(Model.moveIndex(0, 3, 1), 1)
  assert.equal(Model.moveIndex(2, 3, 1), 2)
  assert.equal(Model.moveIndex(0, 3, -1), 0)
  assert.equal(Model.moveIndex(1, 0, 1), -1)
  assert.equal(Model.clampIndex(5, 2), 1)
  assert.equal(Model.clampIndex(0, 0), -1)
})

test("queueLine keeps a space after a long priority", () => {
  assert.equal(Model.queueLine({ priority: "critical", project: "/x/helmraiser", id: "t" }), "critical helmraiser:t")
})
