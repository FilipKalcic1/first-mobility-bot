# Taxonomy Refactor Plan: 950 → 110 Tools

**Date:** 2026-04-19  
**Status:** Design complete, ready for implementation  
**Impact:** Reduces 950-way classification to 110-way, fundamentally improving routing accuracy

---

## Problem

The current system treats every (entity, operation) combination as a separate tool:
- `get_Vehicles`, `get_Persons`, `get_Expenses` → 3 tools doing the same thing (list entities)
- 75 entities × 13 average operations = 950 tools
- FAISS must distinguish between 950 tools with near-identical descriptions
- Even gpt-4o scores only 14.2% when entity words are absent

## Solution

Make `entity` a parameter instead of baking it into tool_id:
- `get_Vehicles` + `get_Persons` + `get_Expenses` → `list_entities(entity="Vehicles")`
- 950 tools collapse to **110 unique operation patterns**

## Numbers

| Metric | Before | After | Reduction |
|--------|--------|-------|-----------|
| Total tools | 950 | 110 | 88% |
| FAISS index size | 950 vectors | 110 vectors | 88% |
| Classification difficulty | 950-way | 110-way | 8.6x easier |

## Operation Pattern Breakdown

### High-frequency patterns (24 patterns, 864 tools — 91% of total)

These are shared across 37-55 entities each:

| Pattern | Entities | Description |
|---------|----------|-------------|
| `GET /` | 55 | List/filter entities |
| `GET /ProjectTo` | 51 | Project specific fields |
| `GET /Agg` | 50 | Aggregate (count, sum, avg) |
| `GET /GroupBy` | 50 | Group by field |
| `GET /{id}` | 48 | Get by ID |
| `POST /` | 46 | Create entity |
| `GET /{id}/metadata` | 46 | Get entity metadata |
| `DELETE /{id}` | 44 | Delete by ID |
| `DELETE /` | 43 | Delete (list) |
| `DELETE /DeleteByCriteria` | 43 | Bulk delete by criteria |
| `PUT /{id}` | 38 | Full update |
| `PATCH /{id}` | 37 | Partial update |
| `POST /multipatch` | 37 | Bulk partial update |
| `POST /{id}/documents` | 37 | Upload document |
| `GET /{id}/documents` | 37 | List documents |
| `GET /{id}/documents/{docId}` | 37 | Get document |
| `PUT /{id}/documents/{docId}` | 37 | Update document |
| `DELETE /{id}/documents/{docId}` | 37 | Delete document |
| `GET /{id}/documents/{docId}/thumb` | 37 | Document thumbnail |
| `PUT /{id}/documents/{docId}/SetAsDefault` | 37 | Set default doc |
| `GET /FileIds` | 2 | Get file IDs |
| `POST /linktenant/{id}` | 1 | Link tenant (Partners) |
| `POST /unlinktenant/{id}` | 1 | Unlink tenant (Partners) |
| `GET /tree/{companyId}` | 1 | Org tree |

### Special-case patterns (86 patterns, 86 tools — 9% of total)

Unique to specific entities (Lookup dropdowns, Stats, VehicleInputHelper, etc.). These stay as individual tools — no benefit from parameterizing single-entity operations.

## Implementation Plan

### Phase 1: Routing layer only (no execution changes)

The insight: we don't need to change tool_ids or the execution layer. We change **how routing works**:

1. **Two-step routing:**
   - Step 1: Detect operation intent (list, get-by-id, create, delete, aggregate, etc.) — 13 categories
   - Step 2: Detect entity (Vehicles, Persons, Expenses, etc.) — 75 categories
   - Combine: `operation + entity → tool_id`

2. **This is what TFI already does.** The Tool Family Index resolves `(entity, queryType) → tool_id` deterministically. The problem is it only fires when `detected_entity` is not None.

3. **Fix: Make entity detection more robust** (done — added 10 new curated entities) and **expand TFI coverage** to handle more query types.

### Phase 2: FAISS index restructuring (if Phase 1 insufficient)

1. Build 24 "operation template" embeddings instead of 950 per-tool embeddings
2. Each template has a rich description of the OPERATION (not the entity)
3. FAISS finds the operation pattern → entity detection fills in the entity → TFI resolves the exact tool

### Phase 3: Full refactor (if needed)

1. Restructure tool_documentation.json: 110 entries with `valid_entities` list
2. Rebuild embeddings (110 vectors instead of 950)
3. Update parameter_manager to inject entity as path param
4. This requires NO changes to tool_executor or api_gateway (already entity-agnostic)

## Risk Assessment

- **Phase 1:** Zero risk — only improves entity detection and TFI coverage
- **Phase 2:** Low risk — FAISS index is rebuilt at startup, old index is git-versioned
- **Phase 3:** Medium risk — changes tool_documentation structure, needs thorough testing

## Recommended Approach

**Start with Phase 1** — it's the highest ROI with zero risk:
- Entity detection improvements are already done (this session)
- Production accuracy log is already added (this session)
- Deploy, measure production accuracy for 1-2 weeks
- If entity detection + TFI handles >80% of queries correctly, Phase 2-3 may not be needed
- If <80%, proceed to Phase 2

## Swagger Services

All tools route through 3 swagger services:
- `vehiclemgt`: 597 tools (vehicles, equipment, trips, expenses, mileage)
- `tenantmgt`: 344 tools (persons, teams, roles, tenants, org units)
- `automation`: 9 tools (cases)
