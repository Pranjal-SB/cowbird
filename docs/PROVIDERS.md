# Provider inventory

The backlog. Every known disposable-mail backend, its front doors, and what it
can do. Source: curated list, 2026-09-05.

**A backend is the unit, not a URL.** Several of these serve the same service
behind different domains and skins; one adapter covers all of them.

`addr` = how long the address lives. `msg` = how long a received message is
retained. `?` = not established yet.

Legend: ⭐ seed set · 🔓 open source, self-hostable · 🔗 shares a backend with
its listed aliases

## Shipped

Live and contract-tested. The original three (mail.tm, emailnator, inboxes)
are additionally proven end to end by `packages/core/tests/test_delivery.py`,
a `live`-marked suite. Of the eight newer providers, six were proven by hand on
2026-09-24: a test mail sent from testemailsender.com (JoltMx) arrived in
guerrillamail, temp-mail.org, 22.do, inboxkitten, nicemail and mail.cx within
33 seconds and read back through `get()`. maildrop rejects that sender with
`554 Invalid FCRDNS`, and 10minutemail could not issue an address from this
machine, so for those two the read path is still proven only against recorded
fixtures. temp-mail.io, temporarymail and re146 were added the same day and
passed `test_delivery.py` itself, JoltMx to `get()`, in 31 seconds for all
three. tempmail.plus and mailticking followed and passed it in 8 and 38
seconds. disposablemail and DuckMail passed it through the xeramail
fallback after JoltMx's daily quota ran out; tempmailhub and
reusable.email passed their live read tests against inboxes that already
held mail. P50 is measured, from a real `cowbird providers` run on
2026-09-24, not declared; the nine newest have not seen CLI traffic yet.

| backend | sites | kind | addr | msg | domains | p50 |
|---|---|---|---|---|---|---|
| mail.tm | mail.tm | own-domain | forever | 7d | 1 | 1.2s |
| emailnator | emailnator.com | gmail-alias | forever | 1d | 6 | 1.1s |
| inboxes | inboxes.com | own-domain | forever | 7d | 18 | 0.0s |
| guerrillamail 🔗 | guerrillamail.com, sharklasers.com, cs.email, dismail.top | own-domain | 1h | 1h | 11 | 0.4s |
| temp-mail.org 🔗 | temp-mail.org, 10minemail.com | own-domain | forever | 2h | 1 | 0.4s |
| 22.do | 22.do | gmail-alias, outlook-alias, own-domain | 1d | 1d | 3 | 0.9s |
| maildrop 🔗 | maildrop.cc, trashmail.ws | own-domain | forever | 1d | 1 | 0.2s |
| inboxkitten | inboxkitten.com | own-domain | forever | 1d | 1 | 0.6s |
| nicemail 🔗 | nicemail.cc (API: web.mailporary.com) | own-domain | forever | 1d | 6 | 0.9s |
| mail.cx | mail.cx | own-domain | forever | 1h | 3 | 13.3s |
| 10minutemail | 10minutemail.com | own-domain | 10m | 10m | 1 | — |
| temp-mail.io | temp-mail.io | own-domain | 1d | 1d | 7 | — |
| temporarymail | temporarymail.com | own-domain | 14d* | ? | 9 | — |
| re146 | mail.re146.dev | own-domain | 1d | 1h | 13 | — |
| tempmail.plus | tempmail.plus | own-domain | ? | ? | 9 | — |
| mailticking | mailticking.com | gmail-alias | ? | ? | 2 | — |
| tempmailhub | tempmailhub.org | gmail-alias | 15m | ? | 1 | — |
| reusable.email | reusable.email | own-domain | forever | 90d | 1 | — |
| disposablemail 🔗 | disposablemail.com, fakemail.net, minuteinbox.com | own-domain | 10m-1h | ? | 3 | — |
| DuckMail 🔗 | duckmail.sbs, freetempmail.com | own-domain | 1d | ? | 19 | — |

`temporarymail` addresses persist only if used once every 14 days. re146's
domains churn, so the adapter fetches them on every `generate()`. temp-mail.io
serves 7 domains today, not the 12 recorded in recon. tempmailhub hands out
whole Gmail accounts from a shared pool, so its inboxes already hold other
people's mail. reusable.email inboxes are public: use an unguessable local
part. DuckMail speaks the mail.tm API and reuses that adapter.

inboxes is a catch-all: `generate()` makes no HTTP call at all, it picks a
local-part and a domain and returns, which is where the 0.0s comes from. It
also accepts a custom local-part, as do maildrop, inboxkitten, nicemail and
mail.cx. emailnator and mail.tm do not.

10minutemail has no p50. On the 2026-09-24 run it drew a Cloudflare challenge
from this machine's egress, same as on 2026-09-12, when it answered normally
over WARP. mail.cx reads slow because its list call is a server-side
long-poll.

The p50 comes from latencies the health store records as a process makes
calls. The provider suites build their own transports, so no test run can put
a number here; only real traffic through the CLI or the server can. `addr` and
`msg` for the eight newer providers are what the
adapter declares, which is what routing acts on. Two differ from the recon rows
they replace: mail.cx retains a message for an hour rather than the twelve the
site suggested, and temp-mail.org issues from one domain, not the unknown count
recorded earlier.

## Parked

Probed, not built. The reason is in the table.

| backend | why |
|---|---|
| smailpro | every call needs a solved Cloudflare Turnstile token in `x-captcha` |
| tempr.email | buildable: the inbox streams over Datastar SSE from `mta.trashmailr.com:81`, which needs its own client |
| dropmail | the free API token path closed |

