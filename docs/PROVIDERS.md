# Provider inventory

The backlog. Every known disposable-mail backend, its front doors, and what it
can do. Source: curated list, 2026-09-05; reconciled against the code and a
live probe of every unshipped host, 2026-11-15.

**A backend is the unit, not a URL.** Several of these serve the same service
behind different domains and skins; one adapter covers all of them.

`addr` = how long the address lives. `msg` = how long a received message is
retained. `?` = not established yet.

Legend: ⭐ seed set · 🔓 open source, self-hostable · 🔗 shares a backend with
its listed aliases

## Shipped

Thirty-two adapters ship. They expose 40 front doors between them, all
unique. Live and contract-tested. The original three (mail.tm, emailnator, inboxes)
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
| maildrop | maildrop.cc | own-domain | forever | 1d | 1 | 0.2s |
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
| smailpro 🧩 | smailpro.com | gmail-alias, outlook-alias | ? | ? | 3 | — |
| cftempmail 🧩🔓 | mail.awsl.uk | own-domain | 100d | ? | 3 | — |
| xeramail 🧩 | xeramail.com | own-domain | 1d | 1d | 2 | — |
| vanishinbox 🧩 | vanishinbox.com | own-domain, edu | ? | 10m | 9 | — |
| zenvex 🧩 | zenvex.dev | own-domain, edu | ? | ? | 6 | — |
| etempmail 🧩 | etempmail.com | edu | 20m | ? | 3 | — |
| eyepaste | eyepaste.com | own-domain | ? | ? | 1 | — |
| receivemail | receivemail.org | own-domain | ? | 3d* | 3 | — |
| emailtemp 🔗 | emailtemp.org, emailgenerator.org | own-domain, edu | ? | ? | 7 | — |
| mtempmail | mtempmail.com | own-domain, edu | 1d | 1d | 3 | — |
| tempmailo | tempmailo.com | own-domain | ? | 2d | 4 | — |
| instantedumail | instantedumail.com | own-domain, edu | 1h | ? | 4 | — |

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

🧩 smailpro needs a Cloudflare solver (`COWBIRD_SOLVER_URL`, see the README):
creating an alias and reading a body each cost one Turnstile solve, 5 to 10
seconds each. Without a solver the pool skips it. Its page lists 43 domains,
but the free tier only issues gmail.com, googlemail.com and outlook.com, and
its listing gives a sender's display name without the address. It passed
`test_delivery.py` through the solver in 43 seconds.

The other 🧩 providers pay for a solve less often. cftempmail and xeramail solve
once per address and then read with the token that returns. zenvex and
vanishinbox solve once for a pass cookie (12 hours and 2 hours) that reads
every address. etempmail solves once per address and replays its session
cookie. All five passed `test_delivery.py` through the solver on 2026-09-25,
in 13 to 24 seconds. The edu providers hand out `.edu.pl` addresses, which
anyone can register: they are not US university mail. zenvex and vanishinbox
inboxes are public by address. cftempmail is the public instance of an
open-source worker; another instance is a subclass overriding `api`.

Six more need no solver and passed `test_delivery.py` on 2026-09-28 in 8 to
20 seconds: eyepaste, receivemail, emailtemp, mtempmail, tempmailo and
instantedumail. All but instantedumail are public by address, so use an
unguessable local part; instantedumail's Supabase row-level security keeps
each inbox to its own anonymous user. eyepaste's feed claims a one-hour drop
but held mail 35 hours old, and receivemail's 3 days is a floor, not a
measurement. The edu domains here are `.edu.pl` or look-alikes, never a
university's.

The same solver can in principle clear 10minutemail's challenge, but on
2026-09-25 every clearance it issued was refused on replay, so
10minutemail still needs an egress Cloudflare does not challenge.

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
| tempr.email | buildable: the inbox streams over Datastar SSE from `mta.trashmailr.com:81`, which needs its own client |
| dropmail | the free API token path closed; the site itself is still up (2026-11-15) |
| yopmail | Turnstile since at least 2026-11-15; the solver's tokens were rejected on replay (same fingerprint wall as 10minutemail) — unreachable from any realistic egress, so its 100+ domains buy nothing |

dropmail was in the seed set only because it was the one known push
(WebSocket) backend, so `watch()` could be proven against a real push provider
before polling adapters were built on it. No shipped provider uses push today.

## Seed set, remaining

| backend | sites | kind | addr | msg | domains |
|---|---|---|---|---|---|
| ⭐ tempr.email | tempr.email | own-domain | ? | 30d | 60 |

Recon corrected the row. tempr.email serves 60 domains, not the "50+" the source list claimed and not
the "50" its own page says in one place, and its 30 days is message retention,
with address lifetime still unestablished.

