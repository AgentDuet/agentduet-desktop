# A backend on your desktop

Stanley Leong · 2026-08-04

Why AgentDuet Desktop is shaped the way it is, in terms a backend engineer already knows.

Kept because it is useful in three places that will each want a different length: a white paper,
an explanation for a customer, and our own direction when a decision gets hard.

## The analogy

**An agent is a UI. Its tools are APIs.**

A user talks to the agent; the agent calls tools; the tools do the work. Swap "screens" for
"sentences" and the shape is a web application.

This is worth more than a teaching aid. It means the security discipline is **already invented**:
authorise every call, validate every input, constrain every output, least privilege, rate limits,
an audit trail. There is an established practice, an industry of auditors, and a vocabulary buyers
already speak. An agent product that ignores it starts from scratch for no reason.

The **OWASP API Security Top 10** is the recognised articulation of it, and the overlap is not
loose. Every problem we found in our own five tools on 2026-08-04 lands on a named category:

| what we found | the category it is |
|---|---|
| tool returns carried exception text, error codes and file paths | excessive data exposure — returning more than the caller should see |
| no rate limit on the tools that ring the owner | unrestricted resource consumption |
| a caller can repeatedly trigger a booking flow | unrestricted access to sensitive business flows |
| a customer-supplied URL would let a tool fetch anything | server-side request forgery |
| a customer tool's return value is trusted as if we wrote it | unsafe consumption of third-party APIs |
| the asker tool list could have become runtime configuration | improper inventory management |

None of these are agent-specific. They are ordinary API failures arriving through a new front
door, which is the whole argument for using this lens.

Two honest qualifications. We have **not** formally mapped our controls to the list item by item —
that is worth doing before an audit tier is sold, and it is not done. And the category names above
are paraphrased; check them against the published list before they appear in anything external.

It also tells you where to look. A penetration tester does not attack through the UI — the UI is
served to the client and can be edited, so they call the API directly with arguments of their
choosing. The equivalent here: **audit the tools as if the prompt did not exist.** The prompt is
not a boundary any more than a web form is.

## What AgentDuet Desktop is, in one line

**A small backend that runs on your own computer, whose users are the people who contact you.**

Add tools and it does more for them. The owner's knowledge is its database. The five built-in
tools are its public API. The phone number is its endpoint.

## Three places the analogy needs sharpening

Everything above carries over. These do not.

**1. Every tool is internet-facing on day one.** In a normal system you choose what to expose, and
most APIs are internal. Here the exposure *is* the product: anyone who can call or message the
owner reaches the tools. There is no internal tier to be sloppy in.

**2. No schema validates persuasion.** API discipline checks arguments. It has nothing to say about
whether the call *should* happen — and that decision is made by a model reading text an attacker
wrote. This is the part with no API precedent, and it is why every action is re-checked in code
against limits declared in advance. The model proposes; code decides.

**3. A breach here acts and speaks.** A compromised backend leaks records. A compromised secretary
makes a booking, agrees a price, or says something in the owner's name to their customer.
Contractual and reputational, not just confidential. It changes what counts as low severity.

## What AI changes

Two things, and they pull in opposite directions.

**It lets someone build a backend without being able to code.** That is the opportunity. It is also
the risk, because the thing they are building has all the exposure above and they have no security
engineer — and never will.

**It lets them audit it too.** The review discipline is well understood and describable, so it can
ship as a prompt rather than as advice. `audit-my-tools` does this: read the declarations, assume
the caller chose every argument and hears every return, report worst first.

Two limits, stated because they matter:

- **Static, not live.** Probing a real secretary rings a real phone and writes real records.
  Sandboxed testing is not built.
- **The auditor shares the author's blind spots**, and unlike a human it can be argued out of a
  finding. Good enough for the long tail of customer tools. Not sufficient for the product's own
  boundary, which stays human-reviewed and test-pinned.

Beyond that there is a paid tier: a real audit of someone's tools, by people. That is a legible
service precisely because the analogy is legible.

## The conclusion that decides our work

> **If you lower the barrier to building a backend, you must raise the floor of its security by
> the same amount.**

The customer has no security engineer. Anything that depends on their discipline will fail. So the
guardrails have to be defaults and structure, not documentation:

- The tool surface is fixed at build time, not configured at runtime.
- A capability with no declared bounds authorises nothing.
- Handlers return a status; the framework writes the sentence, so a handler cannot leak into
  caller-visible text.
- Customer code never runs in our process, and its returns are treated as untrusted input.

This is not a tax on the product. It is the difference between AgentDuet Desktop and someone wiring an agent
to their shell — and it is what a paid audit would be selling against, which is only credible if
the platform's own floor is already high.

## See also

- `docs/tool-surface-risk.md` — the attack class, with a worked example. Written for sharing.
- `docs/design.md` — the architecture, and what would reverse each decision.