dropmail was in the seed set only because it was the one known push
(WebSocket) backend, so `watch()` could be proven against a real push provider
before polling adapters were built on it. No shipped provider uses push today.

## Seed set, remaining

| backend | sites | kind | addr | msg | domains |
|---|---|---|---|---|---|
| ⭐ smailpro | smailpro.com | gmail-alias, outlook-alias | ? | ? | 30 free, 43 total |
| ⭐ tempr.email | tempr.email | own-domain | ? | 30d | 60 |

Recon corrected both rows. smailpro's 43 domains are 2 Gmail, 9 Outlook and 32
in the `other` pool, of which 13 are premium: an anonymous caller reaches 30.
tempr.email serves 60 domains, not the "50+" the source list claimed and not
the "50" its own page says in one place, and its 30 days is message retention,
with address lifetime still unestablished.

## Starred, not seeded

Next in line once the interface holds.

| backend | sites | kind | addr | msg | domains |
|---|---|---|---|---|---|
| ⭐ zemail | zemail.me | gmail-alias | forever | 1d | 7 |

## Self-hostable

Own the backend, and it cannot rot. These are the floor under the pool.

| backend | sites | source | domains |
|---|---|---|---|
| 🔓 cloudflare_temp_email | mail.awsl.uk | `dreamhunter2333/cloudflare_temp_email` | 5 |
| 🔓 sunls-tmail | mail.sunls.de | `sunls24/tmail` | 3 |

## Gmail / Outlook alias backends

Distinct because they produce addresses at a real, non-disposable domain, which
is what defeats domain blocklists.

| backend | sites | addr | msg | domains |
|---|---|---|---|---|
| tmail.io | tmail.io | forever | 1d | 4 |
| temptom | temptom.com | forever | 1d | 15 |
| ghostinbox | ghostinbox.net, temp-gmail.ghostinbox.net | 1d | 1d | 10 |

## Own-domain backends

| backend | sites | addr | msg | domains |
|---|---|---|---|---|
| yopmail | yopmail.com | forever | 8d | 100+ |
| mailnesia | mailnesia.com | forever | 2d | 1 |
| generator-email 🔗 | generator.email, emailfake.com, tempm.com, mail-temp.com | ? | ? | 50+ |
| mintemail 🔗 | mintemail.com, tempail.com | 1h | 1h | 1 |
| anonymmail 🔗 | anonymmail.net, mail.td | ? | ? | 5 |
| emailondeck | emailondeck.com | ? | ? | 1 |
| tmail (Laravel) 🔗 | emailtemp.org, emailgenerator.org | ? | ? | 1 |
| haribu | haribu.net | ? | ? | 1 |
| altaddress | altaddress.org | forever | 3d | 14 |
| driftz | driftz.net | ? | ? | 23 |
| moakt | moakt.com | 1h | 1h | 13 |
| temporary-mail | temporary-mail.net | forever | ? | 11 |
| 48hr.email | 48hr.email | forever | 2d | 7 |
| tempboxpro | tempboxpro.com | session | ? | 6 |
| mails.org | mails.org | ? | 30m | 5 |
| spambox | spambox.xyz | forever | 1d | 4 |
| urtempmail | urtempmail.com | 1d | 1d | 4 |
| xeramail | xeramail.com | 1d | 1d | 2 |
| vmail.dev | vmail.dev | 1d | 1d | 2 |
| duckspam | duckspam.com | forever | forever | 1 |
| tempemail.cc | tempemail.cc | forever | forever | 1 |
| mohmal | mohmal.com | 45m | 45m | 1 |
| adguard | adguard.com/adguard-temp-mail | 7d | 1d | 1 |
| tempmailo | tempmailo.com | 2d | 2d | ? |
| tmail.link | tmail.link | ? | ? | ? |

## Ten-minute tier

Short-lived by design. Low value for slow signup flows, fine for fast OTP.

| backend | sites |
|---|---|
| muellmail | muellmail.com |
| linshi | linshi-email.com |

## .edu tier

Separate capability kind. Unverified, several likely paid or dead. Lowest
priority; investigate before committing to any.

edumailfree.com · zenvex.dev · tempsmail.org · emailgenerator.org · edumail.su ·
run2mail.com · getedumail.com · mtempmail.com · freetempmail.com · imail.edu.vn ·
vanishinbox.com · instantedumail.com · etempmail.com

## From the secondary list

Not yet classified. Source: `rentry.org/i3ozxg6f`.

m.kuku.lu ·
anonbox.net · luxusmail.org · eyepaste.com · tempmail.net · mailtemp.dev ·
tempmailb.com · tempmail4u.com · fake.legal · emailme.at · minmail.app ·
internxt.com/temporary-email · receivemail.org · fakemailgenerator.com ·
tempinbox.xyz · tmailor.com · cryptogmail.com · 10minutemail.net

## Out of scope

- Telegram bots (`t.me/TempMail_org_bot`, `t.me/reusable`, `t.me/fakemailbot`,
  `t.me/smtpbot`) — a different transport, not an HTTP backend.
- Browser extensions (Bloody Vikings!) — a client, not a service.
- mailsac, mailinator — paid/commercial tiers, different product.

## Counts

Roughly 90 front doors. A probe of every host
folded four rows into backends already listed: cs.email and dismail.top are
guerrillamail, 10minemail is temp-mail.org, and emailfake is generator-email.
That leaves roughly 55 distinct backends, of which 20 ship.
