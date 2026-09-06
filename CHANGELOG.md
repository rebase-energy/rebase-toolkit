# Changelog

## Unreleased

### Added

- **The TUI's workspace switcher lists your workspaces, then their environments.** `w`,
  or a click on the title, now opens two tables: every workspace the signed-in profile
  belongs to, and beneath it the environments of the one under the cursor, which refill
  as the cursor moves. Enter on a workspace hands the cursor down; enter on an environment
  is what switches, so a stray keypress never changes what you are looking at. The
  profiles moved one key further in, to `f`: a profile is which server and sign-in the
  whole machine uses, where a workspace is one tenant on that server. Memberships are a
  person's, not an API key's, so a key-backed profile lists them on the session `rebase
  setup` signed in with (`Client.as_session()`), and says what to do when there is none.

- **Switching workspace or environment in the TUI is now this process's alone.** Neither
  `w` nor `v` writes to `~/.rebase/config.json` any more. The directory marker and the
  profile still decide where `rebase tui` opens, and a switch is free to leave them — it
  says so when it leaves a pinned workspace — but two terminals on one machine can hold
  two different workspaces at once, and the next launch in a directory comes back to that
  directory's workspace. The live selection is exported as `REBASE_WORKSPACE` and
  `REBASE_ENVIRONMENT`, the same variables `--workspace` sets, so an editor or shell
  opened from the TUI inherits it; both are put back on exit. `t` is the one deliberate
  write: it makes the live workspace and environment the profile's default, the way
  `rebase workspace use` does. Picking a profile with `f` still switches the whole
  machine, as before. `Client.with_workspace(id, environment_name=...)` is the clone the
  TUI uses; an API key does not travel across it, for the reason `--workspace` drops one.

- **`i` in the TUI opens a graph pane beside the tables.** On a run, or on the timeline
  of one, it draws the workflow's steps as a directed graph, each step coloured by how
  far that run got — and moving the cursor through the runs table recolours it, so a
  failed step stands out without opening the run. It is the data-dependency graph: an
  edge means one step reads another's output, and the ordering edge the compiler adds
  between consecutive steps is not drawn, so a pipeline that fans one result out to
  many steps draws as a fan rather than a chain with a line to every node. The
  timeline's dependency column follows the same definition. A graph that is wider
  than it is deep is laid out left to right, with the gaps between stacked boxes
  halved to match a terminal cell's height, so a fan of fourteen steps runs down the
  pane's long side instead of being squeezed into one unreadable row. On the
  workflows table it draws the cursor's workflow as deployed, and a workflow with no
  steps shows the project's triggers instead: which workflows run after which, and
  which run when a dataset updates, with the cursor's workflow picked out. A step
  called more than once is labelled by its literal arguments alone, so twelve
  `match-quarter` boxes read `current · fortnox` and so on, the legend names the
  steps drawn that way, and the readout gives the full name — the box is what the
  picture is scaled by, and a step name repeated down a rank is what shrinks the text
  past reading. Hover or click a step to light
  everything it waits on. The pane draws with [plotui](https://pypi.org/project/plotui/),
  an optional extra — `pip install "rebase-toolkit[graph]"` — that renders as a terminal
  image, so it needs Kitty, Ghostty, iTerm2, WezTerm or Konsole; elsewhere, and without
  the extra, the pane says so. `m` while the pane is open gives it the whole screen —
  the graph is drawn at the largest text size that fits, so the width is what makes
  a wide pipeline readable — and `m` or `b` give the tables back. `escape` and `b`
  close it before doing anything else.

- **The workflows table shows a day of run history as a bar chart.** A `History` column
  next to `Last run` — and `Schedule`, `Next run`, `Last run` and `History` now come
  straight after `Origin`, ahead of the provenance columns, so an ordinary terminal
  width shows them without scrolling. One character per hour for the last 24 hours with the time axis
  under the header: the bar's height is how many runs landed in that hour (log scale, so
  an hourly job is still visible beside a per-minute one, drawn with the table's mean
  hour at half height, so a cron that lands the same count every hour is a row of bars
  rather than a solid block) and its colour is the status
  in the hour that most wants looking at — one failure among sixty runs is a red bar —
  in two alternating shades so neighbouring bars stay apart. A
  one-off run of a deployed name counts towards that workflow's row, as it does for
  `Last run`. The counts come from the platform's project overview, which aggregates
  them in the database over the whole window; against an older platform the column is
  built from the runs the table already reads, and is only as deep as that list.

