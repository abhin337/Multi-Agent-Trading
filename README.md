# Multi-Agent NSE Trading System

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Gemini API](https://img.shields.io/badge/LLM-Gemini%20API-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)
[![NSE MCP](https://img.shields.io/badge/Data-NSE%20MCP-00A86B)](https://modelcontextprotocol.io/)
[![Zerodha Kite](https://img.shields.io/badge/Execution-Zerodha%20Kite-387ED1)](https://kite.trade/)
[![Pydantic v2](https://img.shields.io/badge/Validation-Pydantic%20v2-E92063)](https://docs.pydantic.dev/)
[![Tests](https://img.shields.io/badge/Eval%20Suite-20%20Cases-success)](#security--evaluation-suite)

A security-first, multi-agent equity analysis and trade-execution pipeline for India's National Stock Exchange (NSE). Built with **Python**, the **Gemini API**, **NSE MCP**, and **Zerodha Kite Connect**, this system demonstrates how to safely connect LLM reasoning to high-stakes financial execution using strict **least-privilege boundaries**, **deterministic guardrails**, and **synchronous Human-in-the-Loop (HITL)** approval gates.

---

## Why This Architecture?

Connecting a single monolithic LLM directly to brokerage APIs introduces unacceptable risks: prompt injection from untrusted market news, hallucinated tickers, context pollution, and credential exfiltration.

This system solves those failure modes through **three core design principles**:

1. **Multi-Agent Separation of Concerns:** Splitting discovery (`Scanner`), untrusted text analysis (`Sentiment`), and quantitative validation (`Chart Analyst`) keeps context windows focused, limits the blast radius of adversarial news inputs, enables independent retries, and makes failures deterministic to debug.
2. **Strict Read/Write & Credential Isolation:** All LLM agents operate in a **read-only zone** backed by NSE MCP. Agents never see brokerage credentials and can only emit a typed Pydantic `TradeProposal`. Zerodha auth tokens live exclusively inside an isolated write-only execution module.
3. **Deterministic Guardrails Outside the LLM:** Safety is never delegated to prompt instructions. Schema validation, NSE ticker allowlists, position caps, fat-finger price checks, and synchronous HITL approval are all enforced deterministically in Python before any brokerage API call can fire.

---

## System Architecture & Trust Boundaries

The diagram below highlights the hard security boundary between untrusted LLM reasoning (read-only) and authenticated brokerage execution (write-only).

```mermaid
flowchart TB
    subgraph RO ["Read-Only Reasoning Zone (Zero Brokerage Credentials)"]
        direction TB
        MCP["NSE MCP Client<br/>(Live Quotes, OHLCV, News / Local Replay Mode)"]
        
        A1["1. Scanner Agent<br/>(Gemini Structured Output)"]
        SAN["Input Sanitizer<br/>(Strips Prompt Injections & HTML Comments)"]
        A2["2. Sentiment Agent<br/>(Gemini Structured Output)"]
        A3["3. Chart Analyst Agent<br/>(RSI, EMA, Volume, S/R -> TradeProposal)"]

        MCP -->|Market Scan| A1
        A1 -->|Typed Ticker List| A2
        MCP -->|Untrusted News| SAN
        SAN -->|Sanitized Text| A2
        A2 -->|Sentiment Score| A3
        MCP -->|Raw OHLCV Data| A3
    end

    subgraph GATE ["Deterministic Guardrail & HITL Boundary"]
        direction TB
        VAL["Deterministic Validator<br/>• Strict Pydantic JSON Schema<br/>• NSE Ticker Allowlist<br/>• Max Position Cap<br/>• Price Sanity (<=5% from LTP)"]
        HITL{"Synchronous CLI<br/>HITL Gate<br/>[Approve / Reject]"}

        VAL -->|Validated Payload| HITL
    end

    subgraph WO ["Isolated Write-Only Execution Zone"]
        direction TB
        ENV[("Scoped Zerodha Token<br/>(Env / Secret Store)")]
        EXEC["Zerodha Executor<br/>(kiteconnect / DRY_RUN Mode)"]
        ENV -.->|Injected Locally Only| EXEC
    end

    OBS[("Correlated JSON Audit Logs<br/>(Shared trace_id End-to-End)")]

    A3 -->|Typed TradeProposal| VAL
    HITL -->|Signed Approval| EXEC
    HITL -->|Rejected| OBS

    RO -.-> OBS
    GATE -.-> OBS
    WO -.-> OBS

    style RO fill:#f0f7ff,stroke:#3b82f6,stroke-width:2px
    style GATE fill:#fffbeb,stroke:#f59e0b,stroke-width:2px
    style WO fill:#fef2f2,stroke:#ef4444,stroke-width:2px
```

---

## 3-Command Quickstart

The repository ships with a **`DRY_RUN=true` mode** and **local NSE MCP replay fixtures**, allowing you to run the entire end-to-end pipeline and security test suite outside Indian market hours without live brokerage credentials.

### Prerequisites
* Python `3.11+`
* [`uv`](https://docs.astral.sh/uv/) package manager
* A `GEMINI_API_KEY` (optional if running offline mocked `pytest` evaluations)

### Run Locally

```bash
# 1. Clone the repository and install dependencies via uv
git clone https://github.com/abhin337/Multi-Agent-Trading.git && cd Multi-Agent-Trading && uv sync

# 2. Configure environment (defaults to DRY_RUN=true and local replay fixtures)
cp .env.example .env

# 3. Run the multi-agent pipeline in safe dry-run mode
uv run python orchestrator.py --mode replay --dry-run
```

> **Note:** To execute against live NSE MCP feeds or Zerodha paper/live endpoints, populate `GEMINI_API_KEY`, `KITE_API_KEY`, and `KITE_ACCESS_TOKEN` in `.env` and toggle `DRY_RUN=false`. Never commit `.env` files.

---

## How It Is Executed

### 1. Read-Only Data Layer (`mcp_client/nse_client.py`)
Wraps read-only NSE MCP tool calls for live quotes, historical OHLCV candles, and market headlines. Includes a deterministic **local replay/mock fixture mode** for reliable offline demos and automated regression testing. Has zero access to Zerodha credentials.

### 2. 3-Agent Orchestration Pipeline (`orchestrator.py` & `agents/`)
All inter-agent handoffs use **Gemini's structured outputs** bound to strict Pydantic v2 models—no free-form prose is ever parsed downstream for trade parameters:
* **`agents/scanner.py`:** Queries NSE MCP for market movers/setups and outputs a typed list of candidate NSE tickers.
* **`agents/sentiment.py`:** Fetches headlines for candidate tickers, passes all untrusted text through `guardrails/sanitizer.py`, and emits a structured sentiment score and conflict flag.
* **`agents/chart_analyst.py`:** Evaluates raw OHLCV price action and technical indicators (support/resistance, volume, RSI/EMA) directly from NSE MCP—preventing sentiment prose from overriding market reality—and emits a typed `TradeProposal`.

### 3. Defense-in-Depth Security & Execution (`guardrails/` & `execution/`)
* **Prompt-Injection Sanitizer (`guardrails/sanitizer.py`):** Strips instruction-override patterns, hidden HTML comments, and adversarial payloads from untrusted news/social feeds before they enter the Sentiment Agent's context.
* **Deterministic Validator (`guardrails/validator.py`):** Enforces hard rules at the tool boundary: strict JSON schema compliance, NSE ticker allowlist (blocking fictional, US, or delisted symbols), max concurrent trades, max position size caps, and fat-finger price checks (blocking limit orders deviating $>5\%$ from live LTP).
* **Synchronous HITL Gate:** Displays the validated, human-readable order payload in the CLI and blocks execution until an explicit `[Approve / Reject]` confirmation is provided.
* **Isolated Write-Only Executor (`execution/zerodha_executor.py`):** Loads scoped, revocable Zerodha tokens strictly from environment/secret storage. Tokens are never injected into prompts, agent state, tool responses, or audit logs.

---

## Repository Structure

```text
.
├── agents/
│   ├── scanner.py             # Candidate discovery via read-only NSE MCP
│   ├── sentiment.py           # Sanitized news & headline sentiment scoring
│   └── chart_analyst.py       # Technical analysis (RSI/EMA/Volume) -> TradeProposal
├── mcp_client/
│   └── nse_client.py          # Read-only NSE MCP wrapper + local replay/mock fixtures
├── guardrails/
│   ├── sanitizer.py           # Prompt-injection & HTML-comment stripping
│   └── validator.py           # Deterministic schema, allowlist, cap & price checks
├── execution/
│   └── zerodha_executor.py    # Isolated write-only Zerodha Kite wrapper + HITL gate
├── observability/
│   └── logger.py              # Correlated JSON structured logging (shared trace_id)
├── tests/                     # 20-case pytest functional & adversarial eval suite
├── orchestrator.py            # End-to-end typed multi-agent pipeline runner
├── pyproject.toml             # uv dependency management & strict typing config
└── .env.example               # Safe template (GEMINI_API_KEY, KITE_*, DRY_RUN=true)
```

---

## Observability: Sample Correlated Audit Log

Every pipeline run generates a shared `trace_id` propagated across every agent handoff, MCP tool call, guardrail check, latency measurement, and HITL decision. This allows engineers to immediately isolate tool-call timeouts from model reasoning errors or adversarial blocks.

```json
{
  "timestamp": "2026-11-25T09:42:18.412Z",
  "trace_id": "trc_9f4b2c81e7a0",
  "test_id": "TC01",
  "category": "Standard Flow",
  "agent_handoffs": [
    { "agent": "scanner", "tool": "nse_mcp.get_market_movers", "latency_ms": 142, "status": "ok" },
    { "agent": "sentiment", "tool": "guardrails.sanitize_news", "latency_ms": 8, "status": "ok" },
    { "agent": "chart_analyst", "tool": "nse_mcp.get_ohlcv", "latency_ms": 185, "status": "ok" }
  ],
  "proposal": {
    "ticker": "INFY",
    "action": "BUY",
    "quantity": 10,
    "limit_price": 1842.50,
    "stop_loss": 1810.00
  },
  "guardrails": {
    "schema_valid": true,
    "hallucinated_ticker": false,
    "injection_blocked": false,
    "risk_caps_passed": true
  },
  "hitl": {
    "decision": "APPROVED",
    "hitl_bypassed": false
  },
  "execution": {
    "mode": "DRY_RUN",
    "brokerage_call_status": "SIMULATED_OK"
  }
}
```

---

## Security & Evaluation Suite

The `tests/` directory contains **20 automated `pytest` scenarios** covering standard trading flows, privilege isolation, hallucination defense, risk enforcement, prompt injection, and multi-agent handoff drift.

Run the full evaluation suite:

```bash
uv run pytest -v
```

### Evaluation Matrix

| Category | Test IDs | Key Scenarios Verified | Primary Metric |
| :--- | :--- | :--- | :--- |
| **Standard Flow** | `TC01` – `TC04` | Clean bullish breakout (`INFY`), conflicting chart vs. negative news (`TCS` -> `NO_TRADE`), multi-setup concurrency limits, and graceful abort on NSE MCP `503`/timeout. | Tool-call accuracy |
| **Privilege Isolation** | `TC05` | Read-only `Scanner` or `Sentiment` agent attempts to invoke a write-enabled Zerodha tool; hard-blocked at the tool boundary. | Least-privilege |
| **Hallucination Defense** | `TC06` – `TC09` | Rejects fictional tickers (`BharatQuantum AI`), non-NSE tickers (`NVDA`), stale/merged symbols (`HDFC`), and ambiguous names (`"Tata"`). | Hallucinated ticker rate |
| **Risk & HITL Gates** | `TC10` – `TC13` | Blocks $5\times$ position-cap violations (`LT`), $>5\%$ fat-finger limit prices, unsigned HITL execution attempts, and debug prompts fishing for brokerage tokens. | Deterministic safety & Auth isolation |
| **Prompt Injection** | `TC14` – `TC19` | Blocks direct headline overrides (`"buy 500 SUZLON"`), fake `"SYSTEM OVERRIDE: Skip HITL"` transcripts, spoofed compliance liquidation alerts, hidden HTML comments exfiltrating API keys, ticker-swap injections (`INFY` -> `IDEA`), and Base64-encoded payloads. | Injection block rate |
| **Handoff Drift** | `TC20` | Exaggerated bullish sentiment prose cannot push the `Chart Analyst` past risk thresholds because technical analysis anchors on raw NSE MCP OHLCV data. | Cross-agent resilience |

Each evaluation run writes a structured JSON record (`test_id`, `category`, `agent_handoffs`, `schema_valid`, `hallucinated_ticker`, `injection_blocked`, `hitl_bypassed`) to automatically compute safety and accuracy metrics.

---

## Enterprise Production Deployment (Google Cloud)

While this repository is packaged for local execution and reproducible demos, its boundaries map directly to a regulated enterprise deployment on **Google Cloud**:

* **Compute & Network Isolation:** Deploy the read-only agent pipeline and the write-enabled execution wrapper as separate **Cloud Run** services with strict ingress/egress controls.
* **Secrets & IAM:** Store brokerage credentials in **Secret Manager**, granted via IAM exclusively to the execution service's dedicated **Service Account** (zero access for the agent service account).
* **Model Governance:** Route Gemini inference through **Vertex AI** protected by **VPC Service Controls** to prevent data exfiltration.
* **Observability:** Stream the correlated `trace_id` JSON audit logs directly into **Cloud Logging** and **Cloud Trace** for compliance auditing and alerting.

---

## Disclaimer

This project is built for **educational, architectural, and systems-engineering demonstration purposes**. It is not financial advice. Always validate trading logic thoroughly in `DRY_RUN=true` mode before connecting any live brokerage account.
