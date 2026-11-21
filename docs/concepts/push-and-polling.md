# Push and polling

Waiting for mail means noticing when it arrives. Every provider does this
through `watch()`, which yields each new message once.

## Polling

Almost every provider is polled: cowbird lists the inbox every few seconds and
yields messages it has not seen. Each provider sets its own interval, because
some services answer in a fraction of a second and others take several.

## Push

tempr.email is the one provider that pushes. Its inbox streams new mail over
server-sent events, and its `watch()` follows the stream and reconnects when
it goes quiet.

Its delivery is not proven yet. tempr's mail server refuses the free senders
the live tests use, so no test has seen a message arrive there. Nothing shows
it broken either. dropmail, which pushed over WebSocket, is parked: its free
API key is no longer issued.

To ask only for push providers, use the `push` filter (see
[Providers and routing](providers-and-routing.md)).

## What `wait` does with it

`otp()`, `link()`, `cowbird wait` and `GET .../wait` all sit on `watch()`. They
take messages as they come and return the first one that matches. A message
stays in the inbox after it is read, so cowbird remembers which messages it
already answered from: a second `otp()` waits for the next code instead of
returning the first again.
