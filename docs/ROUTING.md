# Routing control (design, not on the active roadmap)

**Status: backlog idea, not scheduled.** This was tracked as "Phase 4" and
had a design pass done, but has since been pulled off the active roadmap —
see CLAUDE.md's "Future ideas" section. No routing endpoint, driver verb,
or UI exists, and none is planned in the current build. This doc is kept
only so the design isn't lost if the feature gets picked back up later; if
it does, re-scope against the codebase at that time rather than assuming
the shapes below still fit — written against what's already in place
(`routing_audit` schema, `view_routing`/`propose_routing`/`execute_routing`
permissions, `flows`/`signals` tables) as of the time this was drafted.

**Non-goal for this doc:** cross-vendor routing (e.g. an Appear frame
feeding a Haivision encoder). That needs its own pass once single-vendor
routing has shipped and we know what a "route" actually looks like in
practice for at least two vendors. Everything below is single-hop,
single-vendor.

## Why this is not on the poller's code path

The poller's entire design is "never mutate a device." Routing control is
the first feature that must. Reusing the poller's async fan-out, driver
resolution, or credential handling for a mutating call would mean a single
code path silently gains the ability to push config once *any* future
change touches it. Routing control gets its own request lifecycle, its own
permission checks, and its own audit write — separate from `poller.py` end
to end, on purpose, permanently, not just until it's convenient to merge.

## What a "route" is

A route is a single **flow** row (`flows` table) — one destination of one
signal. Routing control changes which *source port* feeds a given flow's
*destination port* (or removes/creates a flow). It does not touch `signals`
directly; a signal's status is still poller-observed. `signals`/`flows`
stay read-only to the poller and to routing control alike in the sense that
neither writes status — only humans (via routing control) write *intent*,
and the poller keeps writing *observed state*.

Concretely, `flows.source_port_id` is the thing a routing change edits.
Everything else on the row (labels, dest_*) is inventory metadata set up
ahead of time through the existing inventory API.

## Driver contract extension

Three new **optional** methods on `Driver` (`drivers/base.py`), default
`NotImplementedError` so no existing driver needs to change:

```python
def route_capable(self) -> bool:
    """False by default. A driver opts in explicitly."""
    return False

def propose_route(self, dest_port: str, source_port: str) -> RouteProposal:
    """Read-only: validate the requested change is legal on this device
    (port exists, is assignable, compatible signal type) and return a
    RouteProposal describing exactly what will change. Must not mutate."""
    raise NotImplementedError

def execute_route(self, proposal: RouteProposal) -> RouteResult:
    """Mutating. Only called after a human has confirmed the exact
    RouteProposal previously returned by propose_route — see workflow
    below. Must be idempotent-safe to retry on ambiguous failure
    (timeout after send): a second execute_route with the same
    proposal should either no-op or re-confirm, never double-apply."""
    raise NotImplementedError

def verify_route(self, proposal: RouteProposal) -> bool:
    """Read-only: re-read the device and confirm the change actually
    took, independent of what execute_route claimed. This is what
    catches a device that accepted the command but didn't apply it."""
    raise NotImplementedError
```

`route_capable()` lets the API refuse to even show a "propose" affordance
for a device whose driver hasn't implemented routing — same spirit as
`driver_catalog()`'s `notes` being operator-facing.

`RouteProposal` / `RouteResult` are new dataclasses in `drivers/base.py`
alongside `CollectResult`/`TrapResult`, not ad hoc dicts — same pattern as
the rest of the driver contract.

## Workflow

