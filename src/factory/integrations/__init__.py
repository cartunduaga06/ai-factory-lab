"""Integrations with external systems.

Adapters that talk to GitHub (issues, pull requests) and to agent engines
(OpenHands, Codex) live here. Each integration translates an external API into
the factory's domain types so the orchestration layer stays provider-agnostic.
"""

from factory.integrations.base import AgentAdapterBase, IssueSource, PullRequestSink

__all__ = ["AgentAdapterBase", "IssueSource", "PullRequestSink"]
