# The platform we do not own

What we know about **Nexus**, **wss-edge** and the **agentduet SDK**, and how we learned it.

Not a design document and not a task list. This is reference about somebody else's system —
the most expensive kind of knowledge here, because it cannot be read off our own code and it is
re-derived by reading protos, probing servers, or asking someone and waiting.

**Every entry says how it was established.** A fact read from a proto, a fact observed on a live
wire, and a fact somebody told us in chat fail in different ways, and the difference matters
when one of them turns out to be wrong.

> **Fetch before you read someone else's tree.** Twice on 2026-08-31 a stale clone produced a
> confident wrong answer about work another team had already shipped — once concluding OAuth was
> only a design document while 33 files sat merged on `main`, from a checkout 33 commits behind.
> The cost of `git fetch` is a second. The cost of skipping it was nearly asking Tuan to build
> what he finished a week earlier.

---

## DDUET is BaChat — business-account chat, not the person-to-person app

`AddressNetwork` is exactly `{WA, TELCO, DDUET}`, and DDUET **is** Nexus BaChat. Nexus keeps
person-to-person in a separate **friend** module, and `grep -rni friend` across all of wss-edge
returns nothing.

**So a message someone sends you as a friend in the mobile app never reaches a connector.** What
does arrive is anything addressed to a **BA the connector is a member of** — including a human
colleague's reply sent *as* that BA from web or mobile, which lands as ordinary inbound.

Demoing this means messaging the BA, not the person.

*Established by reading `nexus/mono/bachat/ba_chat_http.proto`, 2026-08-27; confirmed
independently by Tuan in #AI-Product: "the friend-to-friend on dduet doesn't go through to
agentduet, only msg sent to BA".*

## Identity: `account_uid` is the key, the email is a label

- **Stable identity — `account_uid`.** Always on the relay as `senderAccountUid`. The 2026-08-10
  design makes it required: *"Every identity decision keys on an account uid."*
- **Verified property — `kyc_status`** (`NONE` / `VERIFIED`) on `BaChatUserInfo`. **Not on the
  relay.** It comes from `GetBaChatUserInfo`, which the connector plane does not expose, so it is
  unreadable today.
- **No anonymous sender exists.** `AccountType` is exactly `{UNSPECIFIED, PERSONAL, BA}` — no
  guest type — and reaching a BA through its public slug requires SSO. A stranger at a slug is
  **pseudonymous**, not anonymous: a stable id always, a readable name usually, a verified
  identity only when KYC says so.

**THE TRAP: do not key on the email.** `user_metadata` is `{email, name}` with *"either key
possibly absent"*; it is `orElse(null)`; it is absent on every BA-authored relay because *"a BA
account holds no email row"*; and the design says outright that *"`userEmail` is no longer a
dependable identity"* because it **flips to the staff member's address** when a colleague replies
as the BA. Key on the uid, show the name.

*Established from the protos 2026-08-28, and confirmed on a live message 2026-08-31 —
`{"email": "…@gmail.com", "name": "Stanley Leong"}` did arrive for a signed-in personal sender.*

## A new conversation's first message arrives twice — or once

Opening a conversation produces a `CONVO_CREATED` system frame whose `dataJson.title` holds the
first sentence of the first message. The real text frame follows **about 35ms later**, same
`sessionUid`, its own `msgUid`.

**Usually.** On 2026-08-28 only the event ever came and the message was never relayed. So both
must be handled: hold the title briefly, use it only if no text frame claims the conversation.

*Two live observations, and they disagreed — which is the point. One observation of an external
system is a hypothesis. The first sample was equally consistent with "nexus never relays the
first message" and "nexus relays it 35ms later and we missed it", and code written on the first
reading created a duplicate of every opening message.*

## Inbound is durably queued — for 24 hours

Every inbound message is JetStream-persisted before delivery; the live socket is a **fast path**
on top of that, not the only path. Messages *"enqueued while no connection was live"* are
*"delivered on connect/reconnect via PEL/backlog"*, and the backlog drains **before** the fast
subscriber resumes, so a reconnect gets what it missed, in order, then anything new.

| setting | value |
|---|---|
| `streamMaxAge` | **24 hours** |
| `ackWait` | 30 seconds |
| `maxDeliver` | 3 |
| `deadLetterMaxAge` | 14 days |

**So the app may be closed and still receive — up to a day.** Beyond that the message is gone as
far as anything we can reach: still in Nexus, where the sender sees their own thread, but the
connector has no way to ask what it missed, because `QueryBaChatMessages` is not exposed.

**That is a data-loss window on a laptop that gets closed over a weekend**, and it is the real
argument for the read RPCs below — sharper than "our people list looks empty".

