# Library

```python
import cowbird
```

All of the library is async. Calls that wait take a `timeout` in seconds and
raise `TimeoutError` when it runs out.

## Getting an address

### `cowbird.inbox(**filters)`

An async context manager that yields an `Inbox` from the best working
provider matching the filters. On exit it deletes the messages it read, where
the provider allows it.

```python
async with cowbird.inbox(kind="gmail-alias") as box:
    ...
```

The filters are the fields of `cowbird.pool.Request`; see
[Providers and routing](../concepts/providers-and-routing.md#filters).
Lifetimes are `datetime.timedelta`s here. Raises `NoProviderAvailable` when no
matching provider can issue one.

### `cowbird.inboxes(n, **filters)`

Like `inbox()`, but yields a list of `n` inboxes spread across different
providers.

## `Inbox`

| Member | Returns | |
|---|---|---|
| `address` | `Address` | The address. `address.value` is the string. |
| `provider` | `Provider` | The provider that issued it. |
| `await otp(timeout=120, pattern=None, poll=None)` | `str` | The first one-time code in new mail. `pattern` is a regex to use instead of the built-in code detection. |
| `await link(timeout=120, match=None, poll=None)` | `str` | The first link in new mail, or the first containing `match`. |
| `await first(extract, timeout=120, poll=None)` | `str` | The first non-empty `extract(message)` over new mail. `otp()` and `link()` are built on it. |
| `watch(poll=None)` | async iterator of `Message` | Every new message, as it arrives. |
| `await messages()` | `list[MessageRow]` | What is in the inbox now. |
| `await get(id)` | `Message` | One message in full. |
| `await delete(id)` | `None` | Delete a message. Raises `NotSupported` where the provider cannot. |
| `await aclose()` | `None` | What the context manager calls on exit. |

`otp()`, `link()` and `first()` remember which messages they answered from, so
calling one twice returns the next match.

## Types

**`Address`**: `value` (the address), `provider` (its name), `expires_at`
(`datetime` or `None` for no expiry), `state` (whatever the provider needs to
open the inbox again; may hold a password, so treat it as a secret).

**`MessageRow`**: `id`, `sender`, `subject`, `received_at`, `locked` (the
provider hides the body behind a paywall).

**`Message`**: `id`, `sender`, `subject`, `received_at`, `html`, `text` (the
body as plain text), `links` (every link in the body).

**`Kind`**: `GMAIL_ALIAS`, `OUTLOOK_ALIAS`, `OWN_DOMAIN`, `EDU`. Plain strings
such as `"gmail-alias"` work too.

**`Capabilities`**: what a provider can do; `provider.caps`. Listed for every
provider in [Providers](providers.md).

## Errors

All in `cowbird.errors`, all subclasses of `CowbirdError`.

| Error | Raised when |
|---|---|
| `NoProviderAvailable` | No provider could issue an address. |
| `NotSupported` | The provider cannot do this call, such as deleting a message. |
| `ProviderDown` | The service is unreachable or failing. `CloudflareChallenge` is a kind of it. |
| `SchemaDrift` | The service answered in a shape the adapter does not understand. Quarantines the provider. |
| `RateLimited` | The service is throttling. |
| `MessageGone` | The message no longer exists. |
| `MessageLocked` | The message body is behind the provider's paywall. |
| `AddressExpired` | The address has expired. |
| `SolverUnavailable` | The Cloudflare solver is set but not answering. |
