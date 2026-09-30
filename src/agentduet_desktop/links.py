"""Put a prefilled calendar event or email draft on the owner's screen — as a LINK.

WHY A LINK AND NOT AN API. Google's calendar and Gmail APIs need an OAuth token with a
sensitive scope (`calendar.events`, `gmail.send`), a verified app, and a consent screen. Our
sign-in is FEDERATED — the app talks to wss-edge, wss-edge talks to Google — so what this
install holds is an AgentDuet token, not a Google one. There is no token here to call Google
with. A link needs none: the owner is already signed in to Google IN THEIR BROWSER, so the
page opens with their session and their account.

WHAT THAT COSTS, said plainly rather than discovered later:

- **Nothing is created and nothing is sent.** The link opens a FORM, prefilled. Google saves
  the event when the owner clicks Save; the mail client sends when they click Send. So this
  cannot post to a calendar the owner does not look at, and it cannot mail anyone silently.
  That is a limitation of the feature and the whole of its safety argument.
- **No attachments**, and no recording or transcript can ride along. A link carries text.
- **A URL has a length ceiling.** `MAILTO_LIMIT` below is the practical one; a long body is
  REFUSED with its size, not silently truncated by whatever opens it.

THE SECURITY PROPERTY, and it is the same one `wasm_host.resolve_url` holds: **a caller passes
FIELDS, and this module builds the URL.** There is no argument in which a URL means anything —
the host, the path and the parameter names are literals here, and every value the caller
supplies is percent-encoded into a parameter slot. So a tool (or a model reading a stranger's
message) cannot make this open an arbitrary page on the owner's desktop, which is what an
`open_url(url)` helper would have handed it. `reveal.py` refuses to open a PATH it was given
for exactly the same reason; the opener here is private for exactly that reason too.

The one field that IS a destination is the email recipient, and there is no getting around
that — a draft to nobody is not a draft. It is safe because of the first bullet, not because of
the encoding: the owner reads the recipient in their own mail client before pressing Send.
"""

import logging
import os
import platform
import re
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("dduet.links")

#: Where the owner ends up. LITERALS, and the only two destinations this module has.
CALENDAR_URL = "https://calendar.google.com/calendar/render"

#: How long an event lasts when nobody says. An hour is the convention every calendar UI uses
#: for a dragged-out slot, and a missing end time is far more likely to be "the usual" than a
#: request for a zero-length event.
DEFAULT_MINUTES = 60

#: The practical ceiling on a `mailto:` URL. The spec sets none; the handlers do — Windows caps
#: a shell command near 2,048 characters and several clients truncate around 2,000. Refusing at
#: 1,800 leaves room for the address and subject and keeps the failure OURS, where it can say
#: what happened, rather than a body that arrives silently cut in half.
MAILTO_LIMIT = 1800

#: Enough to be a real address without pretending to implement RFC 5322. Its job is to reject
#: something that is not an address at all, not to certify one that is.
ADDRESS = re.compile(r"^[^\s@,<>\"]+@[^\s@,<>\"]+\.[A-Za-z]{2,}$")

#: What no field may contain: control characters, which do nothing useful in any of these and
#: are how a value stops being a value.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: THE ONE-LINE FIELDS. A newline in a `mailto:` subject is the header-injection shape, and a
#: calendar title is one line by construction — so the break is REMOVED there rather than
#: encoded. A body and a description are the opposite case: a paragraph break is the point of
#: them, arrives as `%0A`, and is a value the whole way.
LINES = re.compile(r"[\r\n\t]+")


def _clean(value: str, limit: int) -> str:
    """One multi-line field — control characters gone, and a length it cannot exceed."""
    return CONTROL.sub("", str(value or "")).strip()[:limit]


def _line(value: str, limit: int) -> str:
    """One single-line field: as `_clean`, and with every break flattened to a space."""
    return LINES.sub(" ", _clean(value, limit)).strip()


def available() -> tuple[bool, str]:
    """Whether a link can be opened at all — same precondition as `reveal.open_folder`."""
    system = platform.system()
    if system in ("Darwin", "Windows"):
        return True, ""
    if not shutil.which("xdg-open"):
        return False, "no xdg-open on this machine"
    if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        return False, "no desktop session — this looks like a headless machine"
    return True, ""