- **`rebase admin` is a superadmin TUI over every workspace.** One row per workspace with
  its members, quota ceilings, and monthly credit; `enter` fills a pane beneath the list with
  the members, the quota as a table, and this month's spend — the list stays in view and the
  cursor moves down into the quota table, so `e` or `enter` edits the highlighted setting
  directly and `escape`/`b` goes back to the list. Edits come from a pick-list where the
  sensible values are few (memory follows Cloud Run's tiers), or a typed number where the
  value is genuinely continuous (milli-vCPU, cents). A
  workspace that has never run anything is listed with the table defaults and flagged
  `defaults` rather than dropped — its compute-policy row does not exist yet, and the
  listing does not create one. `rebase admin workspaces [--json]` and
  `rebase admin set <workspace> --max-memory-mib … --monthly-credit-cents …` are the
  headless twins. Editing the credit grant updates both the policy and the current month's
  grant row, so it takes effect immediately rather than on the 1st; lowering it below what
  the workspace has already spent blocks its compute at once, and the TUI asks twice before
  doing that. The commands are gated by a profile-level superadmin check, so they need a
  session credential (not an API key) whose email is in the API's `SUPERADMIN_EMAILS` — and
  they work on workspaces the superadmin is a member of, which the principal-level
  `rebase workspace compute-policy set` refuses by design.

- **Job workflows can size their own container with `cpu=` and `memory=`.** A `mode="job"`
  workflow gets its own Cloud Run Job, and can now say how big it is, the same way a function
  already could: `@project.workflow(mode="job", memory="2Gi")`. Both accept Modal-style
  numbers (`memory=2048`) or Cloud Run strings (`"2Gi"`), and both require `mode="job"` — an
  interactive workflow shares the Prefect worker's container, so a limit there is refused at
  deploy time rather than accepted and ignored. This is distinct from `resources=`, which
  annotates steps and never sized the workflow's own container. Requests are validated
  against the workspace's `max_cloud_run_cpu_milli` / `max_cloud_run_memory_mib` at deploy
  time, so asking for too much fails with a clear message instead of being silently shrunk
  and OOM-killed at run time. A superadmin sets those ceilings with the new
  `--max-memory-mib` and `--max-cpu-milli` flags on `rebase workspace compute-policy set`,
  and both now appear in `compute-policy show`.

- **First-class environments now combine Modal-style Python ergonomics with GitOps.**
  Workspaces seed `dev`, `staging`, and `prod` and can create arbitrary additional names.
  Projects, compute, runs, routes, schedules, models, secrets, volumes, and buckets are
  isolated by environment, while the SDK supplies `rb.Environment`, ambient context, and
  explicit overrides without deployment YAML. Protected environments track a project to a
  GitHub ref and Python entrypoint; signed pushes reconcile the exact commit in an isolated,
  release-authorized job, prune compute removed from Python only after a successful apply,
  and retain persistent resources. The TUI adds an environment switcher and sibling Projects,
  Buckets, and Secrets tabs. Volumes are isolated by environment like everything else, but
  have no tab yet: the feature is still experimental and the view will follow once its shape
  is settled.

- **Pressing `w` in the TUI opens the workspace switcher.** Workspace switching
  is now available from the keyboard and command panel as well as by clicking the
  workspace title, from either the workspace overview or an open project.

- **Execution is now configured with `mode` and `isolation`.** Functions, models, workflows,
  and `rebase run` default to `mode="interactive", isolation="shared"` for the lowest-latency
  cloud loop. Functions and models can select `isolation="dedicated"` for a private Cloud Run
  service, while `mode="job"` uses a fresh Cloud Run Job execution. Workflows support the
  shared interactive worker and job mode. The former `quick`, `quick_shared`, and `long`
  `run_type` values remain accepted as deprecated compatibility aliases.

- **`rb.artifact(...)` registers durable output URIs on the current run.** It records a
  name, optional logical key, created-or-reused disposition, media type, size, object
  version, digest, and JSON metadata without moving the object itself. Registration is
  strict in hosted runs and a validated no-op locally. Artifacts inherit the active step
  and inline task automatically; mapped function runs use their map-task identity so the
  artifact appears on the canonical parent workflow as well as retaining its producer run.
  `Run.artifacts()` and `Client.list_run_artifacts(...)` expose the records, and the TUI adds
  an `[ Artifacts ]` timeline filter with HTTP links rendered as links. An artifact can point
  at a bucket object instead of a URI, in which case selecting it and pressing `a` resolves
  and opens its current destination rather than storing a physical location up front.

- **The TUI shows where each deployed workflow comes from.** The workflow table now has
  Source and Commit columns: a Git-backed deployment reads `GitHub` beside the shortened
  SHA it is pinned to, while a source stored by Rebase reads `Rebase` and has no commit to
  imply. Pressing `p` on a GitHub-backed workflow shows the full, copyable SHA; pressing
  `g` opens the file on GitHub at that exact commit. When the connected checkout still has
  the commit, the TUI reads the committed file directly from Git and anchors the URL to the
  workflow function's definition line; if it cannot prove the line, it opens the exact file
  without guessing. The provenance data already comes back with the current-version request
  used for workflow step graphs, so the display adds no API round trip.

- **`rebase setup` lets you sign in as someone else.** A stored session used to be reused in
  silence, which put the provider picker out of reach for as long as the token lived: someone
  who signed in with GitHub, and whose invite had gone to a work address GitHub never reports,
  hit "you were not invited", quit, ran setup again, and hit the same wall with no way back to
  the Google button. Setup now opens on a choice — *Continue as `<who>`* or *Sign in with a
  different account* — and the second clears the session and re-asks for the provider, since a
  `--provider` from the failed attempt would otherwise pin the retry to the login that just
  failed. The offer repeats wherever setup runs out of workspaces to give you: alongside an
  invite list, on the join-or-create menu, and after a handle you have no access to, which now
  re-asks instead of ending the run. Switching re-enters the workspace step against the new
  identity rather than making you start over. The question is skipped when stdin is not a
  terminal, so scripted runs are unaffected.
- **Permission failures name the account you are signed in as.** "You do not have access to
  workspace 'acme'" and the beta-enrollment error are only actionable next to the address
  behind them — the two differ exactly when those messages appear. The session now records the
  login provider from the token's `app_metadata`, so setup reports
  `1234+bob@users.noreply.github.com (via github)` and the mismatch is visible rather than
  inferred.
- **The TUI refreshes itself every 10 seconds**, so a screen left open is current rather
  than quietly stale. `rebase tui --refresh-interval 0` turns it off, or any number of
  seconds up to an hour. Only what is on screen is re-read — the workspace view, or a
  project's targets plus whichever boxes below them are open — and a run's timeline is left
  alone once the run has finished, since it cannot change and is the one place a reader is
  likely to be scrolling through output. The tick stands down whenever repainting would
  take something away rather than give something: a dialog is open, a text selection is
  half made, `s` has handed the mouse to the terminal, a delete is in flight, or a key was
  pressed in the last two seconds. It also fails quietly — a dropped request keeps the last
  good data and counts, rather than switching the view and raising a toast; three in a row
  still speaks up. A refresh you asked for with `r` stays loud.
- **A refresh keeps your place.** Cursor, marked rows and horizontal scroll used to reset on
  every repaint, because a repaint rebuilds every row — merely annoying when you pressed
  `r`, unusable on a timer. `_preserve_view` puts all three back, by row key rather than
  index since a repaint is also what reorders and removes rows, and an expanded timeline row
  stays expanded when the run it belongs to is re-read.

- **`rb.Bucket` is object storage that admits what it is.** A bucket maps one-to-one to a
  real cloud bucket and is addressed by key: `put`, `get`, `list`, `stat`, `delete`, and a
  `gs://` URI you can hand straight to pandas, polars or duckdb. `rb.Volume` already stored
  objects, but presented them as a mounted directory, and the mount makes expensive things
  look cheap — editing one byte of a large file rewrites the whole object, rename is not
  atomic, and there is no locking. None of that is visible from a path. Buckets attach with
  `@rb.function(buckets=["forecasts"])`, which injects the URI as `REBASE_BUCKET_FORECASTS`
  and grants the runtime service account access, so code inside a run reads `gs://` at full
  speed without signed URLs. Volumes stay for the case they are actually good at: a library
  that demands a real filesystem path for read-mostly data. Listing is paginated and says so,
  deletion refuses a non-empty bucket rather than timing out halfway through draining it, and
  `buckets:read`/`buckets:write` are granted to Developers, who could already deploy code that
  uses a bucket. Note attachments apply to deployed code only: an ephemeral `rebase run
  ./file.py::fn` carries no buckets, secrets, env or volumes, as it always has.
- **`rebase hillclimb init` and `rebase hillclimb problems` make local search
  setup and problem discovery Rebase-native.** A fresh repository can now create
  its versionable Hillclimb workspace and browse installed emflow targets without
  dropping down to the standalone engine CLI. `rebase hillclimb start` also gains
  `--holdout/--no-holdout`, threaded through local and hosted runs, so public-data
  smoke checks do not require private holdout credentials; holdout remains on by
  default for real model selection.
- **Steps in a workflow are now a chain.** The compiler adds an ordering edge from each step to
  the one recorded before it, on top of whatever the data bindings already imply, so two steps
  that pass nothing between them no longer compile to independent roots. Steps are the sequence
  a workflow runs in — a straight line to draw, and one unambiguous answer to which step failed
  and what never ran because of it; work that wants to run side by side belongs to tasks inside
  a step. `input_bindings` is untouched, so the graph still distinguishes a dependency that
  carries a value from one that is only order. Existing deployments keep the graph they were
  compiled with; the change applies from the next deploy.
- **The footer shows the four keys you move around with** — `tab` (now *Next pane*), `q`, `r`,
  `b` — and the command palette. Ten hints did not fit the width, so `m Maximi` was being cut in
  half and anything after it was simply gone. The rest are still bound and are listed in the key
  panel, which shows hidden bindings too, so nothing became less discoverable than the half-hint
  it replaced.
- **`rb.current_run()`, and `Function.map` attributes its batch to the step that issued it.**
  A map from inside a workflow step *is* that step's tasks, but the platform could not know it:
  the batch is created by the map request, and the request said nothing about where it came
  from. The runner now puts `REBASE_RUN_ID` and `REBASE_STEP_RUN_ID` in the environment,
  `current_run()` reads them, and `run_function_map` attaches them — so `/runs/{id}/tasks` can
  answer "which task failed, and in which step". Read per call rather than cached, because the
  steps of one run share a process and a value captured at import would name the wrong step.
  A map from a laptop still belongs to no run, which the platform accepts.
- **The run drawer separates what a run was given from what it returned.** Its head is labelled
  lines — `Run ID:` spelled out in full rather than truncated, since the drawer is where you go
  to copy it, and `Status:` on its own line in its own colour. Two full-width rules then split
  it into three: that head, the run's `parameters`, and its `result` (with `error` alongside,
  for a run that has one). Each body keeps its own top-level key rather than taking a caption,
  because the key is genuinely part of the document. The rules are Rich's, drawn to the drawer's
  real width, so they are lines rather than a guess at one.
