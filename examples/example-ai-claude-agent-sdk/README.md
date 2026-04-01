# Claude Agent SDK + PostHog AI Examples

Track Claude Agent SDK calls with PostHog.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

## Examples

- **simple_query.py** - Single query using the `query()` drop-in replacement
- **instrument_reuse.py** - Configure-once with `instrument()`, reuse across multiple queries

## Run

```bash
source .env
python simple_query.py
python instrument_reuse.py
```