def _open(url: str) -> None:
    """Hand one of OUR urls to the desktop. Private on purpose — see the module docstring."""
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", url])
    elif system == "Windows":
        os.startfile(url)                          # noqa: S606  (Windows-only)
    else:
        # Detached, and its output discarded: a browser started here must not die with the
        # daemon, and a chatty xdg-open must not interleave itself into the log.
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def _moment(value: str) -> datetime:
    """A typed date and time as an aware datetime, or a ValueError naming what was wrong.

    Deliberately narrow. It accepts what a person types — `2026-09-10 15:00`, with or without
    a `T`, with or without seconds — and nothing clever: no "tomorrow", no "next Tuesday". A
    model that guesses a date wrong writes a wrong event, and the owner sees a filled form and
    clicks Save. So the date has to come from the owner, and this is the last place that can
    insist on it.

    A naive time means the owner's own clock, which is what they typed and what they mean.
    """
    text = _line(value, 40).replace("T", " ")
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            when = datetime.strptime(text, shape)
        except ValueError:
            continue
        return when.astimezone() if when.tzinfo is None else when
    raise ValueError(f"{value!r} is not a date and time. Write it as 2026-09-10 15:00.")


def _stamp(when: datetime) -> str:
    """Google's `dates` format, in UTC.

    UTC rather than a local time plus `ctz=`, because `ctz` wants an IANA zone name and Python
    can only report the machine's abbreviation (`+08`, `SGT`) without one — and a zone Google
    does not recognise is silently ignored, which moves the meeting.
    """
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def calendar_url(title: str, start: str, end: str = "", notes: str = "",
                 location: str = "") -> str:
    """The Google Calendar link for one event. Raises ValueError on a field it cannot use."""
    subject = _line(title, 200)
    if not subject:
        raise ValueError("an event needs a title.")
    begins = _moment(start)
    finishes = _moment(end) if str(end or "").strip() else begins + timedelta(minutes=DEFAULT_MINUTES)
    if finishes <= begins:
        raise ValueError(f"the end ({finishes:%Y-%m-%d %H:%M}) is not after the start "
                         f"({begins:%Y-%m-%d %H:%M}).")
    fields = {"action": "TEMPLATE", "text": subject,
              "dates": f"{_stamp(begins)}/{_stamp(finishes)}"}
    if notes:
        fields["details"] = _clean(notes, 2000)
    if location:
        fields["location"] = _line(location, 300)
    # Every value encoded into a parameter slot on a literal host. Nothing here can move the
    # destination, which is the property the module docstring describes.
    #
    # `safe="/"` because the slash between the two stamps is Google's own separator and it does
    # not read a `%2F` there — `urlencode` encodes everything by default, so the range arrived
    # as one unparseable string and the event opened with no time at all.
    return CALENDAR_URL + "?" + urllib.parse.urlencode(
        fields, quote_via=urllib.parse.quote, safe="/")


def mailto_url(to: str, subject: str = "", body: str = "") -> str:
    """The `mailto:` link for one draft. Raises ValueError on a field it cannot use.

    A RECIPIENT THAT IS NOT ONE PLAIN ADDRESS IS LEFT OUT, not refused (2026-09-28). The owner
    knows people by name and callers arrive as numbers, so "email Kok Choong" gave a name, the
    draft was refused, and — the refusal never reaching the screen — nothing happened at all.
    A draft with no recipient is valid (RFC 6068) and the owner types the address in their mail
    client. The strict check still decides what may ENTER the link: text that is not one
    address never reaches it, so it cannot add a recipient or a header.
    """
    address = _line(to, 200)
    if not ADDRESS.match(address):
        address = ""
    fields = {}
    if subject:
        fields["subject"] = _line(subject, 200)
    if body:
        fields["body"] = _clean(body, 4000)
    # `safe="@"` — an `@` is legal in a mailto address (RFC 6068) and a `%40` is not read by
    # every client, so encoding it turns a valid draft into a mail to nobody.
    url = "mailto:" + (urllib.parse.quote(address, safe="@") if address else "")
    if fields:
        url += "?" + urllib.parse.urlencode(fields, quote_via=urllib.parse.quote)
    if len(url) > MAILTO_LIMIT:
        raise ValueError(f"that draft is {len(url)} characters as a link, and a mail client "
                         f"will not take more than about {MAILTO_LIMIT}. Shorten the message — "
                         f"a link cannot carry a transcript.")
    return url


def add_to_calendar(title: str, start: str, end: str = "", notes: str = "",
                    location: str = "") -> str:
    """Open a new Google Calendar event, prefilled. The owner saves it — this does not."""
    ok, why = available()
    if not ok:
        return f"Cannot open a link here: {why}."
    try:
        url = calendar_url(title, start, end, notes, location)
    except ValueError as exc:
        return f"Not opened: {exc}"
    try:
        _open(url)
    except OSError as exc:
        logger.warning("could not open a calendar link: %s", exc)
        return f"Could not open the browser: {exc}"
    return (f"Opened Google Calendar with {_line(title, 200)!r} prefilled. "
            "It is not in the calendar until you press Save there.")