- **`Client.list_run_tasks(run_id, step_run_id=...)`**, and **the TUI's activity view preserves
  each task's real lineage**, one row per unit of work with its own status, parameters and result
  or error. Tasks and artifacts carry their owning `step_run_id`/`task_id` path rather than being
  indented merely because of their type, so a run-level output no longer looks like it belongs to
  whichever task happens to precede it. A step reports one outcome for everything inside it; the
  task rows are where "which one of them failed" survives. Against an API without the route the
  method returns no tasks rather than raising, so an older platform costs the rows and not the
  run view.
- **`[ Activity ] [ Steps n ] [ Tasks n ] [ Artifacts n ] [ Logs ] [ Events ]` over the run
  detail.** Activity keeps the records interleaved by time and adds Type and Scope columns;
  `left`/`right` step between the focused views, while `l` jumps straight to Logs and `e` to
  Events. Steps, Tasks and Artifacts appear only when the selected run actually has them, so a
  simple function run does not advertise three empty branches. Events and Logs remain stable
  observability views and overlap on purpose: the stages are the run's own account of itself,
  so they belong both to "what happened" and to "everything it said". One `Tabs` strip filters
  one table rather than mounting six panes holding slices of the same run.
- **A timeline row too wide for the pane can be read two ways.** The table scrolls sideways —
  `^pgup`/`^pgdn`, the wheel, or the bar — where the others stay clipped, because a log line is
  not a column you can widen your way out of. And `enter`, or a click, opens the row out: the
  full text wrapped to the pane, the row grown to fit, `enter` again to close it. Wrapped
  explicitly rather than left to the column, since the column is as wide as the longest
  *unexpanded* line and that is the width the row is trying to escape. Changing filter or run
  closes them all, rather than leaving an expansion attached to whatever line took that place.
