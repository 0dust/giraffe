# Product

## Register

product

## Users

Platform, SRE and application engineers testing an existing self-hosted LLM service
before or after a deployment change, from their laptop or an internal machine.

## Product Purpose

Expose Giraffe's implemented CLI suite through a local browser application. Connect
an endpoint, choose bounded traffic, run the built-in tests, inspect failures and
compare an explicitly selected baseline. The existing runner remains authoritative.
This context comes from the repository PRD, implemented contracts and user direction.

## Brand Personality

Technical, precise and direct. The user supplied Temporal's website as the visual
reference. The interface should make operational results easy to inspect.

## Anti-references

No marketing landing page, decorative metric dashboard, account administration,
governance, fleet provisioning or unrelated enterprise features. These reflect the
user's explicit core-MVP constraint.

## Design Principles

- Put the next useful action and the concrete finding before implementation detail.
- Show workload bounds before starting; request budget is not a completion estimate.
- Keep execution state, acceptance result and baseline comparison distinct.
- Reveal advanced settings and request evidence progressively.
- Use measured results only. Missing coverage remains visible.

## Accessibility & Inclusion

No special accommodations were specified. Implement labeled native controls,
keyboard access, visible focus, text alongside status colors and reduced motion.
