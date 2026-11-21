# Providers and routing

A provider is one free email service, such as mail.tm, Guerrilla Mail or
emailnator, wrapped in an adapter that speaks its API. cowbird ships 44 of
them; [Providers](../reference/providers.md) lists each one with what it can
do.

You rarely pick one yourself. You say what you need, and cowbird picks.

## Kinds of address

- `own-domain`: an address on the provider's own domain, such as
  `x@mail.tm`. The most common kind, and the easiest for a site to block.
- `gmail-alias` and `outlook-alias`: a variant of a real Gmail or Outlook
  mailbox (dots, `+tags`, `googlemail.com`). Sites that block throwaway domains
  usually accept these.
- `edu`: an address on an education domain.

## Filters

Every way of asking for an address takes the same filters: keyword arguments
to `cowbird.inbox()`, the JSON body of `POST /v1/inboxes`, and `--gmail` or
`--provider` on the CLI.

| Filter | Meaning |
|---|---|
| `provider` | Use exactly this provider. No fallback. |
| `kind` | One of the kinds above. |
| `local` | The part before the `@`, for providers that let you choose it. |
| `domain` | Exactly this domain. |
| `domain_not_in` | Never these domains. Use it for domains a site is known to block. |
| `address_ttl` | The address must live at least this long. HTTP: `address_ttl_seconds`. |
| `message_ttl` | Messages must be kept at least this long. HTTP: `message_ttl_seconds`. |
| `push` | Only providers that push new mail instead of being polled. |
| `delete` | Only providers that can delete messages. |
| `self_hosted` | Only open-source backends you could run yourself. |
| `inbox_privacy` | The most exposed inbox you accept: `private`, `public` or `shared`. |

When no provider matches, the request fails with `NoProviderAvailable`
(HTTP 503).

## How a provider is picked

Of the providers that match the filters, cowbird drops those that are `down`
or `quarantined` (see [Health and quarantine](health-and-quarantine.md)) and
those that need a Cloudflare solver when none is set. It then ranks the rest:

1. healthy (`ok`) before `slow`, and a `down` provider due its one retry last;
2. private inboxes before public ones, and public before shared;
3. faster before slower, by measured median latency;
4. more domains before fewer, so addresses are harder to block.

cowbird tries the first. If that provider fails, it moves to the next, until
one issues an address or none are left (`NoProviderAvailable`, HTTP 503).

`cowbird.inboxes(n)` spreads `n` addresses across different providers, so one
provider failing costs one address, not all of them.

## Inbox privacy

Many free services let anyone read any inbox if they know the address. Those
are `public`. Use them for one-time codes, not for anything that should stay
private. [Providers](../reference/providers.md) shows each provider's privacy.
