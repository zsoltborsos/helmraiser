pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The Helmraiser popup: queue, worker and subscription usage controls, running
// runs, runs awaiting approval (with Approve / Reject), drafts (with Drop),
// failures in the last 24 h, and how old the status is.
//
// BarWidget.qml owns the data and the processes; this panel reads
// `hostWidget.status` and asks the host to act. Keys: r refresh, j/k select
// an awaiting run, a approve, x reject, Esc close.
Panel {
  id: root
  moduleName: "zsolt.helmraiser"
  ipcTarget: "zsolt.helmraiser"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property var status: hostWidget ? hostWidget.status : null
  readonly property var awaitingRows: status ? status.awaiting : []
  readonly property var draftRows: hostWidget ? hostWidget.draftRows : []
  readonly property bool busy: hostWidget ? hostWidget.busyRunId !== "" : false
  property int selectedIndex: -1
  property real nowMs: Date.now()

  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property color mutedForeground: Qt.darker(contentForeground, 1.5)
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  onAwaitingRowsChanged: selectedIndex = Model.clampIndex(selectedIndex < 0 ? 0 : selectedIndex, awaitingRows.length)

  function open() {
    root.nowMs = Date.now()
    root.refresh()
    root.controller.show()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function refresh() {
    if (root.hostWidget) root.hostWidget.refresh()
  }

  function moveSelection(delta) {
    root.selectedIndex = Model.moveIndex(root.selectedIndex, root.awaitingRows.length, delta)
  }

  function actOnSelected(verb) {
    if (root.selectedIndex < 0 || root.selectedIndex >= root.awaitingRows.length) return
    if (root.hostWidget) root.hostWidget.act(verb, root.awaitingRows[root.selectedIndex])
  }

  Timer {
    interval: 10000
    running: root.opened
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  component SectionLabel: PanelSectionHeader {
    foreground: root.contentForeground
    fontFamily: root.contentFontFamily
    topPadding: Style.space(6)
  }

  component BodyText: Text {
    width: parent ? parent.width : implicitWidth
    textFormat: Text.PlainText
    elide: Text.ElideRight
    color: root.contentForeground
    font.family: root.contentFontFamily
    font.pixelSize: Style.font.body
  }

  component DetailText: BodyText {
    color: root.mutedForeground
    font.pixelSize: Style.font.bodySmall
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveSelection(dy) }
      onCloseRequested: root.close()
      onDeleteRequested: root.actOnSelected("reject")
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "a" || t === "A") root.actOnSelected("approve")
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: content
          width: parent.width
          spacing: Style.space(4)

          // ---- Header: name, status age, refresh.
          Item {
            width: parent.width
            height: Math.max(title.implicitHeight, refreshButton.implicitHeight)

            Text {
              id: title
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: Model.GLYPH + "  Helmraiser"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              anchors.right: refreshButton.left
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.hostWidget && root.hostWidget.refreshing
                ? "refreshing..."
                : "updated " + (root.status ? Model.formatAge(root.status.updatedAt, root.nowMs) : "never")
              color: root.mutedForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }

            PanelActionButton {
              id: refreshButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰑐"
              tooltipText: "Refresh (r)"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: root.refresh()
            }
          }

          BodyText {
            visible: !!(root.status && root.status.paused)
            text: root.status ? Model.pausedText(root.status.paused) : ""
            color: root.urgentColor
            font.bold: true
          }

          DetailText {
            visible: root.hostWidget ? root.hostWidget.lastError !== "" : false
            text: root.hostWidget ? root.hostWidget.lastError : ""
            color: root.urgentColor
            wrapMode: Text.Wrap
            elide: Text.ElideNone
          }

          BodyText {
            visible: !root.status
            text: "No status yet"
          }

          // ---- Queue, worker and subscription usage.
          Item {
            width: parent.width
            height: Math.max(queueHeading.implicitHeight, queueButton.implicitHeight)

            Row {
              id: queueHeading
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)

              SectionLabel { width: implicitWidth; text: "QUEUE" }
              BodyText {
                width: implicitWidth
                text: root.status ? root.status.queuedCount + " queued" : "0 queued"
              }
            }

            Button {
              id: queueButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.status && root.status.paused ? "Resume" : "Pause"
              tooltipText: root.status && root.status.paused ? "helmraiser queue resume" : "helmraiser queue pause"
              enabled: !!root.status && !root.busy
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.hostWidget) root.hostWidget.control("queue")
            }
          }

          Repeater {
            model: root.status ? root.status.queueNext : []

            DetailText {
              required property var modelData
              text: "  " + Model.queueLine(modelData)
            }
          }

          DetailText {
            visible: !!root.status && root.status.queueNext.length === 0
            text: "  None"
          }

          Repeater {
            model: root.status ? root.status.held : []

            DetailText {
              required property var modelData
              text: "  " + Model.heldLine(modelData)
            }
          }

          Item {
            width: parent.width
            height: Math.max(workerHeading.implicitHeight, workerButton.implicitHeight)

            Row {
              id: workerHeading
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)

              SectionLabel { width: implicitWidth; text: "WORKER" }
              BodyText {
                width: implicitWidth
                text: root.status ? Model.workerText(root.status.worker, root.nowMs) : "idle"
              }
            }

            Button {
              id: workerButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              readonly property bool workerRunning: !!root.status && root.status.worker.state === "running"
              text: workerRunning ? "Stop" : "Start"
              tooltipText: workerRunning ? "helmraiser worker stop" : "helmraiser worker start"
              enabled: !!root.status && !root.busy
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.hostWidget) root.hostWidget.control("worker")
            }
          }

          SectionLabel { text: "USAGE" }

          Repeater {
            model: root.status ? Model.usageRows(root.status.usage) : []

            Row {
              id: usageRow
              required property var modelData
              spacing: Style.space(6)

              BodyText {
                width: Style.space(48)
                text: usageRow.modelData.provider
              }

              Repeater {
                model: usageRow.modelData.windows

                DetailText {
                  required property var modelData
                  width: implicitWidth
                  text: modelData.label + " " + Model.meterText(modelData.fraction, 8)
                    + " " + Math.round(modelData.fraction * 100) + "%"
                }
              }
            }
          }

          DetailText {
            visible: !!root.status && Model.usageRows(root.status.usage).length === 0
            text: "  None"
          }

          Item {
            width: parent.width
            height: Math.max(capsText.implicitHeight, capsButton.implicitHeight)

            DetailText {
              id: capsText
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: implicitWidth
              text: "  caps enforcement: " + (root.status && root.status.capsEnforced ? "on" : "off")
            }

            Button {
              id: capsButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.status && root.status.capsEnforced ? "Off" : "On"
              tooltipText: root.status && root.status.capsEnforced ? "helmraiser caps off" : "helmraiser caps on"
              enabled: !!root.status && !root.busy
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.hostWidget) root.hostWidget.control("caps")
            }
          }

          // ---- Running.
          SectionLabel { text: "RUNNING" }

          DetailText {
            visible: !root.status || root.status.running.length === 0
            text: "None"
          }

          Repeater {
            model: root.status ? root.status.running : []

            Column {
              id: runRow
              required property var modelData
              width: content.width

              BodyText { text: runRow.modelData.task }
              DetailText { text: Model.runLine(runRow.modelData) }
            }
          }

          // ---- Awaiting approval.
          SectionLabel { text: "AWAITING APPROVAL" }

          DetailText {
            visible: !root.status || root.status.awaitingCount === 0
            text: "None"
          }

          DetailText {
            visible: !!root.status && root.status.awaitingCount > 0 && root.awaitingRows.length === 0
            text: root.status ? root.status.awaitingCount + " awaiting; press r to load them" : ""
          }

          Repeater {
            model: root.awaitingRows

            CursorSurface {
              id: awaitingRow
              required property var modelData
              required property int index
              readonly property bool rowBusy: root.hostWidget ? root.hostWidget.busyRunId === awaitingRow.modelData.runId : false

              width: content.width
              height: awaitingColumn.implicitHeight + Style.space(8)
              hasCursor: index === root.selectedIndex
              foreground: root.contentForeground

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: root.selectedIndex = awaitingRow.index
                onClicked: root.selectedIndex = awaitingRow.index
              }

              Column {
                id: awaitingColumn
                anchors.left: parent.left
                anchors.right: actions.left
                anchors.leftMargin: Style.space(6)
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter

                BodyText { text: awaitingRow.modelData.task }
                DetailText { text: awaitingRow.rowBusy ? "working..." : Model.awaitingLine(awaitingRow.modelData) }
              }

              Row {
                id: actions
                anchors.right: parent.right
                anchors.rightMargin: Style.space(4)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(4)

                Button {
                  text: "Approve"
                  tooltipText: awaitingRow.modelData.pending === "plan" ? "Approve the plan; the worker resumes the run (a)" : "Merge (a)"
                  enabled: !root.busy
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: {
                    root.selectedIndex = awaitingRow.index
                    root.actOnSelected("approve")
                  }
                }

                Button {
                  text: "Reject"
                  tooltipText: "Reject; the branch is kept (x)"
                  enabled: !root.busy
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: {
                    root.selectedIndex = awaitingRow.index
                    root.actOnSelected("reject")
                  }
                }
              }
            }
          }

          DetailText {
            visible: root.hostWidget ? root.hostWidget.actionMessage !== "" : false
            text: root.hostWidget ? root.hostWidget.actionMessage : ""
            color: root.hostWidget && !root.hostWidget.actionOk ? root.urgentColor : root.mutedForeground
            wrapMode: Text.Wrap
            elide: Text.ElideNone
          }

          // ---- Drafts awaiting intake (intake stays in agent sessions).
          SectionLabel {
            text: "DRAFTS" + (root.status && root.status.draftCount > 0 ? "  " + root.status.draftCount : "")
          }

          DetailText {
            visible: root.draftRows.length === 0
            text: "No drafts"
          }

          Repeater {
            model: root.draftRows

            Item {
              id: draftRow
              required property var modelData
              readonly property bool rowBusy: root.hostWidget ? root.hostWidget.busyRunId === "draft:" + draftRow.modelData.taskId : false

              width: content.width
              height: Math.max(draftColumn.implicitHeight, dropButton.implicitHeight) + Style.space(8)

              Column {
                id: draftColumn
                anchors.left: parent.left
                anchors.right: dropButton.left
                anchors.leftMargin: Style.space(6)
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter

                BodyText { text: draftRow.modelData.title }
                DetailText { text: draftRow.rowBusy ? "working..." : Model.draftLine(draftRow.modelData) }
              }

              Button {
                id: dropButton
                anchors.right: parent.right
                anchors.rightMargin: Style.space(4)
                anchors.verticalCenter: parent.verticalCenter
                text: "Drop"
                tooltipText: "Drop this draft (the file is kept; the draft is marked dropped)"
                enabled: !root.busy
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.hostWidget) root.hostWidget.act("drop", draftRow.modelData)
              }
            }
          }

          // ---- Failed in the last 24 h.
          SectionLabel {
            text: "FAILED (24 H)" + (root.status && root.status.failedCount > 0 ? "  " + root.status.failedCount : "")
          }

          DetailText {
            visible: !root.status || root.status.failedCount === 0
            text: "None"
          }

          Repeater {
            model: root.status ? root.status.failed : []

            BodyText {
              id: failedRow
              required property var modelData
              text: failedRow.modelData.task + ": " + (failedRow.modelData.reason || "unknown reason")
              color: root.urgentColor
            }
          }

          DetailText {
            topPadding: Style.space(8)
            text: "r refresh · j/k select · a approve · x reject · Esc close"
            wrapMode: Text.Wrap
            elide: Text.ElideNone
          }
        }
      }
    }
  }
}
