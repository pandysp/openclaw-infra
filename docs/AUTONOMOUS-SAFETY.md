# Autonomous Agent Safety

Design document for running non-main OpenClaw sessions without the lethal trifecta.

**Status: Design only. Not implemented.**

## The Problem

Simon Willison's "lethal trifecta" identifies three capabilities that, when combined in a single agent, allow prompt injection to steal data:

1. **Private data** — access to your files, notes, credentials
2. **Untrusted content** — exposure to text an attacker could control
3. **External communication** — ability to send data out (email, HTTP, git push)

An attacker embeds instructions in untrusted content. The agent follows them, reads private data, and exfiltrates it through the external channel. Each leg is individually useful — the combination is dangerous.

**Source**: [The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) by Simon Willison.

## Current State

Main sessions (web chat) have all three legs but are human-controlled — you can pull the plug. Non-main sessions (cron, Telegram) run in Docker with bridge networking — isolated from the host but with outbound internet.

| Capability | Non-main session | Vector |
|---|---|---|
| Private data | Yes | Workspace r/w |
| Untrusted content | Yes | Web research during night shift (GitHub issues, Stack Overflow, Reddit) |
| External comm | Yes | Git push (PRs), HTTP requests |
| Host access | **No** | Sandbox blocks `~/.openclaw/`, sudo, gateway config |

The sandbox prevents privilege escalation and credential theft, but the trifecta still exists within the workspace boundary. The night shift reads private code, browses the web for solutions, and pushes PRs. Telegram delivery is gateway-controlled and not an exfiltration vector.

