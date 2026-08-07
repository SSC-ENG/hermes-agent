---
name: ticket-writing
description: |
  Write well-structured product/engineering tickets using the INVEST framework and user story format. Use this skill whenever the user asks to create a ticket, write a user story, draft a feature request, create a Linear/Asana/Jira issue, write acceptance criteria, break down work into stories, write a bug report, or draft any kind of development task. Also trigger when the user says things like "write this up as a ticket", "turn this into a story", "create an issue for this", "I need to ticket this", or describes work that needs to be captured as a trackable item. If the user mentions INVEST, story points, acceptance criteria, or sprint planning in the context of creating work items, use this skill.
---

# Ticket Writing with the INVEST Framework

This skill produces paste-ready tickets for Linear, Asana, Jira, or any issue tracker. Every ticket follows the INVEST principles and a structured template that ensures engineering can pick it up, estimate it, and deliver it without ambiguity.

## Why INVEST Matters

INVEST is a mnemonic for six qualities that make a ticket actually useful. Without these, tickets become vague wishlists that waste engineering time, cause rework, and erode trust between product and engineering. Each letter addresses a specific failure mode:

### I -- Independent

Each ticket must stand on its own. It should be deliverable without requiring another ticket to ship first (unless explicitly marked as a dependency).

**Red flag:** The word "and" or "or" in a user story almost always means two tickets are hiding inside one. "I want to log in and reset my password" is two features with different testing steps, different regression paths, and different acceptance criteria. Split them.

**How to check:** Ask "Could engineering ship this without shipping anything else in this sprint?" If not, either split it or explicitly declare the dependency.

### N -- Negotiable

The ticket describes the what and the why, not the how. Engineering has permission (and is expected) to push back, suggest alternatives, and improve the approach during refinement. The implementation is a conversation, not a mandate.

**In practice:** When you bring a ticket to refinement, the team should feel empowered to say "What if we did it this way instead?" The ticket is a starting point for alignment, not a finished spec handed over a wall.

### V -- Valuable

The "so that" clause in the user story must articulate real business or user value. If the benefit is trivial, personal, or does not scale, the ticket should be challenged or deprioritized.

**How to check:** Read the "so that" clause out loud. Does it matter to the business, the user, or the team? Would a stakeholder nod and say "yes, that matters"? If not, rethink the ticket.

### E -- Estimatable

The ticket must be specific enough that engineering can give it a story point estimate using Fibonacci (1, 2, 3, 5, 8, 13). If the response to "how big is this?" is "I have no idea where to start," the ticket is too vague or too large.

**How to check:** Can an engineer describe the general approach and rough level of effort in under two minutes? If not, the ticket needs more detail or needs to be broken down.

### S -- Small

If a ticket estimates above 8 story points, it is too large to deliver reliably within a single sprint. Break it into smaller, independently valuable pieces.

**Why this matters:** Large tickets create risk. They are hard to estimate accurately, hard to test completely, and hard to finish within a sprint boundary. Smaller tickets create momentum, improve predictability, and make progress visible.

### T -- Testable

Every ticket must include explicit testing steps that a human (or automation) can follow to verify the feature works. If you cannot describe how to test it, you cannot ship it.

**What good testing steps look like:**
- Numbered, sequential steps
- Each step has an expected result
- Hyperlinks to relevant environments or documentation where applicable
- Cover both the happy path and at least one failure/edge case

## Ticket Template

Use this exact structure when writing tickets. Every field is required unless marked optional.

```
## Title
[Short, specific title. Action verb + object. e.g., "Add SSO login via Entra ID"]

## Type
[Feature | Bug | Product Excellence | Infrastructure | Spike]

## User Story
As a [specific user role],
I want [single piece of functionality -- no "and" or "or"],
so that [measurable business or user benefit].

## Context
[Why this matters now. Business case, stakeholder request, incident that triggered it,
or dependency that makes this urgent. Include links to relevant documents,
Slack threads, or prior tickets.]

## Acceptance Criteria
- [ ] [Criterion 1: specific, observable, binary pass/fail]
- [ ] [Criterion 2]
- [ ] [Criterion 3]
(Each criterion should be testable. Avoid vague language like "works well"
or "is fast." Use concrete thresholds: "page loads in under 2 seconds,"
"error message displays within the modal," etc.)

## Testing Steps
1. [Step 1] -- Expected result: [what you should see]
2. [Step 2] -- Expected result: [what you should see]
3. [Step 3] -- Expected result: [what you should see]
(Include environment URL if applicable. Cover happy path and at least
one edge/failure case.)

## Dependencies (optional)
- [Ticket ID]: [brief description of what must be done first]

## Reference Material (optional)
- [Link to design mockup, PRD, Figma, Canon document, etc.]
- [Link to relevant Slack/Teams thread]
- [Screenshot or diagram if helpful]

## Estimate
[Fibonacci: 1, 2, 3, 5, 8. If 13+, this ticket needs to be split.]

## Notes (optional)
[Anything else: risks, open questions, parking lot items,
migration considerations, rollback plan.]
```

