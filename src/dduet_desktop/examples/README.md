# examples — working capabilities to copy from

Each folder is one complete capability: the `capability.json` that says what may be DONE, and
the document that says what may be SAID about it. They are a matched pair by name —
`pizza_delivery` <-> `pizza-delivery.md` — which is how the code finds one from the other.

These are NOT installed. A fresh instance starts with no capabilities, because a distributed
framework should not hand every new owner a pizzeria. Copy one when it fits, or read one to see
the shape before declaring your own.

## Using one

1. Copy the document into `$DDUET_HOME/knowledge/` (default `~/.dduet/knowledge/`).
2. Declare the capability with the same name, using the bounds in `capability.json`
   (`declare_capability`, or the owner's assistant will do it if asked).
3. Check `list_knowledge`: the capability and its document should appear as a pair, with no
   MISSING marker.

## Writing your own

- The document is the say-side; the bounds are the do-side. Keep them consistent — the agent
  answers from the document and books against the bounds, and a mismatch reads to the asker as
  the agent contradicting itself. `add_knowledge` refuses a fact that disagrees with a bound.
- Write the document in the words an ASKER would use, including their spelling. "What time do
  you close?" found nothing against a document that said only "Hours: 11:00-21:00", and
  "gluten free" missed "Gluten-free".
- One assertion per `- ` bullet, grouped under a `## ` heading, so a fact can be corrected in
  place later rather than appended to.

## The three files of a capability

| file | holds | why that format |
|---|---|---|
| `capability.json` | the bounds that **gate** the action — hours, max, verified-only | code compares these to decide whether to book; a mis-parse authorises something wrongly, so they are typed and machine-owned |
| `<name>.md` | what may be **said** — prices, specifics, the words askers use | the model reads it as prose, and it is the single source for values that are only quoted, never compared |
| `<name>.html` | what may be **clicked** — optional | the shape follows the domain: a menu with sizes looks nothing like a callback form. Omit it and the framework serves a generic time-picker built from the bounds |

Copy the `.md` into `$DDUET_HOME/knowledge/` and the `.html` (if any) into `$DDUET_HOME/canvas/`.
Both are named after the capability — that is how the code finds one from the other.

**Where a value must live in both** (opening hours are gated *and* quoted), the JSON owns it and
the prose must agree. Both directions are checked: `add_knowledge` refuses a fact that
contradicts a bound, and `set_capability_bound` warns which document lines have gone stale.
