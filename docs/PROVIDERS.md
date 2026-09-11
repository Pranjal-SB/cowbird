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

Live, contract-tested, and proven end to end by
`packages/core/tests/test_delivery.py`. P50 is measured, from a real
`cowbird providers` run on 2026-09-07, not declared.

| backend | sites | kind | addr | msg | domains | p50 |
|---|---|---|---|---|---|---|
| mail.tm | mail.tm | own-domain | forever | 7d | 1 | 1.4s |
| emailnator | emailnator.com | gmail-alias | forever | 1d | 6 | 2.3s |
| inboxes | inboxes.com | own-domain | forever | 7d | 18 | 0.0s |

inboxes is a catch-all: `generate()` makes no HTTP call at all, it picks a
local-part and a domain and returns, which is where the 0.0s comes from. It
also accepts a custom local-part. emailnator and mail.tm do not.

## Parked

Reconned, not built, reasons written up. Do not restart either without reading
its recon first.

| backend | why | recon |
|---|---|---|
| smailpro | every call needs a solved Cloudflare Turnstile token in `x-captcha` | `docs/recon/smailpro.md` |
| tempr.email | the message-read endpoint was never observed, only list | `docs/recon/tempr.md` |
| dropmail | the free API token path closed | none |

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
| ⭐ temp-mail.org 🔗 | temp-mail.org, 10minemail.com | own-domain | forever | 2h | ? |
| ⭐ temp-mail.io | temp-mail.io | own-domain | 1d | 1d | 12 |

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
| 22.do | 22.do (also issues outlook/hotmail and own-domain) | 1d | 1d | 3 |
| mailticking | mailticking.com | ? | ? | 2 |
| ghostinbox | ghostinbox.net, temp-gmail.ghostinbox.net | 1d | 1d | 10 |

## Own-domain backends

| backend | sites | addr | msg | domains |
|---|---|---|---|---|
| guerrillamail 🔗 | guerrillamail.com, sharklasers.com, cs.email, dismail.top | 1h | 1h | 11 |
| yopmail | yopmail.com | forever | 8d | 100+ |
| maildrop 🔗 | maildrop.cc, trashmail.ws | forever | 1d | 1 |
| mailnesia | mailnesia.com | forever | 2d | 1 |
| generator-email 🔗 | generator.email, emailfake.com, tempm.com, mail-temp.com | ? | ? | 50+ |
| disposablemail 🔗 | disposablemail.com, fakemail.net | 14d | 14d | 1 |
| mintemail 🔗 | mintemail.com, tempail.com | 1h | 1h | 1 |
| anonymmail 🔗 | anonymmail.net, mail.td | ? | ? | 5 |
| emailondeck 🔗 | emailondeck.com, emailtemp.org, haribu.net, tempmaili.com | ? | ? | 1 |
| altaddress | altaddress.org | forever | 3d | 14 |
| driftz | driftz.net | ? | ? | 23 |
| moakt | moakt.com | 1h | 1h | 13 |
| temporary-mail | temporary-mail.net | forever | ? | 11 |
| 48hr.email | 48hr.email | forever | 2d | 7 |
| temporarymail | temporarymail.com | forever* | ? | 7 |
| tempboxpro | tempboxpro.com | session | ? | 6 |
| mail.cx | mail.cx | 1d | 12h | 5 |
| mails.org | mails.org | ? | 30m | 5 |
| spambox | spambox.xyz | forever | 1d | 4 |
| urtempmail | urtempmail.com | 1d | 1d | 4 |
| nicemail | nicemail.cc (API: web.mailporary.com) | forever | 1d | 6 |
| xeramail | xeramail.com | 1d | 1d | 2 |
| vmail.dev | vmail.dev | 1d | 1d | 2 |
| re146 | mail.re146.dev | 1d | 1h | 2 |
| duckspam | duckspam.com | forever | forever | 1 |
| tempemail.cc | tempemail.cc | forever | forever | 1 |
| reusable.email | reusable.email | forever | forever | 1 |
| mohmal | mohmal.com | 45m | 45m | 1 |
| adguard | adguard.com/adguard-temp-mail | 7d | 1d | 1 |
| tempmailo | tempmailo.com | 2d | 2d | ? |
| tmail.link | tmail.link | ? | ? | ? |

`temporarymail` addresses persist only if used once every 14 days.

## Ten-minute tier

Short-lived by design. Low value for slow signup flows, fine for fast OTP.

| backend | sites |
|---|---|
| muellmail | muellmail.com |
| minuteinbox | minuteinbox.com |
| 10minutemail | 10minutemail.com |
| linshi | linshi-email.com |

## .edu tier

Separate capability kind. Unverified, several likely paid or dead. Lowest
priority; investigate before committing to any.

edumailfree.com · zenvex.dev · tempsmail.org · emailgenerator.org · edumail.su ·
run2mail.com · getedumail.com · mtempmail.com · freetempmail.com · imail.edu.vn ·
vanishinbox.com · instantedumail.com · etempmail.com

## From the secondary list

Not yet classified. Source: `rentry.org/i3ozxg6f`.

inboxkitten.com (🔓 `uilicious/inboxkitten`) · dismail.top · m.kuku.lu ·
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

Roughly 90 front doors. The first full probe (`docs/recon/sweep-2026-09-11-batch-3.md`)
folded four rows into backends already listed: cs.email and dismail.top are
guerrillamail, 10minemail is temp-mail.org, and emailfake is generator-email.
That leaves roughly 55 distinct backends, of which 3 ship.
