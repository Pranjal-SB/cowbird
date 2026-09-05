# Provider inventory

The backlog. Every known disposable-mail backend, its front doors, and what it
can do. Source: curated list, 2026-09-05.

**A backend is the unit, not a URL.** Several of these serve the same service
behind different domains and skins; one adapter covers all of them.

`addr` = how long the address lives. `msg` = how long a received message is
retained. `?` = not established yet.

Legend: ⭐ seed set · 🔓 open source, self-hostable · 🔗 shares a backend with
its listed aliases

## Seed set

The six that the interface must survive before any of the rest are written.

| backend | sites | kind | addr | msg | domains |
|---|---|---|---|---|---|
| ⭐ mail.tm | mail.tm | own-domain | forever | 7d | 1 |
| ⭐ emailnator | emailnator.com | gmail-alias | forever | 1d | 6 |
| ⭐ smailpro | smailpro.com | gmail-alias, outlook-alias | ? | ? | 30+ |
| ⭐ tempr.email | tempr.email | own-domain | forever | 1mo | 50+ |
| ⭐ inboxes | inboxes.com | own-domain | forever | 7d | 19 |
| dropmail | dropmail.me | own-domain | ? | ? | 17 |

mail.tm and dropmail are documented APIs; the other four are reversed.
dropmail is not starred but is in the seed set because it is the only known
push (WebSocket) backend, and `watch()` must be proven against a real push
provider before 50 polling adapters are built on top of it.

## Starred, not seeded

Next in line once the interface holds.

| backend | sites | kind | addr | msg | domains |
|---|---|---|---|---|---|
| ⭐ zemail | zemail.me | gmail-alias | forever | 1d | 7 |
| ⭐ temp-mail.org | temp-mail.org | own-domain | forever | 2h | ? |
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
| 22.do | 22.do | 1d | 1d | 3 |
| mailticking | mailticking.com | ? | ? | 2 |
| ghostinbox | ghostinbox.net, temp-gmail.ghostinbox.net | 1d | 1d | 10 |

## Own-domain backends

| backend | sites | addr | msg | domains |
|---|---|---|---|---|
| guerrillamail 🔗 | guerrillamail.com, sharklasers.com | forever | 1h | 11 |
| yopmail | yopmail.com | forever | 8d | 100+ |
| maildrop 🔗 | maildrop.cc, trashmail.ws | forever | 1d | 1 |
| mailnesia | mailnesia.com | forever | 2d | 1 |
| generator-email 🔗 | generator.email, email-fake.com, tempm.com | ? | ? | 50+ |
| emailfake 🔗 | mail-temp.com, emailfake.com | ? | ? | 50+ |
| disposablemail 🔗 | disposablemail.com, fakemail.net | 14d | 14d | 1 |
| mintemail 🔗 | mintemail.com, tempail.com | 1h | 1h | 1 |
| anonymmail 🔗 | anonymmail.net, mail.td | ? | ? | 5 |
| emailondeck 🔗 | emailondeck.com, emailtemp.org, haribu.net, tempmaili.com | ? | ? | 1 |
| altaddress | altaddress.org | forever | 3d | 14 |
| driftz | driftz.net | ? | ? | 23 |
| moakt | moakt.com | 1h | 1h | 13 |
| cs.email | cs.email | forever | 1h | 12 |
| temporary-mail | temporary-mail.net | forever | ? | 11 |
| 48hr.email | 48hr.email | forever | 2d | 7 |
| temporarymail | temporarymail.com | forever* | ? | 7 |
| tempboxpro | tempboxpro.com | session | ? | 6 |
| mail.cx | mail.cx | 1d | 12h | 5 |
| mails.org | mails.org | ? | 30m | 5 |
| spambox | spambox.xyz | forever | 1d | 4 |
| urtempmail | urtempmail.com | 1d | 1d | 4 |
| nicemail | nicemail.cc | forever | 1d | 3 |
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
| 10minemail | 10minemail.com |
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

Roughly 90 front doors, roughly 60 distinct backends after alias dedup, of
which 6 are seeded, 3 more are starred, 3 are self-hostable, and 13 are the
unverified .edu tier.
