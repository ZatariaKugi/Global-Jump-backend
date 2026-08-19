# AI Assessment Suggestions

Assessment-driven advisor matching for the seeker **dashboard** (“Ai Suggested”) and the advisor **leads** queue. Separate from Find Advisor profile recommendations.

| | |
|---|---|
| **UI (seeker)** | Home dashboard → Ai Suggested advisors |
| **UI (advisor)** | Leads / matched customers queue |
| **APIs** | Dashboard, assessment results, `/advisors/me/leads*` |
| **Source of truth** | Completed `assessments` (destination + visa + score/tier) |
| **Persistence** | `advisor_leads` only |
| **Does not write** | `seeker_advisor_recommendations` |
| **Does not overwrite** | `seeker_profiles.intended_destination` / `intended_visa_type` |

---

## Product surfaces

### 1. Seeker dashboard — Ai Suggested

`GET /api/v1/users/me/dashboard`

- Returns up to **8** live-ranked advisors (`matched_advisors`)
- Requires a **completed** assessment in scope; otherwise `matched_advisors = []`, `assessment_id = null`, eligibility score `0`
- Matching is computed **live** (not read from `advisor_leads` or the Find Advisor cache)
- Each item is `AdvisorMatchRead`: identity, price, rating, `match_score`, optional `match_reasons` / `rule_score` / `ai_score`

### 2. Assessment result preview

Completing or fetching an assessment embeds a matched-advisor preview.  
`GET /api/v1/assessments/{id}/matched-advisors` returns a paginated live shortlist (rejects incomplete assessments).

### 3. Advisor leads queue

On assessment completion, positive matches are persisted as `AdvisorLead` rows (up to 500). Advisors work the queue: new → viewed → contacted / dismissed.

---

## Assessment lifecycle

1. **`GET /assessments/questions`** — active questions for country + visa (supports adaptive `depends_on_option_id`)
2. **`POST /assessments`** — start (`in_progress`), optional A/B variant
3. **`POST /assessments/{id}/answers`** — save progress (`complete=false`) or finish (`complete=true`)
4. **Score** — every applicable question’s weight is in the denominator; unanswered contribute `0` (cannot inflate to 100%). Confidence = answered/applicable. Skipped questions become missing requirements + tips. Category averages and AI narrative still run for answered items.
5. **Tier** — from `AssessmentThreshold` (defaults: ≥80 highly, ≥60 likely, ≥40 borderline, else low)
6. **AI insights** — strengths / weaknesses / missing requirements / `ai_summary` via OpenAI (optional; failure does not block completion)
7. **Advisor matching** — hybrid match → write `advisor_leads`

Already-completed assessments reject another submission (`assessment_completed`).

---

## Data sources

### From the assessment (authoritative)

| Field | Role |
|---|---|
| `destination_country` | Hard country gate |
| `visa_type` | Visa scoring |
| `score` / `tier` | Shown to seeker; passed to AI re-ranker as context (not rule weights) |
| `assessment_id` | Lead FK |

Answers, category scores, and narrative insights are **not** direct inputs to the deterministic advisor score.

### Soft fields from seeker profile (only)

| Field | Role |
|---|---|
| `preferred_language` | Rule + AI |
| `annual_income_band` | Rule price fit + AI |
| `timezone` / `nationality` / `country_of_residence` | AI prompt only |

Profile **intent** columns are never updated by assessment submit.

---

## Hard gates & hybrid scoring

Same matcher as Find Advisor (`advisor_matching_service` + `ai_advisor_match_service`):

1. Approved + active + integrations-ready
2. **Country expertise required** (visa-only excluded)
3. Rule weights (country / language / availability / visa) + experience & price soft bonuses
4. OpenAI re-ranks top **25**; blend **65% rule + 35% AI**
5. OpenAI failure → rule-only ranking

Dashboard: try `positive_only=True` first; if empty, retry with `positive_only=False` (country gate still applies).

---

## Persistence: `advisor_leads`

| Column | Notes |
|---|---|
| `seeker_id` / `advisor_id` / `assessment_id` | Unique together |
| `match_score` | Hybrid (or rule) score |
| `match_reasons` | AI or synthesized (≤1000 chars) |
| `status` | `new` \| `viewed` \| `contacted` \| `dismissed` |