def recipient(to: str) -> tuple[str, list[str]]:
    """The address a draft goes to, and any other addresses the same card holds.

    `to` may be an address, or — since the owner knows people by name and callers arrive as
    numbers — a name or a number, looked up in Contacts (2026-09-30). The first address on the
    card is used and the rest are reported, never added: the owner can switch in the mail
    client, and a draft to two addresses is a decision the owner did not make.
    """
    typed = _line(to, 200)
    if ADDRESS.match(typed):
        return typed, []
    from . import names
    found = [e for e in names.emails_for(typed) if ADDRESS.match(_line(e, 200))]
    return (found[0], found[1:]) if found else ("", [])


def draft_email(to: str, subject: str = "", body: str = "") -> str:
    """Open an email draft, prefilled, in the owner's mail client. Nothing is sent."""
    ok, why = available()
    if not ok:
        return f"Cannot open a link here: {why}."
    address, others = recipient(to)
    try:
        url = mailto_url(address, subject, body)
    except ValueError as exc:
        return f"Not opened: {exc}"
    try:
        _open(url)
    except OSError as exc:
        logger.warning("could not open a mail draft: %s", exc)
        return f"Could not open the mail client: {exc}"
    if not address:
        who = _line(to, 80)
        return (f"Opened a draft with no recipient — add {who + chr(39) + 's' if who else 'the'} "
                "email address in your mail client. Nothing is sent until you press Send.")
    also = f" Contacts also has {', '.join(others)}." if others else ""
    return (f"Opened a draft to {address}.{also} Nothing is sent until you press Send "
            "in your mail client.")


#: A NUMBER, and nothing else: an optional "+", then digits, spaces and the separators people
#: write. It becomes a TEL line in a vCard, where a newline would start a field of its own.
NUMBER = re.compile(r"^\+?[0-9][0-9 ()./-]{5,24}$")


def _vcard_text(value: str) -> str:
    """RFC 6350 escaping for a text value: backslash, comma, semicolon and newline."""
    for raw, escaped in (("\\", "\\\\"), (",", "\\,"), (";", "\\;")):
        value = value.replace(raw, escaped)
    return value.replace("\r", " ").replace("\n", " ")


def vcard(name: str, number: str) -> str:
    """One contact as a vCard 3.0. Raises ValueError on a number that is not one.

    THE SAME PROPERTY AS THE LINKS: the caller passes FIELDS and this builds the card, so a name
    cannot smuggle in an email, a URL or a second contact — it is escaped into one value.
    """
    number = _line(number, 40)
    if not NUMBER.match(number):
        raise ValueError(f"{number!r} is not a phone number")
    name = _line(name, 120)
    lines = ["BEGIN:VCARD", "VERSION:3.0"]
    if name:
        parts = name.split(" ", 1)
        given, family = (parts[0], parts[1]) if len(parts) == 2 else (name, "")
        lines += [f"N:{_vcard_text(family)};{_vcard_text(given)};;;",
                  f"FN:{_vcard_text(name)}"]
    else:
        lines += ["N:;;;;", f"FN:{_vcard_text(number)}"]
    lines += [f"TEL;TYPE=CELL:{_vcard_text(number)}", "END:VCARD"]
    return "\r\n".join(lines) + "\r\n"


def add_contact(number: str, name: str = "") -> str:
    """Hand Contacts a prefilled card to add. The owner confirms there — this saves nothing.

    A vCard OPENED, not a contact WRITTEN (2026-09-30): the Contacts app asks before it adds a
    card, which is the same confirm step as Save in Calendar or Send in Mail, and it needs no
    Contacts permission because this app never touches the address book.
    """
    ok, why = available()
    if not ok:
        return f"Cannot open Contacts here: {why}."
    try:
        card = vcard(name, number)
    except ValueError as exc:
        return f"Not opened: {exc}"
    from . import paths
    digits = re.sub(r"\D", "", number)
    path = paths.RUN / "contacts-new" / f"{digits}.vcf"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(card, encoding="utf-8")
        _open(str(path))
    except OSError as exc:
        logger.warning("could not open a contact card: %s", exc)
        return f"Could not open Contacts: {exc}"
    who = _line(name, 120) or _line(number, 40)
    return f"Opened a new contact for {who} in Contacts. It is added only when you confirm there."
