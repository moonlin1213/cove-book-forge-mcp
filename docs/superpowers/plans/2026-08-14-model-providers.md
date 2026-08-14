# Model Provider Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Every task follows red-green-refactor, receives an independent review, and ends in a focused commit.

**Goal:** Add safe, testable model-provider adapters for DeepSeek and other OpenAI-compatible APIs, Anthropic Claude, and user-supplied custom providers without coupling book analysis to one vendor.

**Architecture:** An async `ModelProvider` protocol owns text generation, JSON generation, capability reporting, health checks, and cumulative usage. Vendor adapters translate that boundary to their HTTP APIs through a shared bounded request gate. A small factory resolves configured built-ins or explicitly registered custom providers; it never performs silent cross-provider fallback.

**Tech Stack:** Python 3.11+, Pydantic 2, HTTPX async client, asyncio, pytest with `MockTransport`, Ruff, strict mypy.

## Global constraints

- API keys are read from the configured environment-variable name only; they are never serialized, logged, returned, or written to disk.
- DeepSeek uses the OpenAI-compatible adapter with its configured API base. No DeepSeek-specific SDK is required.
- Provider selection is exact. An unavailable, rate-limited, or invalid provider response never falls through to another cloud.
- Requests are bounded by timeout, configured concurrency, and requests-per-minute limits. No unbounded retry or JSON-repair loop is allowed.
- Public errors retain the existing closed representation and never contain response bodies, prompts, URLs, keys, or private book text.
- Tests use mocked transports and fake providers only; they never require a live account or network access.
- This phase provides raw text/object generation. Chapter-analysis schema validation, fingerprinting, caching, and bounded repair belong to the next phase.

## Locked public interfaces

Create `providers/base.py` and re-export its public types from `providers/__init__.py`:

```python
class ProviderCapabilities(ContractModel):
    json_mode: bool
    json_schema: bool = False
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    reasoning_parameters: tuple[str, ...] = ()

class ProviderUsage(ContractModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0

class TextGeneration(ContractModel):
    text: str
    model: str
    usage: ProviderUsage

class JsonGeneration(ContractModel):
    value: dict[str, JsonValue]
    model: str
    usage: ProviderUsage

class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...
    @property
    def usage(self) -> ProviderUsage: ...
    async def generate_text(...) -> TextGeneration: ...
    async def generate_json(...) -> JsonGeneration: ...
    async def healthcheck(self) -> None: ...
```

The concrete method parameters are `system_prompt`, `user_prompt`, `max_output_tokens`, and optional `temperature`; `generate_json` additionally accepts an optional JSON-schema mapping for providers that advertise schema support. Public result models remain strict and frozen.

---

### Task 1: Define the provider boundary and factory

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/cove_book_forge/config/models.py`
- Create: `src/cove_book_forge/providers/__init__.py`
- Create: `src/cove_book_forge/providers/base.py`
- Create: `src/cove_book_forge/providers/factory.py`
- Create: `tests/test_provider_contracts.py`
- Create: `tests/test_provider_factory.py`

- [ ] Add failing tests for strict/frozen capability, usage, text, and JSON contracts; the async protocol; exact built-in selection; unknown-provider failure; explicit custom-factory injection; and missing/empty API-key environment variables.
- [ ] Add `httpx>=0.28,<1`. Extend `ModelConfig` only with bounded request timeout and output-token defaults needed by every adapter; keep configuration backward compatible.
- [ ] Implement an async `ModelProvider` protocol and a factory for `openai`, `openai-compatible`, `deepseek`, and `anthropic`, plus a registry of caller-supplied factories. Unknown names return `CONFIG_INVALID` with only `model.provider` as public detail.
- [ ] Resolve secrets at provider construction without retaining the environment-variable name in request payloads or exposing the value in model dumps/errors.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: define model provider boundary`.

### Task 2: Add the OpenAI-compatible provider

**Files:**

- Create: `src/cove_book_forge/providers/transport.py`
- Create: `src/cove_book_forge/providers/openai_compatible.py`
- Create: `tests/test_openai_compatible_provider.py`
- Create: `tests/test_provider_transport.py`

