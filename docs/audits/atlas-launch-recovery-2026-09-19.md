# Atlas launch recovery and verification

Date: 2026-09-19. Source branch: `codex/jev-integration`.

## Reported failure and earlier verification gap

The user's console log showed `cms/ui_assets/index.html` opened through `file://`.
Its root-relative assets resolved to `C:/assets`, and its API requests resolved
to local file URLs. Those requests failed or were blocked by the browser.
The interface requires the Atlas HTTP server and its session bootstrap.

The earlier Jev audit exercised a disposable project served over HTTP. It did
not establish that the user's normal launch method worked, and the readiness
claim was too broad. This audit supplements that earlier, narrower evidence.

The existing `.venv` also referenced a removed Python interpreter. A separate
healthy `.venv-atlas` runtime was installed from the project's declared
dependencies; the old environment was preserved.

## Changes

- `CMS.bat` checks interpreter health and dependencies, binds imports to this
  checkout, preserves explicit project roots, and keeps double-click failures
  readable. It defaults to this project when opened without arguments.
- `Setup-Atlas.bat` explicitly installs the local runtime, with an unattended
  option for CI. Normal launch does not download dependencies.
- All seven HTML entry pages stop application initialization in file mode and
  redirect to a self-contained page explaining how to start Atlas.
- The served main page preserves the existing classic-script scope and loads
  connection assets before application initialization.
- README launch instructions and CI now cover interface recovery and the real
  Windows launcher. CI explicitly uses the selected Python interpreter.

## Verification performed

- Fresh `.venv-atlas` runtime: **559 Python tests passed** in 84.80 seconds.
  Test home was isolated from the real user's library and journal.
- **29 Node interface tests passed**, including all seven file-mode guards,
  recovery-page constraints, HTTP bootstrap ordering, and receipt behavior.
- Windows subprocess tests exercised launcher help from another directory,
  a relative project path with spaces, resistance to a competing local module,
  and missing-runtime recovery for both argument and double-click paths.
- The actual `CMS.bat ui --root . --port 7718 --no-browser` command served this
  CodeCrawl checkout. `/api/meta` identified the expected project root.
- A real browser loaded the project map, its 93 recorded features and connection
  explorer, then navigated through Screens to Context decisions.
- A preview for `cms/scanner.py` saved a local receipt with 8 selected items from
  24 candidates. The explicitly requested file was protected and selected first.
  The page showed Jev off and source freshness unknown rather than claiming
  verified source. No browser warnings or errors were captured in this flow.

## Boundaries

Direct `file://` browser navigation was blocked by the browser tool's security
policy. Recovery behavior was tested through JavaScript contracts, not a visual
file-mode browser test. The supported HTTP application was tested visually.

Viewer mode reads existing project memory. It did not regenerate the map; the
UI correctly displayed its stale-feature warning. No live Jev inference was
performed and no cloud mode was enabled. The packaged desktop executable was
not rebuilt; this validation concerns the source launcher.

The Lean document is a design recommendation. No Lean proof pipeline or proof
of the production application was added. Passing these tests does not establish
all user requirements or the effectiveness of Jev for coding tasks.

## Git handover

The prior source work was preserved in checkpoint `bbd9977`; the selective Lean
direction was recorded separately in `8f157bc`. Launch recovery and its checks
are a subsequent commit. `origin` targets `mrt150683-lgtm/Atlas---Jev`, with the
previous repository retained as `atlas-source`. Local databases, generated
memory, runtime environments and machine-specific settings are excluded from
the new commits. Unrelated existing drafts remain in the working directory.