- **Log output arrives with the run** rather than on request. It joins the four reads already
  issued together, so the Logs chip is instant and costs no extra wait.
- **`rebase init [WORKSPACE]`** connects the repository you are in to a workspace you already
  belong to, by writing the committed `.rebase/config.json` marker — and nothing else. No
  sign-in, no workspace creation, and the machine's active workspace is left where it was.
  `rebase setup` could only reach an existing workspace through its "Create a new workspace"
  prompt, which happened to work because creating one you already own returns it, and it
  re-pointed the global profile on the way past. Omit the argument to pick from your workspaces,
  defaulted to the one whose handle matches the folder name. The marker is anchored at the git
  root, so running it from `deploy/rebase/` marks the repo once; running it again is a no-op that
  does not even call the API; changing an existing marker needs `--force`; and a workspace you are
  not a member of is refused with a pointer to `rebase setup`, never created.

- **`o` opens a project's source file from the TUI**, from the project list or from inside a
  project. A project is a platform record with no path on this machine, so the file is found by
  searching this workspace's search paths for the `rb.project(...)` call that names it — which
  means a file that has moved, or was never deployed from this computer, still resolves. The
  name is read out of the AST rather than matched as text, because the real idiom passes a
  constant (`rb.project(PROJECT_NAME)`) and a text search for `rb.project("epex")` finds
  nothing. Candidate files are never imported. When more than one file declares the project a
  picker asks which; the answer is deliberately not remembered, since a stored path is exactly
  what goes stale. Editor resolution is `REBASE_EDITOR` → the `editor` config key → `$VISUAL` →
  `$EDITOR` → the first of `code`/`cursor`/`zed`/`subl`/`idea` on `PATH` → a macOS application
  bundle. Terminal editors get the terminal, via suspend, and get it back on exit. The file's
  git repository opens with it as the editor's workspace, so the sidebar shows the project
  tree rather than a lone file — the repository rather than the file's own directory, which
  for a deployment file is usually a `deploy/` folder several levels down.
- **`rebase project open`** does the same from the command line, with `--path` to print the
  resolved `file:line` instead of launching anything and `--json` for the full search result.
- **`rebase project search-path {list,add,remove}`** manages where that search looks. The TUI
  records the git repository it was started in automatically, so the common case needs no
  setup; `add` refuses your home directory without `--force`, since registering it would turn
  every lookup into a scan of everything you own.
- **Cron jobs and Endpoints columns in the TUI's project table**, so the workspace view shows at
  a glance what a project holds and how much of it runs on its own. Both overlap the existing
  columns by design: an endpoint belongs to a function or workflow rather than sitting beside
  one, and a cron job *is* a workflow, so a project with 1 workflow, 1 cron and 1 endpoint has
  one deployed thing that both fires on a schedule and answers over HTTP. A workflow counts as a
  cron job when the API gives it a `next_run_at` — the platform's own verdict, which already
  accounts for a missing or paused schedule, a disabled workflow or version, and an unusable
  cron expression, rather than a second copy of those rules here that could drift. Neither
  column costs startup time: crons fall out of the workspace-wide `/workflows` call already
  being made, and endpoints come from one `/endpoints` call issued alongside it. The project
  detail panel carries both counts.
- **`d` deletes from the TUI**, with `shift+up` / `shift+down` to mark a range of rows first.
  It acts on the projects table in the workspace view and on the workflows/functions table of
  the active tab inside a project; marked rows turn amber and are counted in the title. Every
  delete goes through a type-to-confirm dialog — one row asks for its own name typed back, a
  batch asks for the word `delete` — and then deletes with `force`, so contents and run history
  go with it. ASGI apps and runs have no delete endpoint, so `d` declines there.
- **`s` hands the mouse back to the terminal** so text can be selected and copied the way it is
  in any program that never took the mouse — drag, then the terminal's own copy shortcut
  (`cmd+c` on macOS). Pressing `s` again takes the mouse back for hover, clicking and wheel
  scrolling. The two cannot be had at once: while mouse reporting is on the terminal forwards
  drags to the app rather than selecting, which is why `cmd+c` had nothing to copy. Keys work in
  both modes, and the title says which one is active.
- **`shift+left` / `shift+right` adjust the TUI's text selection** after a mouse drag, growing
  or shrinking it at its trailing end so a copy can be trimmed without re-dragging. It stops at
  the ends of the line rather than wrapping, and leaves selections that span several widgets
  alone. Copying itself is Textual's own `ctrl+c` / `cmd+c` binding, unchanged.
- **`Client.delete_projects(ids, force=...)`** deletes many projects in one request, against the
  platform's new `POST /projects/batch-delete`. It returns `(project_id, error)` per failure
  instead of raising, because the route is deliberately not atomic — a project delete tears down
  Cloud Run services and Prefect deployments, which cannot be rolled back, so it reports on each
  project rather than pretending the set succeeds or fails together. Against an API too old to
  have the route it falls back to one request per project, so a toolkit ahead of its platform
  still deletes. The TUI's `d` uses it for a marked run of projects; functions and workflows have
  no batch route and stay a concurrent fan-out.
- **`RebaseWorkflowError.status_code`** carries the HTTP status behind a failure, so callers can
  tell a route this API version does not have from a genuine error.