## Starred, not seeded

Next in line once the interface holds.

| backend | sites | kind | addr | msg | domains |
|---|---|---|---|---|---|
| ⭐ zemail | zemail.me | gmail-alias | forever | 1d | 7 |

zemail is a Laravel Livewire app: a guest `/mailbox` driven by signed
component snapshots. Buildable, but fiddly. Free tier: 20 creations/day,
3 active inboxes.

## Self-hostable

Own the backend, and it cannot rot. These are the floor under the pool.

| backend | sites | source | domains |
|---|---|---|---|
| 🔓 sunls-tmail | mail.sunls.de | `sunls24/tmail` | 3 |
| 🔓 cloudflare_temp_email | mail.awsl.uk (ships as cftempmail 🧩) | `dreamhunter2333/cloudflare_temp_email` | 3 |

The public sunls-tmail instance is walled — Turnstile, 6h cookie per solve —
so it is one for self-hosting, not for tracing. mail.awsl.uk is the public
instance of cloudflare_temp_email and already ships as cftempmail.

## Gmail / Outlook alias backends

Distinct because they produce addresses at a real, non-disposable domain, which
is what defeats domain blocklists.

| backend | sites | addr | msg | domains | verdict (2026-11-15) |
|---|---|---|---|---|---|
| tmail.io | tmail.io | forever | 1d | 4 | walled: Livewire + reCAPTCHA; the solver is Turnstile-only |
| temptom | temptom.com | forever | 1d | 15 | walled: Turnstile; solver candidate |
| ghostinbox | ghostinbox.net, temp-gmail.ghostinbox.net | 1d | 1d | 10 | untraced: temp-gmail.ghostinbox.net answers 404 (dead front door; ghostinbox.net itself alive) — Playwright trace needed |

## Own-domain backends

| backend | sites | addr | msg | domains | verdict (2026-11-15) |
|---|---|---|---|---|---|
| mailnesia | mailnesia.com | forever | 2d | 1 | easy: zero-auth; read proven live on a busy mailbox + RSS endpoint; quirk: common names are aliased and refuse to show mail |
| generator-email 🔗 | generator.email, emailfake.com, tempm.com, mail-temp.com, email-fake.com | ? | ? | 52 | shipped as `generatoremail`; email-fake.com folded in 2026-11-15 (same IP, MX `mx.add5000.com`, same `api-token` schema) |
| trashmail.ws | trashmail.ws | ? | ? | 1 | walled: hCaptcha + reCAPTCHA v3 enforced server-side (Livewire calls 500 regardless); own MX `mail.trashmail.ws`, not a maildrop front door |
| anonymmail 🔗 | anonymmail.net, mail.td | ? | ? | 5 | easy: CodeIgniter JSON API (`/api/create`, `/api/getDomains`, `/api/get`), CSRF from page HTML; live-proven |
| haribu | haribu.net | ? | ? | 1 | medium: session-bound, form-encoded POSTs returning HTML fragments |
| driftz | driftz.net | ? | ? | 37 | easy: React SPA over a public JSON API (api.driftz.net, OpenAPI at /openapi/json); 22 public domains; live-proved generate+list |
| moakt | moakt.com | 1h | 1h | 14 | medium: session-bound (tm_session cookie); create+list proven; public read unproven (busy names reserved, read route unknown) |
| 48hr.email | 48hr.email | forever | 2d | 15 | easy: zero-auth server-rendered; read proven live (list, rendered message, /raw); 15 domains, 48h purge; open source |
| spambox | spambox.xyz | forever | 1d | 4 | walled: reCAPTCHA |
| vmail.dev | vmail.dev | 1d | 1d | 2 | walled (2026-11-15 survey: `/api/verify` demands a live Turnstile token; API keys need dashboard login — supersedes "no longer walled") |
| tempemail.cc | tempemail.cc | forever | forever | 1 | easy: same-origin JSON REST, JWT per account, single domain icmans.com; live-proven |
| mohmal | mohmal.com | 45m | 45m | 6 | quick-medium: connect.sid session on create; /en/extend renews the 45m TTL; 6 domains |
| adguard | adguard.com/adguard-temp-mail | 7d | 1d | 1 | hard: cap.js PoW solvable but /captcha/redeem needs an obfuscated instrumentation blob; not buildable today |
| tmail.link | tmail.link | ? | ? | ? | easy: Django, GET /inbox/<addr>/ opens, POST with csrftoken lists (JSON), public; body page unverified |
| tempmail.cloud | tempmail.cloud | forever* | forever* | 21 | skipped: retention is account-gated; account minting is out of scope (decided 2026-11-15) |
| meowmail.in | meowmail.in | 1h* | 1h* | 4 | easy: public OpenAPI (api.meowmail.in), keyless REST, custom local part, 1h TTL confirmed |