### Status transitions

| Action | Effect |
|---|---|
| `GET .../leads/{id}` | `new` → `viewed` |
| `POST .../leads/{id}/contact` | → `contacted` (status only; no message sent) |
| `POST .../leads/{id}/dismiss` | → `dismissed` |

### Explicitly not written on assessment complete

- `seeker_advisor_recommendations` (Find Advisor cache)
- `seeker_profiles.intended_destination` / `intended_visa_type`

---

## Dashboard gating

The dashboard endpoint always returns; Ai Suggested is empty without a completed assessment.

Scope resolution for display / journey:

1. Query `country` / `visa_type` overrides
2. Else latest completed assessment
3. Else profile intent (display only — **does not** unlock matched advisors)

Optional `days=7|30|90` windows eligibility + matched advisors + document stats to assessments/uploads in that window.

---

## APIs

### Seeker — dashboard

```http
GET /api/v1/users/me/dashboard?visa_type=&country=&days=
```

### Seeker — assessment

| Method | Path | Notes |
|---|---|---|
| `GET` | `/assessments/questions` | Questionnaire |
| `POST` | `/assessments` | Start |
| `POST` | `/assessments/{id}/answers` | Save / complete (+ match preview when complete) |
| `GET` | `/assessments/{id}` | Result + live matches when completed |
| `GET` | `/assessments/{id}/matched-advisors` | Paginated matches |
| `GET` | `/assessments` | History |

All require seeker role + ownership.

### Advisor — leads

| Method | Path | Notes |
|---|---|---|
| `GET` | `/advisors/me/leads` | Filters: `status`, `q`, `visa_type` |
| `GET` | `/advisors/me/leads/{id}` | Detail; marks viewed |
| `POST` | `/advisors/me/leads/{id}/contact` | Mark contacted |
| `POST` | `/advisors/me/leads/{id}/dismiss` | Dismiss |

Require advisor role. Ordered by `match_score` desc, then `created_at` desc.

---

## Separation from Find Advisor

| | Find Advisor (profile) | AI Assessment |
|---|---|---|
| Intent | Profile destination + visa | Assessment destination + visa |
| UI | Ai Recommended list | Dashboard Ai Suggested + advisor leads |
| Storage | `seeker_advisor_recommendations` | `advisor_leads` (+ live match for dashboard) |
| Refresh | Onboarding / profile intent / cache miss | On assessment complete (leads); live on dashboard |
| Can override the other? | No | No — must not touch profile intent |

Taking an assessment for a different country/visa **must not** change Find Advisor rankings.

---

## Key files

| File | Role |
|---|---|
| `app/services/assessment_service.py` | Submit, score, insights, lead generation hook |
| `app/services/advisor_lead_service.py` | Persist / list / status transitions |
| `app/services/seeker_dashboard_service.py` | Dashboard matched advisors |
| `app/services/advisor_matching_service.py` | `match()` / gates / rules |
| `app/services/ai_advisor_match_service.py` | OpenAI re-rank + blend |
| `app/services/ai_insight_service.py` | Narrative assessment insights |
| `app/models/advisor_lead.py` | ORM |
| `app/api/v1/assessments.py` | Assessment routes |
| `app/api/v1/advisors.py` | Leads routes |
| `app/api/v1/seeker_profiles.py` | Dashboard route |
| `app/schemas/assessment.py` | `AdvisorMatchRead`, assessment reads |
| `app/schemas/advisor_lead.py` | Lead reads |
| `app/schemas/seeker_dashboard.py` | Dashboard schema |

---

## Logging

- Logger: `ai_recommendations`
- Destination: normal application logging (`stdout`)
- Format: follows `LOG_JSON` (structured JSON in production, console output locally)

Look for: `assessment_*`, `assessment_recommendation_leads_finished`, `dashboard_*`, matching steps `10`–`18`, OpenAI blend events. Correlate with `trace_id`.

Lead view/contact/dismiss actions do not currently emit dedicated recommendation-log lines.