- **The TUI's clock names its zone** (`10:56:03 CEST`) and sits flush against the right edge
  instead of a couple of cells short of it. **Clicking it picks the zone** every time in the TUI
  is shown in — the clock and every table timestamp, which arrive from the API in UTC and were
  previously displayed that way whatever the clock said. The picker filters the full `zoneinfo`
  list from a box that keeps the focus, so the arrow keys drive the list while you type. The
  choice lasts for the session; it is not written to the profile.
- **The TUI's project view reveals a box at a time.** Entering a project now shows the
  Workflows/Functions switcher and its table filling the screen, and nothing else. Selecting a
  target adds its runs; selecting a run adds its timeline. `b` closes the boxes in reverse before
  it leaves the project. The view previously painted all seven boxes up front, most of them
  holding a "Select a ..." placeholder. The ASGI apps tab appears only for a project that has
  one, and the project's name moved into the header title, next to the workspace.
- **One timeline per run**, in place of the separate events and steps tables. The two routes and
  the run's log output describe the same couple of minutes, and reading them in three places
  meant reconstructing the order by eye. **`l` folds the logs in**, all of them at once, so the
  run reads as one scrollable sequence with each log line under the step or stage it followed —
  chronologically, because a log entry carries a timestamp and a severity and nothing that names
  a step. Expanding also shrinks the two boxes above it, since that is the one view that wants
  the whole screen. Log lines are dimmed and indented, steps are picked out in amber, and the
  first 200 lines are what the API will serve.
- **`p` opens the selected row as JSON** in a drawer over the right of the screen — a workflow or
  function with its endpoints and their absolute URLs and its place in the step graph, a project
  with its counts. A **run** shows only what it was asked to do and what came back: its
  parameters, its result, and its error when it has one. The rest of a run's record is backend
  plumbing — provider ids, version ids, flags — and its status and timings are already the row
  you opened it from. `p` or escape closes the drawer. A workflow's `source_code` is replaced by
  its length, since printing the whole deployed body would bury every other field and `o` already
  opens the file.
- **Opening a box hands it the focus**, so the arrow keys drive whatever you just asked for
  without a `tab` in between: enter on a workflow moves you into its runs, enter on a run moves
  you into its timeline. Closing one with `b` hands the focus back up rather than stranding it
  off screen. **`tab` moves between the boxes** — target table, runs, timeline — instead of
  switching the top box's two tabs and leaving the rest reachable only with the mouse. Switching
  Workflows / Functions / ASGI apps is `left` and `right` now, which also carry the focus with
  them so `tab` keeps its place.
- **The boxes are resizable.** `+` and `-` grow and shrink the focused one in two-row steps, `m`
  gives the focused box the whole view and `b` or `m` again gives it back, and `0` puts everything
  back to the per-level defaults. Rows come from the other boxes nearest-first and keep going:
  pushing the timeline up eats the runs table and then carries on into the target box, rather than
  stopping dead the moment its neighbour is empty. The floor is a box's column header — one row
  for a table, three for the tabbed box, which spends two on its tab strip before the table inside
  gets to draw anything — so a squeezed box still says what is open and what each of its columns
  means. Explicit sizes outrank the defaults, including the ones `l` sets.
- **A box's column header is also the handle that drags the boundary above it.** The usual answer
  is a splitter row between the panes, and that is what this was, hint text and all — but a row
  per boundary is a row the tables do not get, and the header is already sitting on the boundary,
  pinned there while the rows scroll underneath. So it *is* the splitter: grab the runs table's
  header and the target box above follows the pointer. It lights up under the pointer, which is
  the only affordance a terminal has to offer; a press anywhere else in the table is still an
  ordinary press.
- **Workflow and Step columns in the TUI's functions table**, so a step stops looking like a
  loose function. A step *is* a function — the deploy registers it as one — and the row had
  nothing on it to say which workflow calls it, or in what order. Both columns are read off the
  current workflow version's compiled step graph, which is also what puts the rows in graph
  order: a workflow's steps now run down the table in sequence, under the workflow that calls
  them, with functions no graph mentions last. The Step column carries the node key, which
  differs from the function name only when one function is called twice; `p` spells the wiring
  out per step and per workflow. A workflow whose body does the work itself has no graph at all —
  the common case — and leaves the two columns empty. The graph lives on the version rather than
  the workflow, so this costs a request per workflow, issued concurrently; like the endpoint list
  it is supplementary, and a route that fails costs the column and nothing else.
- **Trigger, Started and Duration columns in the TUI's runs table**, which showed only when a run
  was created and when it finished — so a run that queued for 90 seconds before starting read as
  a slow run rather than a delayed one, and nothing distinguished a scheduled firing from an
  ad-hoc `api` dispatch. Backend moved out of the table to make room; it was the widest column
  and `p` has it. The TUI also loads 100 runs per target now rather than 25, matching
  `rebase runs list`.
- **`-h` is an alias for `--help`** on every command and subcommand.
- **Short flags for CLI options**: each option now also answers to `-x`, where `x` is the first
  letter of its long name — `rebase deploy -n api -e prod`, `rebase workflow list -j`. 235 of 269
  options got one. The remaining 34 lost the letter to an option declared earlier in the same
  command (`rebase deploy --sync` has no `-s`, because `--source` took it), and `-h` is reserved
  for `--help` throughout. Hand-written short flags are unchanged.

### Changed

