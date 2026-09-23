# AI Factory Lab — Repository Policy

AI Factory Lab is the project-agnostic engineering factory used to deliver work into product repositories.

## Factory boundary

This repository owns reusable Factory capabilities: agent definitions, Factory policies, reusable skills, patterns, templates, and Factory documentation.

Product repositories own their own application code, domain rules, architecture, tests, infrastructure, secrets, production data, and repository-specific `AGENTS.md` instructions.

Do not place Finanza IA, Dulces El Jericoano, or any other product-specific business rule in the Factory core.

## Delivery safety

- Never push directly to a target repository's default branch.
- Never merge or deploy without explicit Product Owner approval.
- Never modify production data, production workflows, secrets, credentials, or destructive infrastructure without explicit Product Owner approval.
- The Factory stops at a Draft PR in `READY_FOR_PO`.
- Git delivery for a Factory execution belongs only to the orchestrator.
- Specialist agents must not create/switch branches, commit, push, open/merge PRs, or deploy.

## Policy layering

When delivering into a product repository:
1. Factory policy defines global delivery and safety behavior.
2. The target repository's `AGENTS.md` files define project-specific engineering constraints.
3. The Issue/task defines the requested product change.

If these layers materially conflict, stop and report the conflict instead of guessing.

## Change discipline

Keep Factory changes incremental and independently reviewable. Preserve the current v0.2 behavior while capabilities are extracted from product repositories. New capabilities must not silently change active product executions.
