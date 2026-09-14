from cowbird.errors import (
    CloudflareChallenge,
    MessageLocked,
    NotSupported,
    ProviderDown,
    RateLimited,
    SchemaDrift,
)


def test_transient_failures_are_reroutable():
    for exc in (ProviderDown, CloudflareChallenge, RateLimited):
        assert exc("x").reroutable is True


def test_schema_drift_is_reroutable_too():
    # Constructed separately: SchemaDrift takes three arguments, not a message.
    assert SchemaDrift("p", expected="a", got="b").reroutable is True


def test_caller_facing_failures_are_not_reroutable():
    # Rerouting these would hide a real answer: another provider cannot un-lock
    # this message or grow the capability the caller asked for.
    for exc in (MessageLocked, NotSupported):
        assert exc("x").reroutable is False


def test_cloudflare_challenge_is_a_kind_of_provider_down():
    assert issubclass(CloudflareChallenge, ProviderDown)


def test_schema_drift_carries_what_it_expected():
    err = SchemaDrift("mailtm", expected="hydra:member", got=["items", "total"])
    assert "hydra:member" in str(err)
    assert "items" in str(err)


def test_message_gone_is_a_provider_error_that_does_not_reroute():
    from cowbird.errors import MessageGone, ProviderError

    err = MessageGone("inboxkitten: message us-west1:abc expired upstream")
    assert isinstance(err, ProviderError)
    assert err.reroutable is False