*Established from `docs/superpowers/specs/2026-07-23-inbound-fast-path-design.md` and
`NatsQueueProperties`, 2026-08-31.*

## Three read RPCs exist in Nexus; the connector cannot call them

Defined in `ba_chat_http.proto`:

- **`GetBaChatUserInfo`** — `ba_uid` required; a blank `account_uid` returns the BA's **whole
  user list**, paginated, sortable by display name.
- **`ListBaChatSessions`** — the inbox: members, title, `last_message_at`.
- **`QueryBaChatMessages`** — history; a blank `session_uid` means every session the caller is a
  member of.

**wss-edge wires exactly one baChat path: `internal/baChat/v1/agentPostMessage`.** The 2026-08-10
adaptation design puts `agentListSessions`/`agentQueryMessages` under *"Out of scope,
deliberately"*. So this is a plumbing gap, not a missing capability — the ask upstream is to
expose what exists.

**One limit is deliberate, not an oversight:** `BaChatUserInfo.emails` is populated **only** on a
single-user lookup — *"so a connector can turn one relayed userUid into an emailable participant,
not so a whole customer list can be harvested in one call"*. Do not design around removing it.

*Read from the vendored proto 2026-08-27. Tuan replied "i'm good with this"; no timeline yet.*

## Discovery is a public slug

`PostBaChatMessageRequest.profile_url` is a **public BA slug** (`dduet.com/<slug>`), and a message
may be minted against it *instead of* an account uid. So discovery is a URL anyone can hold, and
reaching it requires SSO.

Ours is `dduet.com/stanley-production-ba`, provisioned by Hallie 2026-08-31.

**Environments do not mix.** exp AgentDuet talks to staging DDUET, prod to prod. A staging BA
relays to a staging connector; a `wss-prod` client never sees it.

*Hallie, #AI-Product 2026-08-31: "1 BA can connect with 1 connector but 1 connector can connect
with multiple BAs" — which is why every outbound send passes `ba_uid`, or the server answers
`AMBIGUOUS_BA`.*

## Outbound has no window and no templates

The connector can **start** a conversation: omit `session_uid`, pass the target's account uid as
`participant`, and Nexus mints the session. No 24-hour customer-service window, no approved
templates — neither of WhatsApp's limits applies.

That makes DDUET the cheaper route to delivering a held reply, and the strongest argument for
carrying both channels rather than treating WA as the replacement.

## OAuth: built, working, dev-only

wss-edge merged desktop OAuth to `main` on **2026-08-25** — `vonhutuan-b3`, PR #53, PKCE plus
federated login, rotating refresh tokens, Bearer at the SM-WS and REST doors. 33 files, plus
`./gradlew :server:oauthE2eTest` covering the negative cases against a stub IdP.

**Proven end to end from here, 2026-08-31**, against dev: a real Google consent screen, and a
connector **auto-provisioned** because that identity had none — which is the "connector
provisioning" blocker closing itself.

**"Needs VPN" is not true from this machine.** `wss-dev.internal.b3networks.com` resolves to
`100.100.221.234` and answers on **:8080 over plain HTTP** — the same SD-WAN route that reaches
`internal-apigw-eks` and the T4 box. **Port 443 times out**, which is exactly what makes it look
unreachable if you only try HTTPS, and exactly what was tried first.

`agentduet-desktop` is already a registered client there, and the loopback redirect
(`http://127.0.0.1:<port>/callback`) passes their `LoopbackRedirectUriValidator`.

**Not on prod.** Behind a VPN the feature cannot do its job: the whole point is that an owner
installs the binary and clicks Google, and only B3 staff can reach a VPN'd host.

**Signing in provisions a connector on whatever environment the URL names.** Point it somewhere
non-production and the install silently moves off the connector its DID and production BA route
to. Use a throwaway `AGENTDUET_HOME`, and check `connector.environment()` either side.

*Tuan, #AI-Product 2026-08-31: "it is already on our dev server… that server needs vpn to connect
to… ya not on prod yet".*

## The SDK: `1.1.0` is a pre-release, and that is a silent trap

`Network.DDUET`, `DduetMessage` and `SendDduetMessage` shipped in `agentduet` `1.1.0b1`/`b2`/`b3`.
They are **pre-releases**, so PyPI still serves `1.0.0` as latest and `agentduet>=1.0.0` resolves
a clean install to a version with **no DDUET at all**.

The failure is silent absence, not an error. Pin it before relying on the channel.

`SessionManagerConfig.base_url` overrides the host (env `AGENTDUET_BASE_URL`), with
`PROD_ENDPOINT = wss://wss-prod.agentduet.com` as the fallback — so pointing a throwaway client
at another environment needs no code change.

*Established from PyPI and the installed wheel, 2026-08-27 and 2026-08-31.*
