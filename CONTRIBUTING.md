# Contributing to Harmony

Thanks for helping improve Harmony by NL Intelligence.

The main [README](README.md) is for discovering, installing, and using the product. This guide is for contributors working on Harmony itself.

## Before you start

Use Python 3.10+ and Git. Work on a feature or fix in a separate branch, and keep changes focused. Do not commit local operational state, tokens, credentials, or project-specific logs. Harmony stores working data in `.workflow/` and `.direct-runs/`, which should remain untracked.

## Run tests

From the root of the source checkout:

```bash
python3 -B -m unittest discover -s harmony/direct-workflow/tests -p 'test_*.py'
python3 -B -m unittest discover -s harmony/task-board/tests -p 'test_*.py'
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

These offline Python test suites cover the direct workflow, Companion task board, and CLI. You do not need to run them to install or use Harmony.

For small documentation-only edits, check the affected Markdown links and code examples. For application changes, run the tests relevant to your changes and expand verification when warranted.

## Release controls

Harmony includes an optional GitHub App PR publisher and a command-level PR guard for Claude writer/correction sessions. They must be configured explicitly and do not authorize autonomous publication. See the [direct workflow guide](harmony/direct-workflow/README.md) for implementation and safety details.

Contributions should preserve the existing boundaries between recorded task completion, human approval, and verified execution. Approval to commit never implies permission to merge or publish.

## References

- [Harmony README](README.md)
- [CLI Command Reference](docs/cli-reference.md)
- [Workflow documentation](harmony/direct-workflow/README.md)
- [Companion and approval documentation](harmony/task-board/README.md)
