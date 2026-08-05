# Real Estate Matcher — Architecture Document

**Scope:** this document covers the system as it behaves against the
`generated` data source (500+ synthetic listings) specifically.

Diagrams are [Mermaid](https://mermaid.js.org) — they render natively when
this file is viewed on GitHub, GitLab, and most modern markdown viewers, no
external service, extension, or repo-visibility requirement needed.
Standalone `.mmd` source files are also in `mermaid/` if you want to drop
one into the [Mermaid Live Editor](https://mermaid.live) directly.

---

## 1. Use-Case View

```mermaid
flowchart LR
    Buyer([Home Buyer])
    Claude([Claude<br/>Anthropic API])
    OpenAI([GPT<br/>OpenAI API])

    subgraph System["Real Estate Matcher — generated dataset, 500+ listings"]
        UC1(Browse with hard filters<br/>price, beds, baths, sqft,<br/>HOA, stories, style, schools)
        UC2(Search with AI matching<br/>freeform preferences)
        UC3(Cancel an in-progress<br/>AI search)
        UC4(Select AI provider)
        UC5(Verify a match<br/>inspect raw listing text)
        UC6(Score a listing against<br/>buyer's preferences)
    end

    Buyer --> UC1
    Buyer --> UC2
    Buyer --> UC3
    Buyer --> UC4
    Buyer --> UC5

    UC2 -. include .-> UC6
    UC3 -. extend .-> UC2
    UC4 -. extend .-> UC6

    UC6 --> Claude
    UC6 --> OpenAI
```

**UC1 (hard-filter browsing) and UC6 (AI scoring) are structurally
separate** — no shared code path. That's what makes it a guarantee, not
just an intention, that "Browse all" never incurs AI cost or latency.

---

## 2. Logical View

```mermaid
flowchart TD
    Frontend["app.jsx<br/>React SPA, static"]
    Main["main.py<br/>assembly"]
    ListingsRouter["listings.py<br/>router"]
    MatchRouter["match.py<br/>router"]
    ListingsService["listings_service.py"]
    MatchingService["matching_service.py"]
    SchoolsService["schools_service.py"]
    GeneratedData[("generated_listings.json<br/>500+ listings")]
    SchoolsData[("schools.json")]
    Anthropic{{"Anthropic API"}}
    OpenAI{{"OpenAI API"}}

    Frontend --> ListingsRouter
    Frontend --> MatchRouter
    Main -. includes .-> ListingsRouter
    Main -. includes .-> MatchRouter
    ListingsRouter --> ListingsService
    MatchRouter --> ListingsService
    MatchRouter --> MatchingService
    ListingsService --> SchoolsService
    ListingsService --> GeneratedData
    SchoolsService --> SchoolsData
    MatchingService --> Anthropic
    MatchingService --> OpenAI
```

**`listings.py` never imports `matching_service.py`** — a structural
guarantee that "Browse all" cannot reach an AI provider, not a runtime
check that could be bypassed. **`build_hard_filters()`** in
`listings_service.py` is shared by both routers, not duplicated.

---

## 3. Physical View

```mermaid
flowchart TD
    Browser["Browser<br/>HomeMatch SPA"]
    FrontendServer["Frontend static server<br/>python3 -m http.server :5500"]
    Backend["Backend server<br/>uvicorn app.main:app :8000"]
    GenFile[("generated_listings.json")]
    SchoolsFile[("schools.json")]
    EnvFile[[".env — secrets, gitignored"]]
    AnthropicCloud{{"Anthropic<br/>api.anthropic.com"}}
    OpenAICloud{{"OpenAI<br/>api.openai.com"}}

    Browser -->|"HTTP GET<br/>page load"| FrontendServer
    Browser -->|"HTTP fetch/XHR<br/>/listings /match/start<br/>/match/{id} /cancel"| Backend
    Backend --> GenFile
    Backend --> SchoolsFile
    Backend --> EnvFile
    Backend -->|"HTTPS + key"| AnthropicCloud
    Backend -->|"HTTPS + key"| OpenAICloud
```

**Single process** — concurrency comes from Python threads
(`ThreadPoolExecutor`, up to `MAX_CONCURRENT_BATCHES`), not multiple
backend processes. Job state lives in an **in-memory dict**, lost on
restart — the one piece that would need to move to something shared
(Redis, a database) before running behind more than one worker process.
API keys: gitignored, never sent to the browser, never logged — only
usage counts are printed to the server terminal.

---

## 4. Sequence Diagram — AI-Matching a Search

```mermaid
sequenceDiagram
    actor Buyer
    participant FE as Frontend
    participant Router as MatchRouter
    participant LS as listings_service
    participant MS as matching_service
    participant AI as Claude / OpenAI

    Buyer->>FE: Enter preferences, click "Find my matches"
    FE->>Router: POST /match/start
    activate Router
    Router->>LS: build_hard_filters(), fetch_listings(),<br/>normalize_listing(), filter_by_school_rating()
    LS-->>Router: candidate listings
    Router->>MS: start_match_job()
    MS->>MS: spawn background thread
    MS-->>Router: job_id, total_batches
    Router-->>FE: job_id, total_batches
    deactivate Router

    Note over MS,AI: Sliding-window concurrency —<br/>BATCH_SIZE=8, up to MAX_CONCURRENT_BATCHES in flight

    loop until all batches done or cancelled
        MS->>AI: score_batch(8 listings)
        AI-->>MS: requirements[] per listing
        MS->>MS: score = round(100 * met/total)
    end

    loop every 800ms while running
        FE->>Router: GET /match/{job_id}
        Router-->>FE: status, progress
    end

    opt Buyer clicks Cancel
        FE->>Router: POST /match/{job_id}/cancel
        Router->>MS: cancel_job()
        MS->>MS: stop submitting new batches
    end

    MS->>MS: status = done | cancelled
    FE->>Router: GET /match/{job_id} (final)
    Router-->>FE: matches[]
    FE->>FE: split into Full / Partial matches
    FE-->>Buyer: render result cards
```

**Score is computed by our own code**, not trusted directly from the
model's returned number — a deliberate fix after testing showed models
could state a correct-sounding reason while returning an inconsistent
score. **Cancellation is real:** it stops further batches from being
submitted; an already-in-flight API call finishes (can't be recalled).

---

## 5. Security View

```mermaid
flowchart LR
    subgraph Untrusted["Untrusted zone"]
        Browser["Browser / SPA"]
    end

    subgraph Trusted["Trusted zone — server process"]
        API["FastAPI app"]
        Validators["Pydantic validators"]
        Secrets[".env secrets"]
        Jobs["In-memory job registry"]
    end

    subgraph External["External trusted providers"]
        Anthropic{{"Anthropic API"}}
        OpenAI{{"OpenAI API"}}
    end

    Browser -->|HTTP| API
    API --> Validators
    API --> Secrets
    API --> Jobs
    API -->|"HTTPS + key"| Anthropic
    API -->|"HTTPS + key"| OpenAI

    style Untrusted fill:#2a0f0f,stroke:#CC0000,stroke-width:2px
    style Trusted fill:#0f2412,stroke:#007700,stroke-width:2px
    style External fill:#1e1e1e,stroke:#888888,stroke-width:2px
```

### Security findings, in plain writing

| # | Finding | Current state | Real risk if deployed publicly, unaddressed |
|---|---|---|---|
| 1 | **No authentication on any endpoint** | Confirmed — zero auth code anywhere in `app/routers/` | Anyone reaching the port can trigger real AI API costs, or poll/cancel any job by guessing its UUID |
| 2 | **CORS defaults to `*`** | `CORS_ALLOW_ORIGINS` defaults to wildcard in `config.py` | Any website's JS could call this API from a visitor's browser |
| 3 | **A secret in frontend code is not a real barrier** | N/A — general principle | Anyone can view it via browser DevTools and replay requests directly, bypassing the UI entirely |
| 4 | **Secrets handling** | `.env` gitignored, never returned in responses, never logged (only usage counts) | Verified correct |
| 5 | **Input validation** | Every field validated by Pydantic (type, enum membership) | Verified correct — invalid input gets a clean 422 |
| 6 | **Data sensitivity** | Entirely synthetic — fictional addresses, fictional school names/ratings | No real PII or real-world claims at risk |

**Bottom line:** hardened at input validation and secrets handling; **zero
access control** by design, appropriate for local/small-group sharing with
a hard spend cap set on the AI provider side, not appropriate for public
deployment without adding real authentication.
