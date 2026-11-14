import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import "Model.js" as Model

// Helmraiser in the bar: a glyph plus running / awaiting-approval counts, and
// the host for the approvals panel.
//
// status.json is only rewritten when a helmraiser command runs, so the widget
// runs `helmraiser status --json` on a timer (which also rewrites the file) and
// watches the file for the writes `work`, `approve` and friends make between
// ticks. With no status from either source the widget hides itself.
//
// Left click toggles the panel, right click refreshes, middle click opens
// `helmraiser report --since 24h` in a floating terminal.
BarWidget {
  id: root
  moduleName: "zsolt.helmraiser"

  readonly property string helmraiserBin: String(setting("helmraiserBin", "helmraiser"))
  readonly property int refreshIntervalMs: Model.refreshIntervalMs(setting("refreshIntervalSec", 30))

  // Guessed from the environment until the CLI reports where it wrote.
  readonly property string guessedStatusPath: Model.statusPath({
    XDG_RUNTIME_DIR: Quickshell.env("XDG_RUNTIME_DIR") || "",
    HELMRAISER_DATA_DIR: Quickshell.env("HELMRAISER_DATA_DIR") || "",
    HOME: Quickshell.env("HOME") || ""
  })
  property string reportedStatusPath: ""
  readonly property string statusFilePath: reportedStatusPath !== "" ? reportedStatusPath : guessedStatusPath

  property var status: null
  property var draftRows: []
  property string lastError: ""
  property string actionMessage: ""
  property bool actionOk: true
  property string busyRunId: ""
  property string busyVerb: ""

  readonly property bool urgent: Model.isUrgent(status)
  readonly property bool refreshing: statusProcess.running

  visible: status !== null
  implicitWidth: visible ? button.implicitWidth : 0
  implicitHeight: visible ? button.implicitHeight : 0

  function refresh() {
    if (!statusProcess.running) statusProcess.running = true
  }

  function applyStatus(parsed) {
    root.status = Model.mergeStatus(root.status, parsed)
    if (parsed && parsed.statusPath !== "") root.reportedStatusPath = parsed.statusPath
    if (parsed) {
      if (parsed.draftCount === 0) root.draftRows = []
      else if (!draftsProcess.running) draftsProcess.running = true
    }
  }

  // Approvals (merge or plan), rejections and draft drops refresh when done.
  // A plan approval returns at once: it hands the run back to the worker.
  function act(verb, row) {
    if (!row || root.busyRunId !== "" || (verb !== "approve" && verb !== "reject" && verb !== "drop")) return
    root.busyRunId = verb === "drop" ? "draft:" + row.taskId : row.runId
    root.busyVerb = verb
    root.actionMessage = verb === "drop" ? "Dropping " + row.title + "..."
      : (verb === "approve" ? "Approving " : "Rejecting ") + row.task + "..."
    root.actionOk = true
    actionProcess.command = verb === "drop" ? Model.dropArgv(root.helmraiserBin, row.taskId)
      : Model.cliArgv(root.helmraiserBin, [verb, row.runId, "--json"])
    actionProcess.running = true
  }

  function control(name) {
    if (!root.status || root.busyRunId !== "") return
    var action = name
    if (name === "queue") action = root.status.paused ? "queue resume" : "queue pause"
    else if (name === "worker") action = root.status.worker.state === "running" ? "worker stop" : "worker start"
    else if (name === "caps") action = root.status.capsEnforced ? "caps off" : "caps on"
    root.busyRunId = "control:" + name
    root.busyVerb = action
    root.actionMessage = action + "..."
    root.actionOk = true
    actionProcess.command = Model.controlArgv(root.helmraiserBin, name, root.status)
    actionProcess.running = true
  }

  function openReport() {
    if (root.bar) root.bar.run(Model.terminalCommand(root.helmraiserBin, ["report", "--since", "24h"]))
  }

  // ---- Panel contract: Bar.findPanelWidget routes shell summon/hide/toggle
  //      through open/close/opened on the bar-widget root.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property real openPanelIndicatorWidth: button.labelWidth

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Timer {
    interval: root.refreshIntervalMs
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  FileView {
    id: statusFile
    path: root.statusFilePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var parsed = Model.parseStatus(text())
      // A torn read keeps the last good status; the next write fixes it.
      if (parsed) root.applyStatus(parsed)
    }
    onLoadFailed: {
      // Missing file and no fresh CLI answer: nothing to show.
      if (!statusProcess.running && root.lastError !== "") root.status = null
    }
  }

  Process {
    id: statusProcess
    running: false
    command: Model.cliArgv(root.helmraiserBin, ["status", "--json"])

    stdout: StdioCollector {
      id: statusOut
      waitForEnd: true
    }

    stderr: StdioCollector {
      id: statusErr
      waitForEnd: true
    }

    onExited: function(exitCode) {
      var parsed = exitCode === 0 ? Model.parseStatus(statusOut.text) : null
      if (parsed) {
        root.lastError = ""
        root.applyStatus(parsed)
        statusFile.reload()
      } else {
        var detail = String(statusErr.text || "").trim().split("\n").pop()
        root.lastError = "helmraiser status failed" + (detail ? ": " + detail : " (exit " + exitCode + ")")
        console.warn("zsolt.helmraiser", root.lastError)
      }
    }
  }

  Process {
    id: draftsProcess
    running: false
    command: Model.draftsArgv(root.helmraiserBin)

    stdout: StdioCollector {
      id: draftsOut
      waitForEnd: true
    }

    onExited: function(exitCode) {
      // A newer status with no drafts wins over an in-flight list response.
      if (exitCode === 0 && root.status && root.status.draftCount > 0)
        root.draftRows = Model.parseDrafts(draftsOut.text)
    }
  }

  Process {
    id: actionProcess
    running: false

    stdout: StdioCollector {
      id: actionOut
      waitForEnd: true
    }

    onExited: function(exitCode) {
      var result = Model.actionResult(root.busyVerb, actionOut.text, exitCode)
      root.actionOk = result.ok
      root.actionMessage = result.message
      root.busyRunId = ""
      root.busyVerb = ""
      root.refresh()
    }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: "zsolt.helmraiser"

    function refresh(): void { root.broadcast("refresh") }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? Model.GLYPH : Model.labelText(root.status)
    active: root.urgent
    tooltipText: Model.tooltipText(root.status)

    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else if (b === Qt.MiddleButton) root.openReport()
      else root.toggle()
    }
  }
}