The eventual fix is splitting the night shift into Research (web access, no private data) and Dev (code access, no web) agents. See [SECURITY.md, Threat 4](./SECURITY.md#4-agent-host-command-abuse) for current mitigations.

## Design Principle

Don't try to make LLMs resist prompt injection — that's unsolved. Instead, structure agents so that a prompt-injected agent **can't complete the attack chain** because it's structurally missing a capability.

The human analogy: banks don't make tellers immune to social engineering. They ensure the teller who talks to customers can't also authorize wire transfers. Separation of duties, need-to-know, procedural controls over individual judgment.

## The Organizational Model

Each autonomous agent is an employee with a defined role. No role has all three legs of the trifecta.

```
                         ┌─────────────┐
                         │     You      │
                         │ (main session)│
                         └──────┬──────┘
                                │ reviews, approves,
                                │ promotes content
       ┌──────────┬─────────────┼──────────┬──────────┐
       ▼          ▼             ▼          ▼          ▼
  ┌─────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
  │Mailroom │ │Research│ │Dev     │ │Finance │ │Exec    │
  │ U:✓ P:✗ │ │ U:✓ P:✗│ │ P:✓ U:✗│ │ P:✓ U:low│ │ P:✓ U:✗│
  │ E:✗     │ │ E:✗    │ │ E:fixed│ │ E:✗    │ │ E:self │
  └─────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

### Mailroom

Reads incoming email. Sorts, tags, summarizes. Produces structured routing slips.

- **Reads**: email inbox
- **Writes**: structured summaries to `queue/mail/`
- **No access to**: vault, code, finances, CRM
- **Cannot**: send email, push code, post anything

If socially engineered by a malicious email: nothing to steal, nowhere to send it.

### Research Department

Browses the web, reads GitHub issues, Stack Overflow, Reddit. Produces structured research reports.

- **Reads**: the open internet
- **Writes**: structured findings to `queue/research/`
- **No access to**: vault, code repos, finances, CRM, email
- **Cannot**: send anything externally

If a malicious web page injects instructions: no private data to exfiltrate.

### Development Department

Reads and writes code repos. Can git push to pre-configured remotes only. **No internet access.** Gets research context only through structured reports from Research.

- **Reads**: code repos, structured reports from `queue/research/`
- **Writes**: code, git push to own repos only
- **No access to**: vault, email, finances, CRM
- **Cannot**: browse the web, send email

This splits the developer workflow: Research figures out the approach, Dev writes the code.

### Finance Department

Reads only an isolated financial data store — separate from the main vault. Cannot send anything externally. Prepares documents for human review.

- **Reads**: `finance/` data store only
- **Writes**: `finance/` and `queue/finance-reports/`
- **No access to**: vault, code, email, CRM
- **Cannot**: send email, push code, file anything externally

### Executive Assistant

Has vault access. Reads notes, organizes todos, manages priorities. Can send Telegram messages to your hardcoded ID only. **Never reads raw external content** — only structured summaries from Mailroom and Research.

- **Reads**: vault, structured summaries from `queue/mail/` and `queue/research/`
- **Writes**: vault, `outbox/` (Telegram to you)
- **No access to**: raw email, the internet, code repos, finances
- **Cannot**: send email, push code, browse the web

Most privileged role, kept furthest from untrusted content.

### CRM — the hard case

CRM inherently requires all three legs: reading client messages (untrusted), knowing your relationship history (private), sending follow-ups (external to arbitrary addresses). This can't be structurally broken.

**Mitigations**:
- CRM agent drafts replies, human sends (remove the E leg)
- CRM data store is isolated from vault (compartmentalized P — a breach leaks client pipeline, not finances or personal notes)

## Workspace Zoning

The workspace has a taint gradient, not a binary trusted/untrusted split:

```
Most trusted                                    Least trusted
────────────────────────────────────────────────────────────
  core/          memories/       notes/          inbox/
  Human-written  Agent-written   Session notes   Raw web,
  skills,        summaries of    may contain     email,
  system prompts past work       derived web     imports
                                 content
```

Enforcement: Docker bind mounts. Each agent's container only mounts the directories its role requires. No filesystem escape.

### The laundering problem

Content migrates across trust boundaries over time. A human approves saving a web excerpt as a note → the note enters the trusted zone → a future autonomous agent reads it and encounters the injected payload. The human is the unwitting laundering mechanism.

**Mitigations**:
- Non-main agents never promote content from inbox to trusted zones — only the main session (human-controlled) can
- A sanitization pass (small model, no tools, no context) summarizes content before promotion, breaking most injection payloads through paraphrasing

## Structured Handoff Channels

The security of this model depends on the `queue/` channels between roles:

```
queue/
├── mail/            # Mailroom → EA. Fixed JSON schema.
├── research/        # Research → Dev, EA. Fixed JSON schema.
├── finance-reports/ # Finance → EA. Fixed JSON schema.
├── crm-drafts/     # CRM → You (main session review).
└── dev-questions/  # Dev → Research (request more info).
```

Each entry uses a fixed schema with enumerated fields and character limits on strings. No free-text blobs. This is the equivalent of forcing a bank teller to communicate only through forms with checkboxes and a short notes field — artificial bandwidth constraints that make exfiltration impractical.

Example schema for mail queue:

```json
{
  "items": [
    {
      "category": "task | info | urgent | spam",
      "from": "string, max 80 chars",
      "subject": "string, max 120 chars",
      "action_needed": true
    }
  ],
  "total": 12,
  "unread": 5
}
```

An attacker can leak at most ~120 characters through the subject field. Reducing to enumerated-only fields (no free text) closes even that.

## What You Lose

| Single-agent (human in loop) | Multi-agent (autonomous) |
|---|---|
| "Read this email and add a todo to my vault" | Mailroom summarizes → EA creates todo from summary. Less context. |
| "Research this API, then implement it" | Research produces report → Dev implements from report. Context loss at boundary. |
| "Check my CRM and draft a reply to this email" | Can't do autonomously. Drafts queued for human review. |

**Integrated reasoning across domains requires all three legs, which requires you in the loop.** The org model trades cross-domain intelligence for safe autonomous operation.

## Open Questions

- How granular should workspace zones be? More zones = more isolation but more complexity.
- Can a sanitization agent reliably break injection payloads through summarization?
- Is the structured handoff schema expressive enough for useful autonomous work?
- Should the Dev agent's git push be gated by a human approval step?
- How to handle the Research → Dev handoff when the dev agent needs to follow a URL from a research report?
