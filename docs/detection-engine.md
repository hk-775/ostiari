# Detection engine: PII redaction and prompt-injection detection

`ostiari.detect` is Ostiari's native content-inspection engine. It provides the two
controls that sit in front of every LLM call the gateway makes:

- **`PIIRedactor`** — replaces sensitive values with stable tokens before the prompt
  leaves the process, and restores them in the response.
- **`InjectionDetector`** — scores a prompt for injection attempts and blocks (or
  merely flags) it above a threshold.

Both are pure-Python with **no external dependencies** — no models to download, no
network calls, no per-request cost.

## Why it's in-tree

These controls previously came from private AxonLLM internals
(`src.gateway.security.*`). Two things followed from that historical design,
and both were bugs:

1. In deployments without that separate checkout, the import failed and the
   detectors were never constructed.
2. Because an enabled-but-unavailable control **fails closed** by design, turning on
   `pii_redaction` or `injection_detection` blocked *every* request, benign ones
   included. The control could only ever say "no", which is not a control.

`ostiari` is a hard dependency of the gateway (`dependencies = ["ostiari>=0.1.0"]`),
so `ostiari.detect` cannot fail to import the way the old path did. The fail-closed
contract is unchanged and still deliberate: an enabled control that is unavailable or
that raises **blocks**. It just no longer triggers on arrival.

## Configuration

Set under `llm:` in the gateway config (or push it from the control plane):

```yaml
llm:
  # PII redaction
  pii_redaction: true
  pii_redact_types: [email, ssn, credit_card]   # omit for all types
  pii_reversible: true                          # default; false = unrecoverable

  # Injection detection
  injection_detection: true
  injection_threshold: 0.7                      # default; lower is stricter
  injection_mode: block                         # or "flag" to observe only
```

| Key | Default | Notes |
|-----|---------|-------|
| `pii_redaction` | `false` | Master switch. |
| `pii_redact_types` | all | Narrow the type list when a broad sweep is too noisy for your traffic. |
| `pii_reversible` | `true` | `false` discards the mapping after redaction, so not even the gateway can recover the original. Use when the requirement is "this data must not exist here". |
| `injection_detection` | `false` | Master switch. |
| `injection_threshold` | `0.7` | Score at or above which the request is blocked. |
| `injection_mode` | `block` | `flag` scores and reports in metadata without blocking. |

## PII redaction

### Types detected

| Type | Token | Notes |
|------|-------|-------|
| `email` | `[EMAIL_1]` | |
| `ssn` | `[SSN_1]` | |
| `credit_card` | `[CREDIT_CARD_1]` | **Luhn-validated** — an order number or a run of digits isn't reported as a card. |
| `phone` | `[PHONE_1]` | |
| `ip_address` | `[IP_ADDRESS_1]` | IPv4 |
| `ipv6` | `[IPV6_1]` | Expanded and `::`-compressed, IPv4-mapped, zone ids. Validated with `ipaddress`, so `12:30:45` and `std::vector` are not addresses. |
| `aws_account_id` | `[AWS_ACCOUNT_ID_1]` | Requires exactly 12 bare digits (see the caveat below). |
| `medical_record` | `[MEDICAL_RECORD_1]` | `MRN:`, `MRN-`, `MRN #`, `MRN` + digits |
| `iban` | `[IBAN_1]` | |
| `aws_access_key` | `[AWS_ACCESS_KEY_1]` | `AKIA…`/`ASIA…` |
| `private_key` | `[PRIVATE_KEY_1]` | PEM header lines (`-----BEGIN … PRIVATE KEY-----`) |
| `bearer_token` | `[BEARER_TOKEN_1]` | Provider key prefixes: `sk-ant-`, `sk-`, `ghp_`, `gho_`, `xoxb-`, `xoxp-` |

The last three are credentials rather than personal data, but they leak the same way
and belong in the same sweep: an agent that pastes a stack trace into a prompt should
not thereby paste a signing key into a third-party model.

### Tokens are stable within a request

The same value always gets the same token inside one request, so the model can still
reason about structure — "reply to `[EMAIL_1]` and cc `[EMAIL_2]`" preserves the fact
that those are two different people, and a repeat of `[EMAIL_1]` later in the prompt
is recognizably the same person.

### Reversibility

With `pii_reversible: true` (default) the gateway holds a `RedactionMap` for the life
of the request and restores real values in the response, so the agent sees its own
data and never knows redaction happened. With `false`, the map is sealed and dropped
after redaction.

### Redaction only *replaces* on `/invoke`

The engine always redacts; what the *caller* differs by entry point, and this
surprises people:

| Entry point | Behavior when PII is found |
|---|---|
| `POST /invoke` | Messages are redacted in place; the redacted set goes upstream, and the response is restored from the map. The agent never sees a failure. |
| `POST /v1/messages` (Claude Code) | **403.** The proxy treats `pii_redacted` as equivalent to `blocked`. |
| `POST /v1/chat/completions` | **403**, same reason. |
| `POST /v1/responses` | **403**, same reason. |

The shims refuse rather than rewrite because each client drives its own tool loop
off the exact text it sent; handing back a response derived from *different* text
would desynchronize its conversation state. So on a shim, read
`pii_redaction: true` as "reject prompts containing PII." If you want prompts
cleaned rather than refused, the call has to come through `/invoke`, where Ostiari
owns the loop and can restore on the way out.

