# Project Notes

## DEVELOPMENT ENVIRONMENT

All work must be done inside the project's virtual environment. Activate it before running any commands:

```bash
source install.sh
```

This creates the venv and installs dependencies on first run, or just activates the existing venv.

## BEFORE COMMITTING - MANDATORY BUILD CHECKS

**NEVER COMMIT WITHOUT RUNNING ALL THREE CHECKS:**

```bash
# Run from project root directory (venv must be active)
ruff check .
ruff format --check .
pytest tests/ -q --cov=kicad_jlcimport --cov-fail-under=80
```

ALL THREE must pass with zero errors before any commit or push.

## GIT WORKFLOW

**NEVER push directly to main.** Always create a feature branch and open a PR. Do not push to or modify main without explicit user permission. Do not revert commits on main without explicit user permission.

## PROJECT STRUCTURE

This project uses the standard **src layout**. The package source lives in `src/kicad_jlcimport/`.

```
src/kicad_jlcimport/         # main package
├── easyeda/                  # EasyEDA data fetching, types, and parsing
├── kicad/                    # KiCad file generation and library management
├── gui/                      # wxPython GUI
├── tui/                      # Textual TUI
├── importer.py               # import orchestration
└── ...
tests/                        # test suite
tools/                        # developer utilities (not part of the package)
```

## TWO UIs

This project has two separate UIs: a wxPython dialog (`src/kicad_jlcimport/dialog.py`) and a Textual TUI (`src/kicad_jlcimport/tui/app.py`). When the user shows a screenshot or describes a UI issue, verify which UI it applies to before implementing changes. Native-looking widgets = wxPython dialog; terminal-rendered widgets = TUI.

## FIXING TEST FAILURES

**Understand the root cause before changing anything.** Don't jump to the first edit that makes tests pass.

- **Never modify production code just to make a test pass.** If a test fails, first determine whether the test or the production code is wrong. Read the production code's docstrings, comments, and recent commit history before deciding.
- **Treat merged/reviewed production code as intentional.** If code is deliberately designed a certain way (especially error handling, security boundaries, or API contracts), fix the tests to match the design — not the other way around.
- **Mock external dependencies.** When tests fail because they make real network, filesystem, or OS calls, mock the external dependency. Don't weaken production error handling to tolerate environment differences.