```
1. PROPOSE   POST /api/routing/propose   {flow_id, new_source_port_id}
             permission: propose_routing
             -> resolves driver, calls driver.propose_route()
             -> writes routing_audit row: confirmed=0, old_route_json
                (current flows row), new_route_json (the RouteProposal),
                requested_by, requested_at
             -> returns {proposal_id, diff}   -- does NOT touch the device

2. DIFF      GET /api/routing/proposal/{id}
             permission: view_routing
             -> renders old vs new human-readable (port names, not ids)
             -> proposal has a short TTL (e.g. 10 min); stale proposals
                are rejected at confirm time and must be re-proposed,
                so a diff a human looked at 2 hours ago can't be executed
                against a device state that has since changed.

3. CONFIRM + -> a *separate* authenticated request, not a flag on the
   EXECUTE       propose call. This is the one place UI must force a
             distinct click/step — no double-submit, no confirm=true
             param on the propose request.
             POST /api/routing/proposal/{id}/confirm
             permission: execute_routing (deliberately a different,
             stronger permission than propose_routing — see roles below)
             -> re-validates the proposal hasn't expired or been
                superseded by a newer proposal on the same flow
             -> calls driver.execute_route(proposal)
             -> updates routing_audit: confirmed=1, executed_at, result

4. VERIFY    Immediately after execute, before returning success to the
             caller: calls driver.verify_route(proposal).
             -> result='success' only if verify_route confirms it
             -> result='failed' if execute_route raised, or verify_route
                came back false — either way the operator is told to
                check the device by hand, NOT auto-rolled-back (see
                "no automatic rollback" below)
             -> flows.source_port_id is only updated in NexNOC's own DB
                on result='success' -- a failed/unverified execute must
                not silently update our record of what's routed

5. AUDIT     Every state transition above writes to routing_audit as it
             happens (not batched at the end), so a crash mid-workflow
             still leaves a row showing exactly how far it got.
```

## No automatic rollback

If `execute_route` succeeds but `verify_route` fails (or the request
times out and we don't know which happened), routing control does **not**
attempt to auto-revert. Auto-rollback on an ambiguous broadcast-path
failure risks a second blind mutation compounding the first, on a live
signal, with no human looking. Instead: `result='failed'`, the audit row
records exactly what was attempted, and the flow is surfaced in the UI as
"routing state uncertain — verify by hand" until an operator explicitly
re-proposes (possibly a revert-shaped proposal) through the same gated
workflow. Slower to recover, much harder to make worse.

## Permission model

Already reserved in `auth.py`, currently unassigned to any default role
except `admin`:

| Permission | Grants |
|---|---|
| `view_routing` | See current routes and pending/past proposals. Read-only. |
| `propose_routing` | Steps 1–2: create and view a diff. Cannot execute. |
| `execute_routing` | Step 3: confirm and push. Implies nothing about propose — an admin could grant propose to an on-call operator without giving them execute, so a second, more trusted person confirms. |

This mirrors a two-person-rule shape without hard-coding one: whether
propose and execute are actually held by different people is a role-
assignment decision the admin makes per site, not something the code
enforces structurally. If real two-person-rule turns out to be required,
that's a follow-up (e.g. "a user cannot confirm their own proposal") —
flagging it here as explicitly deferred, not forgotten.

## What's still open (needs a decision before implementation starts)

1. **Proposal TTL length** — 10 minutes above is a placeholder, not a
   confirmed value.
2. **Concurrent proposals on the same flow** — second propose supersedes
   the first (auto-invalidate), or reject until the first is confirmed
   or expired? Leaning supersede, but not decided.
3. **Which vendor gets `route_capable()` first.** Haivision is the
   obvious candidate — the `/apidoc` comment in `drivers/haivision.py`
   already notes start/stop/edit calls exist on the device and were
   deliberately excluded from the poller "because they belong to Phase 4."
   Appear routing would need its own MMI/IpGateway confirmation pass,
   same caveat as Phase 3.
4. **UI surface** — new page vs. an action on the existing links table.
   Not scoped here; this doc is API/workflow/data-model only.

## Files this touches when implemented

| File | Change |
|---|---|
| `drivers/base.py` | `RouteProposal`/`RouteResult` dataclasses, 3 new optional `Driver` methods, all default-unimplemented |
| `drivers/haivision.py` (or whichever vendor goes first) | Real `route_capable`/`propose_route`/`execute_route`/`verify_route` |
| `db.py` | `routing_audit` CRUD (table already exists — see `schema.sql`) |
| new `routing_api.py` | Mirrors `inventory_api.py`'s shape: stdlib HTTP handlers, no framework |
| `server.py` | Route the four new endpoints, permission checks via `auth.py` |
| `web/` | New UI surface (page or table action — TBD, see open question 4) |