- [ ] Add failing `httpx.MockTransport` tests for OpenAI and DeepSeek URL construction, authorization, message payloads, plain text, JSON-object mode with an explicit JSON instruction, usage accounting, model identity, health check, malformed/truncated responses, and safe HTTP/network/timeout error mapping.
- [ ] Implement a shared async semaphore and deterministic requests-per-minute gate with injectable clock/sleep for tests. Each public generation call performs at most one HTTP request in this phase.
- [ ] Implement `OpenAICompatibleProvider` against `<api_base>/chat/completions`, using the configured base exactly as an API prefix. Defaults are `https://api.openai.com/v1` for OpenAI and `https://api.deepseek.com` for DeepSeek.
- [ ] Map 401/403 to `MODEL_AUTH_FAILED`, 429 to retryable `MODEL_RATE_LIMITED`, network/timeout/5xx to retryable `MODEL_UNAVAILABLE`, and malformed, empty, non-object JSON, or incomplete model output to `MODEL_OUTPUT_INVALID` without leaking response content.
- [ ] Track cumulative usage from provider token fields and expose per-call usage in the returned result.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: add openai compatible provider`.

### Task 3: Add Anthropic and custom-provider support

**Files:**

- Create: `src/cove_book_forge/providers/anthropic.py`
- Create: `examples/custom_provider.py`
- Create: `tests/test_anthropic_provider.py`
- Modify: `tests/test_provider_factory.py`

- [ ] Add failing mocked tests for Anthropic `/v1/messages`, required headers, separate system prompt, text-block collection, token usage, health check, prompt-directed JSON output, malformed content, status mapping, and zero provider fallback.
- [ ] Implement `AnthropicProvider` with its own request/response translation while reusing the shared request gate and safe error mapping. Advertise native JSON mode/schema as unavailable; parse only a direct JSON object for `generate_json` and leave bounded repair to the analysis layer.
- [ ] Provide a minimal typed custom-provider example and prove through the factory tests that a caller can register and use it without importing private Cove code or enabling a managed library.
- [ ] Keep custom loading explicit: no dynamic import path, arbitrary code execution, or plugin discovery is added in this phase.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: add anthropic and custom providers`.

### Task 4: Integrate, document, and verify the phase

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `src/cove_book_forge/doctor.py`
- Modify: `tests/test_cli_doctor.py`
- Create: `tests/test_provider_integration.py`

- [ ] Add fake-provider integration tests that construct each built-in through configuration and complete text/JSON generation without network access; verify no secret or prompt appears in serialized errors.
- [ ] Extend doctor with read-only provider-configuration readiness checks for known provider names, required key environment variables, and valid API bases. Doctor must not send network requests.
- [ ] Document DeepSeek/OpenAI-compatible, Anthropic, and custom-provider configuration, environment-secret handling, supported capabilities, and the explicit absence of cloud fallback. Do not claim chapter analysis, output generation, jobs, or MCP transport is implemented yet.
- [ ] Update the changelog while preserving the visible `book-to-skill` acknowledgement and all earlier phase history.
- [ ] Run the complete quality gate:

```bash
uv lock --check
uv run --no-sync pytest --cov=cove_book_forge --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src/cove_book_forge
uv build --clear
uv run --no-sync python scripts/verify_distribution.py dist
git diff --check
git status --short
```

- [ ] Commit: `docs: document model provider support`.

## Definition of done

- DeepSeek, OpenAI-compatible, and Anthropic adapters produce typed text/JSON results through one async public boundary.
- A user can inject a custom provider through the public registry without modifying core source code.
- Concurrency, request rate, timeout, status mapping, token accounting, and health checks are deterministic and covered without live requests.
- Missing credentials, network failures, rate limits, invalid output, and unknown providers yield closed safe errors with no secret, prompt, response-body, URL, or book-content leakage.
- No adapter silently changes provider, retries without a bound, or claims unsupported JSON-schema capability.
- Full tests, Ruff, strict mypy, lock verification, build, distribution hygiene, and documentation checks pass.