- **Run lists are summaries; a run's body is one request away.** `Client.list_runs`,
  `rebase run list` and the overviews behind the TUI no longer carry each run's `result`
  and `parameters`. A list is read to see what ran, and one run's result can be a
  megabyte: a project whose functions return large results could not be opened in the
  TUI at all, because 200 of them at once was more than the platform would send. Each
  row now says how big the bodies are instead — `result_bytes`, `parameters_bytes` — and
  `Client.list_runs(include=["result", "parameters"])` or `rebase run list --include
  result,parameters` asks for the whole records where a script wants them. `rebase run
  get` and `Client.get_run` are unchanged. In the TUI, `p` on a run opens the drawer at
  once on the sizes and reads the record behind it, once, off the screen's thread; a run
  opened with Enter is not read twice. Against a platform older than this the lists
  still carry the bodies, and nothing else changes.

- **The TUI's timer no longer repaints a view that did not change.** The overview routes
  answer with an ETag, and the periodic re-read sends it back: a platform that confirms
  nothing changed (`304`) costs one round trip and no repaint, which is what most ticks
  are. `r` still re-reads in full. `Client.read_workspace_overview` and
  `read_project_overview` are the reads that can answer "unchanged"; the `get_*` pair
  keep their shape. The project overview also now carries the platform's own folding of
  one-off runs per name (`ephemeral_targets`) and the newest run of each deployed target
  (`latest_runs_by_target`), so a workflow idle since yesterday keeps its Last run even
  though the page of runs the view is sent got shorter. Responses come back gzipped.

### Removed

- **The TUI's summary bar and project detail panel** — the `Profile | API | Projects | ...` line
  and the `Project epex / Functions: 0 | Workflows: 1 / Updated | ID` panel under it. Between them
  they cost seven rows at the top of the project view to restate what the project table already
  showed. The project's name is in the header title now, its counts stay in the workspace table,
  and what is left is a one-line error panel that appears only when a request fails.

