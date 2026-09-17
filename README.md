# Agency

An open-source platform for building, running, and monitoring multi-step AI
workflows **entirely on your own computer**.

You describe a workflow in a human-readable YAML file. Each step is a node on a
map: an AI agent, a decision that splits the path, a step that fans work out in
parallel, a branch that joins paths back together, a tool that performs a
specific job, or a human who must approve things before they move forward.
Agency walks the map, feeds each step's result into the next, and manages
everything in between — including your GPU memory, so several models can share
one machine without crashing it.

Everything runs locally: the models run on your own hardware, your data never
leaves the machine, and you pay nothing per word.

## Requirements

- **Python** >= 3.10
- **[uv](https://docs.astral.sh/uv/)** for dependency management and running the CLI
- A local model server (e.g. a **llama.cpp** `llama-server`). Agency can
  *auto-start* a server for you if you give it a model file path — see below.
- **Optional:** an NVIDIA GPU + `pynvml` for VRAM monitoring (`--extra gpu`)
- **Optional:** a **Docker** daemon for sandboxed tool execution (the default
  tool environment). Tool-only runs also work in `local` mode without Docker.

## Install

```bash
uv sync --extra dev      # add --extra gpu for NVIDIA VRAM monitoring
```

## Configure your models

Workflow files reference model files by **bare filename** (e.g.
`path: gemma-4-e2b-it-Q8_0.gguf`) so the repo is clone-and-run. Agency resolves
each filename against an ordered list of directories and, when the declared
endpoint is unreachable, auto-starts a local llama.cpp server from the resolved
file.

Copy the env template and point it at your model directory:

```bash
cp .env.example .env
# edit .env:
#   AGENCY_MODELS_DIRS=~/.llama-cpp/models
#   LLAMA_CPP_SERVER_PATH=llama-server
```

- `AGENCY_MODELS_DIRS` — colon-separated, **ordered** list of directories to
  search (element 0 is primary). Default: `~/.llama-cpp/models`. Put your
  `.gguf` files in any listed directory.
- `LLAMA_CPP_SERVER_PATH` — the llama.cpp server binary used for auto-start
  (default `llama-server`, resolved on `PATH`).

Settings are read from a local `.env` (gitignored) when you start the CLI. Real
environment variables always take precedence over `.env`.

## Run a workflow

```bash
uv run agency workflows/default.yaml
```

Useful subcommands:

```bash
uv run agency run <workflow.yaml> [--models FILE] [--environment sandbox|local]
uv run agency replay <workflow.yaml> --checkpoint NODE [--run RUN_ID]
uv run agency serve [--host 127.0.0.1] [--port 8000] [--log-dir runs]
uv run agency --tui --demo      # live TUI with a simulated run
```

A human-in-the-loop (`human_in_loop`) node pauses the run and prompts for input
before continuing. Headless runs exit `0` on completion and `1` on failure.

## HTTP API + Flutter client (Recommended)

`agency serve` exposes a REST + WebSocket API. The companion Flutter app
(`agency_app/`) talks to it over REST plus a read-only `/events` stream and
renders the workflow as a live canvas:

```bash
uv run agency serve                 # start the API server
cd agency_app && flutter run        # then run the client
# or:
# cd agency_app && flutter run -d Linux # run the client as native desktop app
```

See `workflow.md` for a catalog of the shipped example workflows and
`AGENTS.md` for a full architectural deep-dive.

## Development

```bash
uv sync --extra dev
uv run pytest              # run the test suite
uv run ruff check src/ tests/
```
