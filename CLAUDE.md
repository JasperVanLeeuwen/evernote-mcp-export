# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For what the project is, the command pipeline, configuration, output layout, and
the internals ("How it works"), read [README.md](README.md) — it is kept current
and not duplicated here.

## Workflow (required)

**Always start with user stories, then use TDD.** For any change:

1. **Story first.** Add or update a story in [docs/user-stories.md](docs/user-stories.md)
   with acceptance criteria phrased so they map onto tests. No code before there is
   a story that justifies it. Bug fixes get a story too (the missing behavior).
2. **Red.** Write a failing test in `tests/` that encodes the acceptance criteria.
   Run it and confirm it fails for the right reason.
3. **Green.** Write the minimum code to pass.
4. **Refactor.** Clean up with tests green.

## Testing

Tests are `unittest.TestCase` classes run via pytest (`testpaths = ["tests"]`).
They use fakes (e.g. `FakeMCP` in `tests/test_security_and_config.py`) instead of
the network, so the whole suite runs offline. Keep it that way — **no test should
require auth or a live server.**

```powershell
python -m pytest                                                 # all tests
python -m pytest tests/test_convert.py                           # one file
python -m pytest tests/test_convert.py::ClassName::test_method   # one test
```

## Architecture notes

The README's file table covers responsibilities. Two structural facts to respect
when editing:

- **No package — flat modules that import each other.** `verify_export.py`,
  `repair_missing.py`, and `convert_export.py` all `import evernote_export`, and
  `pyproject.toml` declares a single `py-modules = ["evernote_export"]`. Keep the
  scripts in the repo root; don't reach for a package layout without updating
  `pyproject.toml` and every import.
- **`convert_export.py` is format-pluggable.** Add output formats via its
  `FORMATS` registry, not by editing the driver.