- **The TUI's target and run detail panels** — the `Workflow epex-bid-curves / Project: epex |
  Run type: long / State: enabled | ...` block and the `Run 8547eb63... / Status | Backend /
  Parameters: {"batch_size": 8, "days_back": 1, ...` block below it. Twelve permanent rows in the
  middle of the project view, almost all of it a second copy of the table row directly above, and
  the one thing they had that the tables did not — a run's parameters and result — was truncated
  at one line each. Both are `p` now, whole and scrollable, and the rows they cost went to the
  three tables that show something different on every line.

### Fixed

- **`?` shows every key the screen answers to.** The panel listing them was reachable
  only through the command palette, which is a poor place to keep the answer to "what can
  I press here" — and the footer, by design, has room for four hints out of a dozen. `?`
  is now the fifth, and toggles the panel: `?` again, `b` or `escape` put it away.
  (`k` was not a candidate: it is spoken for by `j`/`k` paging.)

- **`b` and `escape` close the keys panel.** The panel the command palette opens under
  "Keys" could only be closed from the palette again: Back walked the views underneath it
  while the panel stayed put. It is the outermost thing on screen, so it is now the first
  thing Back closes, and the press that closes it does nothing else.

- **The three rows at the top of the screen are one band.** The header, the chip strip
  under it and the column header under that were three colours — the app background, and
  Textual's `$panel` blue-grey on the two below, with the workspace chip strip falling
  through to the background and the clock a few percent lighter again. All of it is one
  grey now (`#232826`), in both views — and so are the column headers further down the
  project view, so the furniture around the rows reads as furniture rather than as
  stacked bars in three colours.

- **Two fingers scroll a table sideways.** A horizontal swipe now steps two cells and
  lands immediately, where Textual's own handling animated four cells a notch: a trackpad
  sends a burst of notches, and four animated cells apiece slid the table end to end
  behind the fingers. It drives the same bar `shift`+wheel and dragging do, on the tables
  that scroll — projects, workflows, functions and the timeline. The tables of
  fixed-width fields are still clipped by design.

- **`left` and `right` step the workspace's Projects / Buckets / Secrets chips.**
  The gesture was bound only on the project view's target tables, so on the workspace
  overview — the first screen `rebase tui` opens — the arrows did nothing at all and the
  tabs could only be reached with the mouse. They now step whichever chip strip sits
  above the focused table, wrapping either way round and carrying the focus onto the table
  the chip opened, the same way they already worked for Workflows / Functions and the
  timeline filters.

- **Automatic refresh no longer makes the workflow table pulse between two widths.** The
  fast first paint used when opening a project contains names and schedules but not the
  version-backed Source, Commit, step graph or last-run time. Reusing that progressive
  paint during a refresh replaced a complete table with dashes every tick, resized its
  columns, then reversed the change when the detail reads returned. Refresh now leaves the
  previous complete frame on screen until its complete replacement is ready; progressive
  painting remains in place only when there is no existing project table to look at.

- **A finished run no longer shows a stage as still running.** The platform opened stages it
  never closed — `step-graph` on every workflow run, `submit` on two function paths — so a run
  that had succeeded minutes earlier kept a row reading `running`, in the brightest green on
  the screen. The emitters now say `info` for an announcement that introduces work rather than
  reporting on it, which is what the event enum has always had for the purpose. Events already
  written keep their status forever, so the timeline also mutes a non-terminal status on a run
  that has reached `succeeded`, `failed` or `cancelled`: the word is still what the API sent,
  since inventing a status would be worse, but it no longer reads as live. The CLI renders
  `info` as a neutral bullet rather than a green tick, and its streaming path now shares the
  snapshot path's status mapping instead of carrying a second copy that had to be taught each
  new status separately.

- **Log lines in the timeline no longer borrow columns that mean something else.** A log row
  carries no stage, so the four-space indent meant to nest it landed in an empty Stage cell and
  rendered as nothing — the one kind it was meant to set apart got no indent at all, while
  tasks did. Severity had the matching problem in Status, a column that means lifecycle for an
  event and outcome for a step, where `INFO` is neither and only ever repeated the `log` type
  beside it. Log rows are now flush with the messages around them, Status is left to the two
  vocabularies it belongs to, and a severity worth noticing leads the message in its own colour.

- **Clicking the header of an empty table no longer crashes the TUI.** Textual's
  header-click handler reads `ordered_columns[column_index]` without checking the list is
  populated, so a click there raised `IndexError` and took the whole app down — reported
  from the timeline pane, reachable in every table. Every table here can be in that state
  by design: columns arrive with the rows, so an unloaded or cleared table has none while
  still drawing a header row. A shared `HeaderSafeDataTable` base swallows the click, using
  `prevent_default` rather than `stop` because Textual invokes `_on_click` for every class
  in the MRO — stopping the bubble to parents does not stop the base handler that indexes.

- **The timeline no longer empties itself while you are reading it.** An automatic refresh
  re-reads the runs box, and drawing that box cleared the timeline underneath — right when a
  different target is opened, since that timeline belongs to another run, but wrong on a
  refresh. A finished run is deliberately not re-read, so nothing put it back: the screen
  went blank until an arrow key redrew it from memory. The clear now only happens on a fresh
  selection.

- **Opening a run took about 1.3 seconds.** The run, its events, its steps and its tasks were
  four sequential round trips behind one keypress — measured at 266ms, 330ms, 314ms and 353ms
  against the deployed API — and none of them needs another's answer. Issued together they cost
  the slowest one instead of the sum, so the wait is now about 350ms. The caller passes the run's
  target type from the row it just selected, so the steps and tasks reads no longer wait to be
  told whether they apply.

- **The background no longer changes shade between views.** Textual tints a focused DataTable
  five percent lighter, and in the workspace view one table fills the screen — so walking into a
  project visibly darkened the whole app and walking back out lightened it again. The tint is
  off; the focused pane is still the one with the bright cursor row, which says it in colour
  rather than by washing the background.

- **Notifications no longer look like they belong to another program.** Textual's toast is a
  grey `$panel` slab 60 cells wide whatever it has to say — the one widget on screen still
  wearing the default theme. It is a bordered card in the brand green now, sized to its text and
  standing clear of the footer instead of sharing a row with the key hints. Stacking and the
  severity split are Textual's and are kept, re-pointed at the brand green, amber and coral, so
  a warning and an error read apart before you read them.

- **`p` did nothing in the timeline box.** Tasks and artifacts now open their own full records,
  including scope and producer-run provenance, while a lifecycle event, step or log line opens
  the run it belongs to.

- **Every project's runs showed under every other project's workflow.** Opening a workflow in
  the TUI, or running `rebase workflow runs <name>`, listed every run in the workspace: the
  nordpool workflow showed epex's runs, with epex's parameters and epex's GCS paths in them.
  `/runs` filters on `target_id` — a run names what it ran through `target_type` + `target_id`,
  and its own `workflow_id` column is null for an ordinary registered run — but the client sent
  `workflow_id`, a parameter the route does not have. FastAPI drops unknown query parameters
  without complaint, so what looked like a filter was nothing at all and the whole workspace came
  back. `list_runs` now sends `target_id`; `workflow_id` / `function_id` / `model_id` are kept as
  spellings of it, and contradicting them raises rather than silently picking one. The fake API
  in the tests had the same phantom parameter, which is why the suite never noticed.

- **Deleting a project failed with `Method Not Allowed` against an API without the batch-delete
  route.** The fallback to one request per project was reached on a 404, and 404 is not what such
  an API answers: `/projects/batch-delete` is matched by `/projects/{project_id}`, so the path
  resolves with `project_id="batch-delete"` and only the method is refused — `POST` comes back 405
  with `Allow: GET`. The 405 was treated as a genuine error and surfaced instead, so `d` on a
  project could not delete anything at all. Both statuses now mean "this route is not here".

- **An empty workspace no longer looks like one that is still loading.** The TUI drew nothing
  either way, so pointing a profile at an empty workspace read as a hang. It now says
  `No projects in workspace <id>` and how to refresh or switch.

- **The TUI's ASGI apps tab no longer flashes on the way into a project without one.** Hiding it
  only ran once the project's data had landed, so every project opened with three tabs and then
  dropped to two a moment later. It now starts hidden and appears only when there is something
  behind it, which is what the tab was always meant to mean.

- **A directory's `.rebase/config.json` marker now overrides the active workspace**, not just the
  profile chosen to reach it. The marker previously only won when some profile already stored that
  `workspace_id`, so a repo pinned to a workspace you had no dedicated profile for silently ran
  against the globally active one instead — `rebase tui` inside the rebase-grid checkout opened
  `agent-work`. Nothing about that needed a second profile: the workspace travels as one
  `X-Rebase-Workspace` header and an API key reaches every workspace you belong to, so the marker
  now sets the header on the active credentials. Precedence is `Client(workspace_id=...)`, then the
  marker, then the profile — so switching workspace by hand still wins inside a marked repo, and
  the TUI's own switcher passes the choice explicitly for that reason. The TUI's title names the
  workspace the data actually came from, and says so if a pin was not honoured.

- **Selecting a run could crash the TUI.** Textual reads a plain `str` passed to `Static.update`
  as content markup, so a run whose parameters, result or error contained a `[` raised
  `MarkupError` inside the worker and took the app down. The detail panels render `Text` now,
  which is never parsed.

- **The TUI's column headers no longer appear before their data and then jerk into place.** A
  DataTable sizes its columns from the header text until the first row lands, so headers added at
  mount were laid out against `Project | Workflows | Functions | Endpoints` and snapped to new
  widths the moment the projects arrived. Headers are now added in the same paint as the rows, so
  a loading table is simply empty, and clearing a table takes its header with it rather than
  leaving one sized for data that is gone.

- **Deleted rows leave the TUI immediately instead of after every request has returned.** A marked
  run of projects was deleted one round trip at a time and then followed by a full workspace
  reload, so rows the user had already confirmed gone sat on screen for seconds. The rows now come
  off as soon as the dialog is confirmed, the deletes are issued concurrently, and the reload only
  happens if one of them fails — in which case the surviving rows come back. Projects go in a single
  batch request; functions and workflows have no batch route and stay a concurrent fan-out, so they
  are still one request each, just no longer one wait each.

- **`rebase tui` starts in about a second instead of stalling on the project list.** The workspace
  overview issued two requests per project — one for functions, one for workflows — end to end, so
  startup was `2 × projects` round trips deep: ~13.5s on a 21-project workspace. Workflow counts now
  come from a single workspace-wide `/workflows` call (every workflow carries its `project_id`), and
  the function counts, which have no workspace-wide route, are fetched concurrently. Same numbers,
  ~1.8s.

- **Unknown options and commands print a usage error again** instead of a Rich traceback, and exit
  2 rather than 1. typer >= 0.26 vendors its own copy of click, and the vendored exception classes
  do not subclass the ones in the `click` package, so the CLI's `except click.ClickException`
  handler never fired for anything typer's parser raised. `Abort` (Ctrl-C at a prompt) was affected
  the same way.

## 0.6.1 — 2026-08-07

### Fixed

- `rebase-toolkit[hillclimb]` now installs the published `hillclimb` 0.2 release
  with its compatible `emflow` 0.3.1 integration and packaged benchmark data.
- The `rebase` compatibility package now forwards its `hillclimb` extra.

### Changed

- `rebase-toolkit[all]` excludes Snowflake because its current pandas constraint
  conflicts with hillclimb's pandas 3 requirement; install the `snowflake` extra
  separately.

## 0.6.0 — 2026-07-07

### Added

- **GitLab integration**: `rebase connect gitlab [group/project]` connects a GitLab
  repository (gitlab.com or self-managed via `--host`) with an access token — resolved
  from `--token`, `$GITLAB_ACCESS_TOKEN`, or a hidden prompt, and validated against the
  GitLab API. The token is stored in the platform's Secret Manager (never in the Rebase
  database); reconnecting rotates it. Provides the same surface as the GitHub
  integration: workspace/project source backing, repo file reads, starter workflows,
  and promotion merge requests. Client wrappers: `connect_gitlab_repo`,
  `list_gitlab_repo_connections`, `get_gitlab_repo_file`,
  `create_gitlab_starter_workflow`, `create_gitlab_promotion_mr`.

