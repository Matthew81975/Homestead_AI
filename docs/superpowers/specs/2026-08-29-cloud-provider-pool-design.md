# Cloud AI Provider Pool Design

Date: 2026-08-29

## Goal

Extend HCS Live mode from a single placeholder cloud configuration into a provider/model pool that can continue work across cloud services without silently degrading the caliber of model used during a task.

Offline local inference remains unchanged and separate.

## User-facing behavior

HCS keeps an explicit cloud capability tier for each active task, such as `high`, `medium`, or `light`.

For a task already using cloud AI:

1. HCS may automatically switch **providers** when needed.
2. HCS may automatically switch **models only when the replacement is in the same capability tier**.
3. If continuing requires a different capability tier, HCS pauses and asks the user for approval before continuing.
4. HCS shows the active tier, provider, and model in the Alexandria UI.
5. HCS records routing decisions and failures in the Log tab.

The system must never silently drop to a lower-caliber model merely to keep a task going.

## Provider pool

Each provider entry contains:

- provider id and display name
- API base URL
- credential reference
- enabled/disabled state
- supported models
- capability tier for each model
- priority/weight
- cooldown state after rate-limit/quota/capacity errors
- health state
- optional user-defined cost/usage preference metadata

Credentials remain local and must not be committed to Git. Configuration files committed to the repository contain only credential references or placeholders.

Initial architecture should support OpenAI-compatible REST providers first because several cloud services expose the same `/v1/chat/completions` style interface. Provider-specific adapters can be added later where APIs differ.

## Routing policy

Routing operates at task scope.

A task routing state contains:

- task/conversation identifier
- pinned capability tier
- current provider
- current model
- recent provider failures/cooldowns

For each cloud request:

1. Prefer the current provider/model while it is healthy and available.
2. If a provider fails because of quota, rate limit, transient capacity, or connectivity, try another healthy provider.
3. Prefer the same exact model when another provider offers it.
4. If not available, choose another model in the same capability tier.
5. Do not cross capability tiers automatically.
6. If no same-tier route is available, return a structured `approval_required` result containing the proposed replacement model/tier rather than silently failing over.
7. After user approval, continue the task with the approved replacement and update the pinned task tier/model state as appropriate.

Authentication/configuration errors are not treated as transient provider failures; they are surfaced clearly and that provider is disabled from routing until corrected.

## Usage spreading

The pool should spread usage only within the currently pinned capability tier.

Within that tier, selection uses health-aware weighted rotation:

- healthy providers participate
- cooldown providers are skipped until their retry time
- same-model routes are preferred over merely same-tier routes
- weights can distribute traffic across equivalent providers before limits are reached
- hard failures temporarily reduce or remove a provider from rotation

This provides proactive usage spreading while preserving task quality.

## Capability tiers

Capability tiers are HCS metadata, not claims that two models are identical.

Initial tiers:

- `high`: strong reasoning/coding/general models suitable for complex tasks
- `medium`: capable general-purpose models for normal work
- `light`: fast/low-cost models for simple tasks

The user may override model-to-tier assignments. HCS should store tier assignment explicitly rather than infer it dynamically on every request.

## Components

### cloud_router.py

Owns provider selection, task pinning, weighted rotation, failover, cooldowns, and approval-required decisions.

Public responsibilities:

- load configured providers/models
- choose a route for a task
- record success/failure
- expose current task routing state
- return structured approval-required results when a tier change is needed

### cloud_provider.py

Defines a provider adapter interface and an initial OpenAI-compatible adapter.

Responsibilities:

- construct authenticated request
- translate HCS messages/tools into provider payload
- normalize provider responses
- classify errors into rate-limit, quota, capacity, auth/config, timeout, or other failure categories

### task routing state

Persist minimal routing state so a multi-turn Alexandria conversation stays in the same capability tier. Do not persist secrets.

### server.py

`/chat` chooses the offline local path or cloud router based on effective AI mode.

`/ai/status` reports:

- Internet availability
- whether at least one cloud route is configured
- current cloud task tier/provider/model
- provider pool health summary

New endpoints should support listing/configuring provider metadata without exposing secrets.

### GUI

Live mode should display something like:

`Cloud: High | Provider: <name> | Model: <model>`

When the router returns `approval_required`, Alexandria presents the proposed model/tier change and asks the user whether to continue. No request is sent to the lower/different tier until approval is explicit.

A provider-management UI can be added incrementally; the first implementation may use local configuration plus clear status display if that keeps the initial change tractable.

## Error handling

Automatic failover is allowed for:

- HTTP 429 rate limits
- provider quota exhaustion when another same-tier route exists
- transient 5xx capacity/service errors
- request timeouts/connectivity failures

Do not silently fail over on:

- invalid API credentials
- malformed configuration
- policy/authentication failures that need user correction
- any route requiring a capability-tier change

All routing decisions should be logged with provider/model/tier and reason, excluding secrets.

## Security

- API keys never go in Git.
- Prefer environment variables or a local secrets/config file excluded from updates/version control.
- Logs must redact Authorization headers and credentials.
- Provider status endpoints must not return secrets.

## Testing

Tests should cover:

1. Live mode routes to cloud instead of local LLM.
2. Weighted rotation spreads requests among healthy same-tier routes.
3. Exact-model failover is preferred.
4. Same-tier different-model failover is automatic.
5. Cross-tier failover returns `approval_required`.
6. Approved cross-tier replacement resumes the task.
7. 429/5xx/timeouts trigger cooldown/failover.
8. auth/config errors do not rotate indefinitely.
9. offline mode remains unchanged.
10. provider/model/tier information appears in normalized chat responses and status.
11. secrets are not included in logs/status responses.

## Initial implementation boundary

The first implementation should establish the router, task-tier pinning, OpenAI-compatible provider adapter, configuration format, server routing, status reporting, and tests.

It should not attempt provider-specific billing APIs, automated price discovery, dynamic model benchmarking, or autonomous tier reclassification. Those can be layered on later.
