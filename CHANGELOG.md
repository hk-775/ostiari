# Changelog

All notable changes to Ostiari will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-22

### Added
- Policy engine with YAML-based rules (allow, block, score-based evaluation)
- Risk scoring (0-100) with configurable thresholds (allow / intervene / block)
- Anomaly detection: loop detection, drift detection, hallucination checks, contradiction detection
- Circuit breaker with configurable failure threshold, recovery timeout, and half-open state
- Multi-framework adapters: OpenAI, Anthropic Claude, AWS Bedrock, Strands Agents
- Intervention gateway for human-in-the-loop approval of medium-risk actions
- Checkpoint/rollback engine for agent state management
- Full trace storage with SQLite backend and redaction filter
- Real-time WebSocket streaming for live monitoring
- Web dashboard (FastAPI) with trace viewer, policy editor, and agent metrics
- Terminal UI (Textual) for local development monitoring
- CLI with validate, traces, report, tui, and dashboard commands
- Policy hot-reload from file, HTTPS, or S3 sources
- `@protect` decorator for inline function guarding
- Health checker with dependency status reporting
- Report generator for trace analytics
- PyPI publish workflow via trusted publishing
- CI matrix: Python 3.10-3.13, Ubuntu/macOS/Windows
- 585 tests (unit, property-based, integration) with 90%+ coverage target
