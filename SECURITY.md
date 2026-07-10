# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Ostiari, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: security@ostiari.dev

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Security Considerations

Ostiari is a safety layer — it evaluates and gates agent actions but does not execute them. Key security notes:

- **Policy files** should be treated as security configuration and access-controlled
- **Trace storage** may contain sensitive parameters — use the built-in redaction filter
- **Intervention callbacks** should validate the source of approval decisions
- **Fail-open mode** (`fail_open=True`) should only be used in development

## Redaction

Ostiari includes a `RedactionFilter` that removes sensitive patterns from stored traces. Configure patterns in your `OstiariConfig`:

```python
config = OstiariConfig(
    redact_patterns=["password", "secret", "token", "api_key"]
)
```