Two rows removed 2026-11-15 as duplicates of shipped adapters:
temporary-mail.net is mailticking, and tempboxpro is a mail.tm skin. A third
fold from the 2026-11-15 survey: email-fake.com is the same backend as the
shipped generator-email family (same IP 91.196.52.205, MX mx.add5000.com, same
api-token schema).

## Ten-minute tier

Short-lived by design. Low value for slow signup flows, fine for fast OTP.

| backend | sites | verdict (2026-11-15) |
|---|---|---|
| muellmail | muellmail.com | untraced: GraphQL answers `UNAUTHENTICATED`; web client untraced |
| linshi | linshi-email.com | walled: CF challenge even via impersonation; IUAM candidate |

## .edu tier

Separate capability kind. Lowest priority; investigate before committing to
any. Verdicts from the 2026-11-15 probe:

- edumailfree.com — untraced-medium: create+list ran through Transport on
  2026-09-24 (captcha off); plain curl drew a Cloudflare challenge on
  2026-11-15, curl_cffi may still pass.
- tempsmail.org — walled: reCAPTCHA v2 server-side.
- edumail.su — walled: IUAM; redirects to edumail.bz.
- run2mail.com — walled: IUAM.
- getedumail.com — walled: login.
- imail.edu.vn — dead: NXDOMAIN.

(freetempmail.com removed 2026-11-15: it is a DuckMail front door and already
sits in the shipped table.)

## From the secondary list

Source: `rentry.org/i3ozxg6f`. cryptogmail.com and
internxt.com/temporary-email removed 2026-11-15: both are mail.tm duplicates,
covered by the shipped mail.tm adapter. email-fake.com folded 2026-11-15: see
the duplicates note under Own-domain backends.

Surveyed 2026-11-15:

- tmailor.com — easy: JSON API mapped (POST /api/webapp-newemail → address + JWT; /api/webapp-listinbox with per-mailbox token map); the Datastar/SSE premise in the old recon is dead — no SSE in its current stack
- anonbox.net — easy: GET /en returns address + secret mbox link; caveats: mbox format, fetched mailboxes are deleted by cron
- tempmail4u.com — quick-medium: one unauthenticated GET (api.tempmail4u.com/api/fakeemails/?email=), catch-all @jsontoexcel.net; ~25s cold latency
- 10minutemail.net — easy: cookie PHP session, address @zefvo.com; generate+list+read proven via its own welcome mail; NOT related to the shipped 10minutemail.com
- tempinbox.xyz — parked until a browser trace: replicated Livewire calls 500
- minmail.app — walled: redirects to temp-mail.app — invite-gated premium product, no anonymous API

Walled: emailme.at (reCAPTCHA) · m.kuku.lu · tempmail.net (all CF-challenged;
IUAM candidates).

Dead: see Dead below.

## Dead

Probed 2026-11-15 and not serving mail. Nine dead, four dead-or-slow —
re-probe before writing any of these off, but do not build against them.

| backend | evidence |
|---|---|
| mintemail 🔗 (mintemail.com, tempail.com) | MX is NXDOMAIN — cannot receive mail at all |
| luxusmail.org | NXDOMAIN |
| tempmailb.com | NXDOMAIN |
| mailtemp.dev | maintenance page |
| fake.legal | maintenance, public API paused |
| fakemailgenerator.com | origin 502 |
| tempmaili | list 404s |
| imail.edu.vn | NXDOMAIN |
| urtempmail.com | serving an unconfigured "Install Page" |
| emailondeck.com | timed out (dead-or-slow) |
| altaddress.org | timed out; TTLs unverified (dead-or-slow) |
| duckspam.com | TLS handshake fails, http 404 (dead-or-slow) |
| mails.org | 503 "Coming Soon" (dead-or-slow) |

## Out of scope

- Telegram bots (`t.me/TempMail_org_bot`, `t.me/reusable`, `t.me/fakemailbot`,
  `t.me/smtpbot`) — a different transport, not an HTTP backend.
- Browser extensions (Bloody Vikings!) — a client, not a service.
- mailsac, mailinator — paid/commercial tiers, different product.

## Counts

86 distinct known backends (names folded by MX/API evidence; liveness probed
2026-11-15; two added from the FMHY temp-mail section the same day): 32
shipped, 2 parked, 52 unbuilt — of the unbuilt, 9 dead, 4 dead-or-slow,
12 walled, 27 untraced. The 32 shipped adapters expose 40
front doors between them, all unique. Two entry-point names differ from their
directory: `tenminutemail/` registers as `10minutemail`, `twentytwodo/` as
`22do`. (Survey of 2026-11-15 pending full recount.)
