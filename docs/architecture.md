# Real Estate Matcher — Architecture Document

**Scope:** this document covers the system as it behaves against the
`generated` data source (500+ synthetic listings) specifically.

Diagrams are [PlantUML](https://plantuml.com) source, embedded below and
as standalone `.puml` files in `diagrams/`. Render at
[plantuml.com/plantuml](https://www.plantuml.com/plantuml/uml/), via the
PlantUML VS Code extension, or any local PlantUML toolchain.

---

## 1. Use-Case View

```plantuml
@startuml UseCaseView
left to right direction
skinparam packageStyle rectangle
skinparam actorStyle awesome
skinparam linetype ortho
skinparam nodesep 40
skinparam ranksep 60

actor "Home Buyer" as Buyer

rectangle "Real Estate Matcher\n(scope: generated dataset, 500+ listings)" {
  usecase "Browse with hard filters\n(price, beds, baths, sqft,\nHOA, stories, style, schools)" as UC1
  usecase "Search with AI matching\n(freeform preferences)" as UC2
  usecase "Cancel an in-progress\nAI search" as UC3
  usecase "Select AI provider" as UC4
  usecase "Verify a match\n(inspect raw listing text)" as UC5
  usecase "Score a listing against\nbuyer's preferences" as UC6
}

actor "Claude\n(Anthropic API)" as Claude
actor "GPT\n(OpenAI API)" as OpenAI

Buyer -- UC1
Buyer -- UC2
Buyer -- UC3
Buyer -- UC4
Buyer -- UC5

UC2 ..> UC6 : <<include>>
UC3 .up.> UC2 : <<extend>>
UC4 .up.> UC6 : <<extend>>

UC6 -- Claude
UC6 -- OpenAI

note bottom of UC1
  Never calls an AI provider —
  its router never imports
  the matching service.
end note

note bottom of UC6
  Batched (8/call), scored
  concurrently, score computed
  from a requirements-met
  breakdown, not trusted
  directly from the model.
end note

@enduml
```

---

## 2. Logical View

Strict top-to-bottom layering — frontend, assembly, routers, services,
data, external providers — so every arrow flows one direction, no
backward or diagonal crossings.

```plantuml
@startuml LogicalView
top to bottom direction
skinparam componentStyle rectangle
skinparam linetype ortho
skinparam nodesep 30
skinparam ranksep 50

package "Layer 1 — Frontend" {
  [app.jsx\n(React SPA, static)] as Frontend
}

package "Layer 2 — Assembly" {
  [main.py] as Main
}

package "Layer 3 — Routers  (HTTP only)" {
  [listings.py] as ListingsRouter
  [match.py] as MatchRouter
}

package "Layer 4 — Services  (business logic)" {
  [listings_service.py] as ListingsService
  [matching_service.py] as MatchingService
  [schools_service.py] as SchoolsService
}

package "Layer 5 — Data" {
  database "generated_listings.json\n(500+ listings)" as GeneratedData
  database "schools.json" as SchoolsData
}

package "Layer 6 — External AI Providers" {
  cloud "Anthropic API" as Anthropic
  cloud "OpenAI API" as OpenAI
}

Frontend --> ListingsRouter
Frontend --> MatchRouter

Main --> ListingsRouter : includes
Main --> MatchRouter : includes

ListingsRouter --> ListingsService
MatchRouter --> ListingsService
MatchRouter --> MatchingService

ListingsService --> SchoolsService
ListingsService --> GeneratedData
SchoolsService --> SchoolsData

MatchingService --> Anthropic
MatchingService --> OpenAI

note right of MatchingService
  listings.py never imports
  this module — a structural
  guarantee that "Browse all"
  cannot reach an AI provider.
end note

note right of ListingsService
  build_hard_filters() is shared
  by both routers, defined once
  here — not duplicated.
end note

@enduml
```

---

## 3. Physical View

Same layering discipline — browser, dev machine services, filesystem,
external clouds, each in their own tier.

```plantuml
@startuml PhysicalView
top to bottom direction
skinparam nodeStyle rectangle
skinparam linetype ortho
skinparam nodesep 30
skinparam ranksep 50

node "Browser" as Browser {
  artifact "HomeMatch SPA" as SPA
}

package "Developer's Machine" {
  node "Frontend static server\npython3 -m http.server  :5500" as FrontendServer

  node "Backend server\nuvicorn app.main:app  :8000" as Backend {
    artifact "app/ code" as AppCode
    artifact "In-memory job registry" as JobRegistry
  }

  folder "Local filesystem" as FS {
    file "generated_listings.json" as GenFile
    file "schools.json" as SchoolsFile
    file ".env  (secrets, gitignored)" as EnvFile
  }
}

package "External Services" {
  cloud "Anthropic\napi.anthropic.com" as AnthropicCloud
  cloud "OpenAI\napi.openai.com" as OpenAICloud
}

Browser --> FrontendServer : HTTP GET\n(page load)
Browser --> Backend : HTTP (fetch/XHR)\n/listings, /match/start,\n/match/{id}  (polled), /cancel

Backend --> FS : reads at startup /\nper request
Backend --> AnthropicCloud : HTTPS + key
Backend --> OpenAICloud : HTTPS + key

note right of Backend
  Single process. Concurrency
  comes from Python threads,
  not multiple processes.
end note

note right of EnvFile
  API keys: gitignored, never
  sent to the browser, never
  logged (only usage counts).
end note

@enduml
```

---

## 4. Sequence Diagram — AI-Matching a Search

Participants ordered strictly left-to-right in call order (Buyer → Frontend
→ Router → listings_service → matching_service → AI), so the flow reads
top-to-bottom with no backward jumps between lifelines.

```plantuml
@startuml SequenceAIMatch
participant Buyer
participant "Frontend" as FE
participant "MatchRouter" as Router
participant "listings_service" as LS
participant "matching_service" as MS
participant "Claude / OpenAI" as AI

Buyer -> FE : Enter preferences,\nclick "Find my matches"
FE -> Router : POST /match/start
activate Router

Router -> LS : build_hard_filters()\nfetch_listings()\nnormalize_listing()\nfilter_by_school_rating()
LS --> Router : candidate listings

Router -> MS : start_match_job()
MS -> MS : spawn background thread
MS --> Router : job_id, total_batches
Router --> FE : job_id, total_batches
deactivate Router

note over MS, AI
  Background: sliding-window concurrency,
  BATCH_SIZE=8, up to MAX_CONCURRENT_BATCHES
  batches in flight at once.
end note

loop until all batches done or cancelled
  MS -> AI : score_batch(8 listings)
  AI --> MS : requirements[] per listing
  MS -> MS : score = round(100 * met/total)
end

loop every 800ms while running
  FE -> Router : GET /match/{job_id}
  Router --> FE : status, progress
end

opt Buyer clicks Cancel
  FE -> Router : POST /match/{job_id}/cancel
  Router -> MS : cancel_job()
  MS -> MS : stop submitting new batches
end

MS -> MS : status = done | cancelled
FE -> Router : GET /match/{job_id}  (final)
Router --> FE : matches[]
FE -> FE : split into Full / Partial matches
FE --> Buyer : render result cards

@enduml
```

**Score is computed by our own code**, not trusted directly from the
model's returned number — a deliberate fix after testing showed models
could state a correct-sounding reason while returning an inconsistent
score. **Cancellation is real:** it stops further batches from being
submitted; an already-in-flight API call finishes (can't be recalled).

---

## 5. Security View

Left-to-right zone flow: untrusted browser → trusted server process →
external trusted providers.

```plantuml
@startuml SecurityView
left to right direction
skinparam linetype ortho
skinparam nodesep 30
skinparam ranksep 50
skinparam rectangle {
  BorderColor<<untrusted>> #CC0000
  BorderColor<<trusted>> #007700
  BorderColor<<external>> #555555
}

rectangle "Untrusted zone" <<untrusted>> {
  [Browser / SPA] as Browser
}

rectangle "Trusted zone (server process)" <<trusted>> {
  [FastAPI app] as API
  [Pydantic validators] as Validators
  [.env secrets] as Secrets
  [In-memory job registry] as Jobs
}

rectangle "External trusted providers" <<external>> {
  [Anthropic API] as Anthropic
  [OpenAI API] as OpenAI
}

Browser --> API : HTTP\n(CORS_ALLOW_ORIGINS)
API --> Validators
API --> Secrets
API --> Jobs
API --> Anthropic : HTTPS + key
API --> OpenAI : HTTPS + key

note bottom of Browser
  No authentication on any
  endpoint. Fine for local
  single-user dev; a real gap
  before public deployment.
end note

note bottom of Secrets
  Never committed, never
  returned in a response,
  never logged.
end note

note bottom of Jobs
  In-process dict — lost on
  restart, no ownership check
  on job_id.
end note

@enduml
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
