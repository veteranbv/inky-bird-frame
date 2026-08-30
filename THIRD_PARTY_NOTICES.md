# Third-party notices

Inky Bird Frame is licensed under the [MIT License](LICENSE). The controller
container also redistributes these command-line tools:

| Component | License | Source |
| --- | --- | --- |
| [OpenAI Codex CLI](https://github.com/openai/codex) | Apache License 2.0 | The version is pinned by `CODEX_VERSION` in [`Dockerfile`](Dockerfile). |
| [GitHub CLI](https://github.com/cli/cli) | MIT License | The version is pinned by `GH_VERSION` in [`Dockerfile`](Dockerfile). |

The container stores the exact license material distributed with those pinned
versions under `/usr/share/licenses/inky-bird-frame/third-party/`. The build
verifies each file before it copies the notices into the runtime image.

Installed Python and Debian packages retain the license and copyright material
provided by their package formats.
