# Installation

cowbird is not on PyPI yet, so you run it from a clone of the repository.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Linux or macOS. Docker, if you want the server in a container.

## Install

```bash
git clone https://github.com/Pranjal-SB/cowbird.git
cd cowbird
uv sync --all-packages --all-extras
```

This installs the library, the `cowbird` command, the HTTP server and every
provider into the project's virtual environment. Run commands through
`uv run`, as in `uv run cowbird providers`.

## Check it works

```bash
uv run cowbird providers
```

This prints one line per provider with its status. Status comes from calls
cowbird has actually made, so on a fresh install most providers show `ok` with
no latency yet. Get one address to prove the whole path:

```bash
uv run cowbird new
```

It prints the address, the provider that issued it, and how long the address
lives.

## Optional extras

- **Cloudflare solver.** Six providers sit behind Cloudflare Turnstile and are
  skipped until you point cowbird at a solver. See
  [Use a Cloudflare solver](../guides/cloudflare-solver.md).
- **The HTTP server.** See [Run the server](../guides/run-the-server.md).

## Next

[Your first inbox](first-inbox.md).