## 0.5.0 — 2026-07-07

### Added

- **Canonical energy layout for BigQuery** (`rebase.sources.energy`), mirroring rebase's
  open data model (energydatamodel/timedatamodel) and its ClickHouse persistence: an
  append-only `series_values` table with three time axes (`valid_time`,
  `knowledge_time`, `change_time`) plus a `series` catalog keyed by
  `(path, data_type, name)` with a deterministic `series_id`.
  `BigQuerySource.ensure_energy_schema` / `register_series` / `write_series` /
  `read_series` cover schema creation, idempotent registration, SIMPLE/VERSIONED writes
  (corrections are new rows), and point-in-time reads with timedb semantics: latest,
  `as_of` knowledge cutoff, `overlapping` (every forecast issue), and the full audit
  trail. `rebase.sources.SeriesKey` is exported. The `QUALIFY`-based read builder is
  dialect-portable to Snowflake and Databricks.
- BigQuery query parameters now support `datetime` values (`TIMESTAMP`).

## 0.4.0 — 2026-07-07

### Added

- **Per-function `cpu=` / `memory=`**, Modal-style, on functions and models (including
  `Predictor` and the other model classes): `@rb.function(cpu=2, memory=1024)` (cores /
  MiB as numbers) or Cloud Run strings (`cpu="500m"`, `memory="1Gi"`), and as class
  attributes on models. Applies to both the isolated Cloud Run service and Cloud Run
  Jobs backends; requests are clamped by the workspace compute policy. Unset means the
  platform defaults.

## 0.3.0 — 2026-07-07

### Added

- **Secrets, Modal-style.** `rebase.Secret` — a named bundle of environment variables
  attached to deploys with `secrets=[rebase.Secret.from_name("acme-snowflake")]`; every key
  becomes an env var at run time. Build bundles with `Secret.from_name`, `Secret.from_dict`,
  or `Secret.from_dotenv`. Values live in the platform's Secret Manager and are injected by
  Cloud Run — they never pass through the deploy payload or source snapshot.
- **`rebase secret` CLI**: `create NAME KEY=value ... [--from-dotenv FILE] [--force]`
  (use `KEY=-` to read one value from stdin), `list`, and `delete`.
- **`env=` / `secrets=` on functions and models** (previously ASGI apps only), including
  `Predictor` and the other model classes — credentialed code now runs on every backend.
- **`rebase.sources` warehouse connectors** for Snowflake, Databricks, BigQuery, and
  Microsoft Fabric behind per-warehouse extras (`rebase-toolkit[snowflake]`,
  `[databricks]`, `[bigquery]`, `[fabric]`, or `[sources]` for all four): a uniform
  `read` / `read_bitemporal` / `write` surface with a leakage-safe bitemporal mapping
  (`BitemporalSpec`) for honest emflow backtests.
- Client methods `set_secret`, `get_secret`, `delete_secret`, `list_secrets`.

## 0.2.0

Initial public toolkit: projects, functions, workflows, models, endpoints, runs,
images, hillclimb, GitHub and Hugging Face integrations.
