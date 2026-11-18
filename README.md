# Multibagger Hunter

A Python stock scanner with a Streamlit website. The goal is to let users pick
their own investment strategies, including finding potentially undervalued
stocks with 3–5× upside.

**Status:** early skeleton. The site runs, but the scanner is not built yet.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. At https://share.streamlit.io click **New app**, pick the repo, and set the
   main file to `app.py`.

## Files

- `app.py` — the website
- `requirements.txt` — Python packages to install
- `README.md` — this description