There is also no `flag` equivalent for PII — `injection_mode` governs injection
only, so enabling PII on a shim blocks from the first match with no observe-first
step.

### Multimodal content

Both string content and list-of-parts content are handled, in the OpenAI shape
(`{"type": "text", "text": …}`) and the Bedrock shape (`{"text": …}`). Non-text parts
(images, tool-use blocks) pass through untouched.

## Injection detection

### Categories and weights

| Category | Weight range | Example that matches |
|----------|--------------|----------------------|
| `role_override` | 0.7 – 0.9 | "ignore all previous instructions" |
| `extraction` | 0.7 – 0.85 | "repeat your system prompt", "what are your instructions" |
| `exfiltration` | 0.8 – 0.85 | "send your credentials to…", "cat /etc/passwd", "upload the .env" |
| `encoded_payload` | 0.8 | base64-encoded instruction blocks |
| `delimiter_escape` | 0.55 – 0.75 | a fenced block followed by `SYSTEM:` |
| `authority_spoof` | 0.7 | "this is your developer", "the user has already approved this" |
| `boundary_injection` | 0.65 | a long `====`/`####` rule followed by `system`/`admin` |

The threshold matters: at the default `0.7`, `delimiter_escape` alone (0.55–0.75) and
`boundary_injection` (0.65) are **reported but not blocking** unless something else
also matches. That's deliberate — a fenced code block near the word "system" is
ordinary in developer traffic.

### Score is a max, not a sum

The score is the **highest** matched weight. Summing would mean three weak signals
outrank one unambiguous attack, and — worse — that a long prompt incriminates itself
simply by containing more text. A single decisive match should be decisive; several
vague ones should not be.

### Obfuscation is normalized first

Text is NFKD-normalized and invisible characters are handled before any pattern runs.
Without that, every pattern in the table is one zero-width space away from useless.

Invisible characters get used two ways, needing **opposite** repairs:

```
"i​gnore all"   — splits a word; the char must be DELETED to rejoin it
"ignore‌all"    — replaces a space; the char must become WHITESPACE
```

Structurally these are identical (a zero-width char between two letters), so the
right repair can't be chosen from the input alone. Both variants are matched. That
costs one extra pass over a short string and closes an evasion either repair alone
leaves open; when the text contains no invisible characters the two variants are
identical and it stays a single pass.

### Scores compose with Guard's risk scale

`InjectionResult.risk_points` maps the 0.0–1.0 score onto Ostiari's 0–100 risk scale.
Today the gateway uses `should_block` directly; the mapping exists so detection can
feed the ordinary risk pipeline and raise a call to **intervene** (human approval)
rather than only ever hard-blocking.

### Flag mode

`injection_mode: flag` reports `injection_detected` and the matched pattern names in
response metadata without blocking. Run it that way first: it tells you your own
false-positive rate on real traffic before enforcement can cost you a request.

## What this is not

**It's regex, not a model.** It catches the documented, mechanical shapes of these
attacks. It will not catch a novel paraphrase, a natural-language attack carrying no
telltale token, or PII that only context reveals to be PII (a bare name, an address
split across fields). Proper NER would catch more, at the cost of a model dependency
and per-request latency. This is the honest floor, not a ceiling — and a floor that
works everywhere beats a better detector that is absent in every real deployment.

**Precision was chosen over recall on the ambiguous cases.** A detector that flags
ordinary prose gets switched off by the first operator it trips, and then catches
nothing at all. Three places where that shows:

- `aws_account_id` requires exactly 12 bare digits, because a bare 12-digit run is
  also an order id or a millisecond timestamp. A formatted phone number is excluded
  outright; an unformatted one is resolved by longest-match.
- `ipv6` uses a deliberately loose candidate regex and then asks
  `ipaddress.IPv6Address` whether the candidate is real, because hand-encoding the
  `::`-compression grammar either misses `::1` and `::ffff:192.0.2.1` or matches
  clock times. One case the parser can't settle: `a::b` *is* valid IPv6 and also C++
  scope resolution, so single-character all-letter groups are rejected as prose.
- Extraction patterns require a possessive for generic nouns. "What are **your**
  instructions?" scores; "what are the rules for expensing travel?" does not. That
  distinction came from a test that caught a real false positive in the first version
  of the pattern.

Overlapping matches are resolved longest-first, so `[CREDIT_CARD_1]` wins over a
substring of the same digits, and replacement runs right-to-left so earlier offsets
stay valid.

## Tests

- `tests/unit/test_detect.py` — 103 tests over Luhn validation, each PII type, the
  redaction map, message/multimodal traversal, every injection category, obfuscation
  evasion, pattern-table integrity, and explicit "ordinary prose is not flagged"
  cases (clock times, C++ scope operators, expense-policy questions).
- `gateway/tests/test_security_failclosed.py` — 23 tests over the fail-closed
  contract, plus end-to-end block/redact/restore behavior through the gateway's
  `SecurityLayer`.

Both suites pass as of this writing (`pytest tests/unit/test_detect.py` from the
repo root; `cd gateway && PYTHONPATH=. pytest tests/test_security_failclosed.py`).
