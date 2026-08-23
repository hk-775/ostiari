# AxonLLM upstream provenance

Ostiari bundles AxonLLM so the routing engine used by the gateway is available
from a clean source checkout, in CI, and in the production gateway image.

- Upstream: https://github.com/hk-775/axonllm
- Release tag: `v0.3.1`
- Commit: `a7730a516928272c570da53845248f1f61c31f7c`
- Package: `axon-llm==0.3.1`
- License: MIT No Attribution License (MIT-0)

The upstream `LICENSE` and `THIRD_PARTY_NOTICES.md` files are retained beside
this document.

GitHub transferred the repository from `AxonLLM/axonllm` to
`hk-775/axonllm` on 2026-08-23. Ostiari updates repository hyperlinks from the
immutable `v0.3.1` release metadata; the bundled implementation remains the
exact tag and commit recorded above.

## Updating the bundled source

1. Select an immutable, reviewed upstream tag.
2. Verify the tag resolves to the expected commit.
3. Replace only `LICENSE`, `THIRD_PARTY_NOTICES.md`, `README.md`,
   `pyproject.toml`, `axonllm/`, `src/`, and `config/` from that tag.
4. Update the tag and commit above.
5. Run the full Ostiari gateway suite, including the live AxonLLM contracts.
6. Build the gateway image and run the embedded-router smoke check.

Do not patch the bundled copy without either contributing the change upstream
or documenting the divergence here.
