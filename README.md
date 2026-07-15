# Diffi

API response comparison desktop app — detect differences between old and new API responses.

Built with PySide6 and Python.

## Usage

```bash
uv run diffi
```

Or directly:

```bash
uv run python main.py
```

## Features

- Compare old vs new API responses across multiple IDs
- Single API mode to inspect responses
- HTTP method, headers, query params, and body configuration
- Field mapping to handle renamed fields between API versions
- Summary report with per-ID breakdown
