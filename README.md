# agents-dispatcher

Run coding-agent CLIs as supervised background workers, with routing, quota
awareness and a watchdog.

Status: pre-release. Nothing here is usable yet.

## Development

The project uses `uv` and Python 3.12 or newer. The test suite is run on
Python 3.12, 3.13 and 3.14; 3.14 is the default interpreter of the project
environment. Run these from the repository root:

```
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked python -W error -m unittest discover -s tests -t . -v
```

## License

Apache License 2.0. See `LICENSE`.
