# Helmraiser bar widget for Omarchy

An Omarchy 4 bar widget (`zsolt.helmraiser`) that shows Helmraiser's state and lets you
approve or reject runs that await approval, and drop pending drafts.

## What it shows

The bar button shows an airplane-takeoff glyph followed by counts. `1▶` means one run is running,
`2?` means two runs await approval, `3⊘` means three queued tasks are held (their
project has a run that has not ended, such as one awaiting approval), `4✎` means four
drafts await intake, and `‖` means the queue is paused. The draft count follows the held
count, before the pause marker. Counts of zero are left out. The button uses the bar's urgent colour when the queue is paused or a
run awaits approval. It hides itself when no status is available: `status.json` is
missing and `helmraiser status --json` fails. The tooltip repeats the counts and has one
line per held task with the run that holds it, for example
`held: alpha:ci-cache by run 11111111 (awaiting merge approval)`. After the held lines
and before failures, it shows `1 draft awaiting intake` or `<n> drafts awaiting intake`
when drafts exist. Drafts alone do not make the button urgent.

The panel lists the following:

- a pause banner with the reason and, if set, the resume time
- the queue depth and next five tasks in dispatch order (a held one is marked
  `(held)`), with **Pause** / **Resume**, then one line per held task with the run that
  holds it
- the background worker state, PID, running time and its runs against its limit
  (`1/2 runs`), with **Start** / **Stop**
- subscription usage meters for the 5-hour, weekly and monthly windows, plus an
  **On** / **Off** control for caps enforcement
- each running run: task, stage, iteration, cost and minutes
- each run awaiting approval (merge or plan), with **Approve** and **Reject** buttons
- a **Drafts** section with a count, each draft's title, project, size, age and intake
  status (including any intake error), and a **Drop** button; `No drafts` when empty
- the runs that failed in the last 24 h, with their failure reasons
- how long ago the status was written

| Input | Action |
|-------|--------|
| Left click | Toggle the panel |
| Right click | Refresh (`helmraiser status --json`) |
| Middle click | `helmraiser report --since 24h` in a floating terminal |
| `r` | Refresh |
| `j` / `k` (or arrows) | Select a run awaiting approval |
| `a` / `x` | Approve / reject the selected run |
| `Esc` | Close |

## Where the data comes from

`status.json` changes only when a helmraiser command writes it. The widget therefore runs
`<helmraiserBin> status --json` every `refreshIntervalSec`, when the panel opens and on a
right click. That command also rewrites the file. Its output includes the awaiting and
failed rows, which the file does not have. The widget also watches the file for the
writes that `work`, `approve` and `reject` make between ticks. It looks for the file at
`$XDG_RUNTIME_DIR/helmraiser/status.json`, then `$HELMRAISER_DATA_DIR/status.json`, then
`~/.helmraiser/status.json`. After the first successful poll it uses the `status_path`
that the CLI reports.

Both status sources include per-project draft counts. After a successful status read,
the widget runs `<helmraiserBin> drafts list --json` only while drafts exist, and only if
no list request is already running. A successful list read replaces the draft rows;
a failed request keeps the previous rows. A zero draft count clears them without a
list request.

Drop runs `<helmraiserBin> drafts drop <task_id> --reason "dropped from the Helmraiser widget" --json`.
It marks the draft dropped and keeps its file. Like other actions, it allows only one
action at a time, shows the result or error, and refreshes status afterwards. Intake
stays in agent sessions so someone reviews the acceptance block; the widget does not
intake, edit or queue drafts.

The queue, worker and caps buttons run `<helmraiserBin> queue pause|resume --json`,
`<helmraiserBin> worker start|stop --json` and `<helmraiserBin> caps on|off --json`.
Like the approval actions, each allows only one action at a time, reports its result
in the panel and refreshes status when it finishes.

Approve runs `<helmraiserBin> approve <run_id> --json`, and Reject runs
`<helmraiserBin> reject <run_id> --json`. Each runs in the background and refreshes the
status when it finishes. The result, or the error, is shown under the list. A plan
approval returns at once too: it hands the run back to the worker, and the result says
whether the running worker picks it up (`Approved alpha:ci-cache (resuming; the worker
picks it up)`), does not serve that project, or that no worker is running. Reject is
immediate: it removes the run's worktree and keeps its branch.

Commands run through `bash -lc`, so the login `PATH` applies. If `helmraiser` is not on
that `PATH`, set `helmraiserBin` to an absolute path, such as
`<repo>/.venv/bin/helmraiser`.

## Install for development

```bash
ln -s <repo>/omarchy/zsolt.helmraiser ~/.config/omarchy/plugins/zsolt.helmraiser
omarchy plugin enable zsolt.helmraiser right
```

The shell reloads plugin code whenever you save a file. If a change does not apply, run
`omarchy-shell shell rescanPlugins`.

## Settings

Set these on the widget's entry in `~/.config/omarchy/shell.json`:

| Key | Default | Meaning |
|-----|---------|---------|
| `refreshIntervalSec` | `30` | Seconds between `helmraiser status --json` polls (minimum 5) |
| `helmraiserBin` | `"helmraiser"` | Name or absolute path of the helmraiser executable |

## IPC

```bash
omarchy-shell zsolt.helmraiser toggle   # also open, close, show, hide
omarchy-shell zsolt.helmraiser refresh  # refreshes every bar instance
```

## Tests

```bash
node --test omarchy/zsolt.helmraiser/tests/
omarchy plugin validate omarchy/zsolt.helmraiser
```

`Model.js` holds the pure logic: status parsing, labels, age and cost formatting,
urgency, and command construction. The tests cover it, including shell-quoting round
trips through `bash`.