## Readability Standard: Parent Stories Are for Humans (FRE 65+)

This is a binding HAA documentation standard. It governs the parent Linear issue / user
story only, not sub-issues.

1. **Parent issues and user stories must read at Flesch Reading Ease (FRE) 65 or higher.**
   Write them with the `simple-writing` skill: plain language, short sentences (under 25
   words), active voice, no unexplained jargon. Define any acronym on first use. A
   non-engineer stakeholder must be able to read the parent story alone and understand the
   value being delivered, with no clarifying questions.
2. **All technical depth goes in comments on the technical-scope sub-issues, never in the
   parent story.** Schemas, endpoints, SQL, data digests, file paths, and detailed
   acceptance criteria belong in sub-issue comments. The parent story keeps the User Story,
   Context, and a plain-language Acceptance Criteria summary; anything an engineer needs to
   implement the work lives one level down.
3. **This pairs with the Complexity Points placement rule:** Complexity Points (CPTC) go on
   the technical-scope sub-issue; the parent story / milestone estimate stays empty (0). One
   axis (points) tracks execution cost on the sub-issue; the other (FRE 65+) keeps the
   parent readable for the human who owns the value. Keep both rules together when writing
   or reviewing a ticket.

**How to check FRE 65+:** score with any standard Flesch Reading Ease calculator (word
length, sentence length). If you cannot verify a score, apply the `simple-writing` editing
workflow (BLUF, one idea per sentence, plain words, active voice) and re-read the parent
story as the newest, non-technical stakeholder on the team. If it still needs a technical
term to make sense, that term belongs in a sub-issue comment instead.

## Ticket Types Explained

**Feature:** New user-facing functionality. Has a user story. Delivers direct value.

**Bug:** Something that used to work (or was supposed to work) and does not. Include reproduction steps, environment, expected vs. actual behavior, and severity.

**Product Excellence:** Not a feature, but makes the product better under the hood. Examples: database migration for cost savings, performance optimization, security hardening, dependency upgrades. Include a business case (e.g., "saves $2,700/month in AWS maintenance costs").

**Infrastructure:** Operational work: CI/CD improvements, environment setup, monitoring, tooling. Often discovered during incidents or RCAs.

**Spike:** Time-boxed research to answer a question or reduce uncertainty before committing to a feature ticket. Output is a recommendation or decision, not code. Always has a fixed time limit (e.g., "4 hours" or "1 sprint day").

## Workflow Integration

### Where Tickets Live in the Process

1. **Backlog** -- Ticket is written but not yet refined.
2. **Refinement** -- Team reviews the ticket, challenges the INVEST criteria, adjusts scope, and estimates.
3. **Ready for Development** -- Ticket passes INVEST, has an estimate at or below 8, and the team agrees it is clear enough to start.
4. **Sprint Planning** -- Highest-value tickets are pulled into the sprint based on team capacity.
5. **In Development** -- Engineer picks it up. Sub-issues are created as needed for engineering subtasks.
6. **Testing/Staging/Production** -- Ticket progresses through environments with evidence at each stage.

### Sub-Issues vs. Feature Tickets

Feature tickets are written by product. They describe what to build and why. Engineering creates sub-issues underneath feature tickets for their own implementation tasks (e.g., "update DB schema," "write API endpoint," "add unit tests"). Sub-issues are never top-level tickets. They live under the parent feature.

### When to Split a Ticket

Split when any of these are true:
- The user story contains "and" or "or"
- The estimate exceeds 8 story points
- The ticket requires work across multiple services or teams that could ship independently
- Testing one part does not require the other part to exist

## Output Format

When asked to write a ticket, produce the filled-in template above as a Markdown code block that can be pasted directly into Linear, Asana, or any Markdown-compatible tracker. Do not add commentary outside the ticket unless the user asks for it. If the request is ambiguous, make a reasonable first pass and flag assumptions in the Notes section rather than asking a series of clarifying questions before producing output.

If the input naturally breaks into multiple tickets (the "and"/"or" rule), produce all of them, each as a separate code block, with a one-line explanation of why the split was made.
