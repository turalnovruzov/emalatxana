# Contributing

emx is a personal tool, shared in case it is useful to you. It is built by Claude Code agents with its owner as product owner (see [`CLAUDE.md`](CLAUDE.md)), so outside code changes do not fit the way it runs.

- **Bugs and ideas:** open an issue. Say what you did, what you expected and what happened, with your OS and your tmux, Python and Claude Code versions.
- **Pull requests:** open an issue and ask first. A pull request nobody asked for may be closed.
- **Forks** are welcome under the [MIT licence](LICENSE).

## Running the tests

```sh
python3 -m unittest discover -s tests -v
```

The stack is tmux plus the Python standard library ([ADR 0001](docs/adr/0001-tmux-and-python-standard-library.md)), so the tests need nothing installed. When you try emx by hand next to a workspace you already run, give it a tmux socket of its own with `EMX_SOCKET`.
