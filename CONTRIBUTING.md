# Contributing to Ostiari

Thank you for your interest in contributing to Ostiari.

## Development Setup

```bash
git clone https://github.com/hk-775/ostiari.git
cd ostiari
pip install -e ".[dev]"
```

## Running Tests

```bash
# Unit tests
pytest tests/unit/ -v

# Property-based tests
pytest tests/property/ --hypothesis-seed=0

# Integration tests
pytest tests/integration/

# Full suite with coverage
pytest tests/ --cov=ostiari --cov-report=html
```

`tests/` covers the Guard library only. The gateway and control plane have their
own suites, each run from its own directory:

```bash
cd gateway && PYTHONPATH=. pytest tests/
cd control-plane/backend && PYTHONPATH=. pytest tests/
```

Two groups of gateway tests skip unless you opt in:

- **Redis-backed shared store** — set `OSTIARI_TEST_REDIS_URL` to a throwaway
  Redis. The tests call `flushdb`, so do **not** point them at anything you care
  about.
- **AxonLLM routing** — the `@requires_axon` tests need AxonLLM installed and
  `OSTIARI_AXON_ROOT` set. AxonLLM is a separate repo, so CI does not install it
  and these skip there.

`make test` runs the suites the way CI does.

## Code Quality

```bash
# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/

# Type check
mypy --strict src/
```

## Submitting Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-change`)
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass and coverage stays above the gate (`fail_under = 70`
   in `pyproject.toml`, measured over `ostiari` with the TUI and dashboard
   omitted)
6. Run `ruff check` and `mypy --strict src/`
7. Commit with a clear message
8. Open a pull request

## Adding a New Adapter

To add support for a new agent framework:

1. Create `src/ostiari/adapters/your_framework.py`
2. Implement the `FrameworkAdapter` protocol from `src/ostiari/adapters/protocol.py`
3. Add an optional dependency in `pyproject.toml` under `[project.optional-dependencies]`
4. Add unit tests in `tests/unit/test_adapters_your_framework.py`
5. Update README.md with usage example

## Adding a New Anomaly Detector

1. Create `src/ostiari/anomaly/your_detector.py`
2. Implement the `CustomDetector` protocol from `src/ostiari/anomaly/protocol.py`
3. Export it from `src/ostiari/anomaly/__init__.py`
4. Add unit tests and property tests

## Design Principles

- **Zero runtime dependencies on agent frameworks** — adapters are optional extras
- **Fail-safe by default** — if Ostiari can't evaluate, block (configurable to fail-open)
- **Observable** — every evaluation produces a trace
- **Pluggable** — policy sources, storage backends, detectors are all protocol-based
- **Framework-agnostic** — the core never imports framework-specific code

## Reporting Issues

Please include:
- Python version
- Ostiari version (`ostiari --version`)
- Minimal reproduction
- Full traceback

## Security Issues

For security vulnerabilities, please see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the MIT-0 License.
