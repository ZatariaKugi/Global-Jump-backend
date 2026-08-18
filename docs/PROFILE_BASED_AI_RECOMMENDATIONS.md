# Profile-Based AI Recommendations (Find Advisor)

Seeker-facing **Ai Recommended** list on Find Advisor. Driven only by onboarding / profile intent — never by AI Assessment.

| | |
|---|---|
| **UI** | Find Advisor → **Ai Recommended** |
| **API** | `GET /api/v1/advisors?recommended=true` |
| **Source of truth** | `seeker_profiles.intended_destination` + `intended_visa_type` |
| **Persistence** | `seeker_advisor_recommendations` |
| **Not used** | Completed assessments, assessment score/tier, `advisor_leads` |

---

## Product behavior

1. Seeker completes onboarding (or sets destination + visa on profile).
2. Backend runs hybrid matching (rules + OpenAI) and **saves** ranked advisors.
3. Find Advisor with `recommended=true` **reads the saved rows** (compute + save only on cache miss).
4. Cards show `match_percentage` from the persisted hybrid score.

If destination or visa is missing, the endpoint falls back to normal directory listing (featured-first when `recommended=true` affects sort).

> Onboarding also returns free-text `ai_suggestions` from `ai_insight_service.generate_onboarding_suggestions`. Those are **not** advisor recommendations and are **not** stored in this table.

---

## Data sources

### Seeker profile (required)

| Field | Role |
|---|---|
| `intended_destination` | Hard country gate + context |
| `intended_visa_type` | Visa scoring + context |

### Seeker profile (soft)

| Field | Role |
|---|---|
| `preferred_language` | Rule language score + AI prompt |
| `annual_income_band` | Rule price-fit score + AI prompt |
| `timezone` | AI prompt only |
| `nationality` | AI prompt only |
| `country_of_residence` | AI prompt only |

Match case always uses:

- `context_source = "profile"`
- `assessment_id = null`
- No eligibility score / tier

### Advisor side

Approved + active advisors that are Stripe/Zoom integration-ready, with:

- `country_expertise` (required)
- `visa_specializations`
- `languages`
- weekly availability slots
- `years_of_experience`
- starting price
- average rating

---

## Hard gates

1. Role `advisor`, `is_active`, `verification_status = approved`
2. Integrations ready (Stripe Connect; Zoom when OAuth enabled)
3. **Destination country expertise required** — visa-only advisors are excluded

Ranking preference among eligible advisors:

1. Country + visa (best)
2. Country only (still eligible, lower score)

---

## Scoring (hybrid)

### Rule engine (default weights; admin-configurable)

| Factor | Default weight |
|---|---|
| Country | 40 |
| Language | 20 |
| Availability | 20 |
| Visa (“setting”) | 20 |

Soft bonuses (capped): experience ≤ 8, price fit vs income ≤ 6. Final rule score capped at 100.

Admin APIs: `GET/PUT /api/v1/admin/matching-weights`  
Changing weights does **not** auto-invalidate the cache.

### OpenAI re-rank

- Top **25** rule-ranked candidates (`AI_CANDIDATE_POOL`)
- Model: `OPENAI_MODEL` (default `gpt-5.4-mini`)
- Timeout: `OPENAI_TIMEOUT_SECONDS` (default 20s), one retry
- Returns `ai_score` (0–100) + short reason; cannot invent advisors

### Blend

```text
final_score = 0.65 × rule_score + 0.35 × ai_score
```

If OpenAI is missing/fails → keep pure rule ranking (no 500).

---

## Persistence

### Table: `seeker_advisor_recommendations`

| Column | Notes |
|---|---|
| `seeker_id` / `advisor_id` | Unique pair |
| `assessment_id` | Always `null` for this feature |
| `destination_country` / `visa_type` | Snapshot of profile intent |
| `context_source` | Always `"profile"` |
| `match_score` | Final hybrid (or rule-only) score |
| `rule_score` / `ai_score` | Optional debug fields |
| `match_reasons` | AI reason or synthesized text |
| `rank` | 1 = best |

Replace-all on refresh: delete seeker’s rows, then insert the new set (up to 500).

No TTL — cache is valid when rows exist for the current destination + visa with `context_source = profile`.

---

## When recommendations regenerate

| Trigger | Behavior |
|---|---|
| `POST /api/v1/users/me/onboarding` | Always `refresh_for_seeker` |
| `PATCH /api/v1/users/me/profile` | Refresh **only if** destination or visa changed |
| `GET /advisors?recommended=true` | Cache hit → read DB; miss → compute + save |

**Does not** auto-refresh on: language/income/timezone edits alone, advisor profile changes, rating changes, or admin weight changes.

---

## APIs

### Primary

```http
GET /api/v1/advisors?recommended=true&page=1&page_size=10
```

Optional filters still apply (`q`, `country`, `visa_type`, `language`, `min_price`, `max_price`, `min_rating`, `sort`). Saved rank order is preserved after filter intersection.

Response card field of interest: `match_percentage` (integer from persisted `match_score`).

Not exposed on listing cards: `match_reasons`, `rule_score`, `ai_score`.

### Related

| Method | Path | Notes |
|---|---|---|
| `POST` | `/users/me/onboarding` | Saves profile + refreshes recommendations |
| `PATCH` | `/users/me/profile` | Refreshes when intent changes |
| `GET` | `/users/me/profile` | Intent fields used as context |
| `GET` | `/advisors`, `/advisors/featured`, `/advisors/{id}`, `/advisors/slug/{slug}` | Rule-only `match_percentage` from profile (no cache write) |

---

## Explicitly out of scope

- Latest completed assessment destination/visa
- Assessment eligibility score / tier
- Dashboard “Ai Suggested” block
- `advisor_leads` (advisor CRM queue)

Helpers used: `match_context_from_profile`, `build_profile_match_case`.  
`replace_from_matches` **rejects** non-`profile` context.

---

## Key files

| File | Role |
|---|---|
| `app/api/v1/advisors.py` | `list_advisors` recommended path |
| `app/api/v1/seeker_profiles.py` | Onboarding + profile refresh triggers |
| `app/services/seeker_recommendation_service.py` | Cache ensure / refresh / persist |
| `app/services/advisor_matching_service.py` | Profile case, gates, rule score |
| `app/services/ai_advisor_match_service.py` | OpenAI re-rank + 65/35 blend |
| `app/models/seeker_advisor_recommendation.py` | ORM |
| `migrations/versions/f1a2b3c4d5e6_add_seeker_advisor_recommendations.py` | Schema |
| `app/services/matching_weights_service.py` | Admin weights |
| `app/core/logging.py` | Recommendation log setup |

---

## Logging

- Logger: `ai_recommendations`
- Destination: normal application logging (`stdout`)
- Format: follows `LOG_JSON` (structured JSON in production, console output locally)

Look for: `find_advisor_*`, `seeker_recommendations_cache_hit|miss`, `ai_blend_*`, `seeker_recommendations_saved`. Correlate with `trace_id` (= request id).
