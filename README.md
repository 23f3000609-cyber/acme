# Acme Appliances Ledger Agent (no LLM API key required)

This FastAPI service exposes `POST /` with JSON `{"question":"..."}` and returns `{"answer": ...}`.

## 1. Deploy on Render

1. Create a GitHub repository and upload `main.py`, `requirements.txt`, and `render.yaml`.
2. In Render, create a **Web Service** connected to that repository.
3. Set the start command to `uvicorn main:app --host 0.0.0.0 --port $PORT` (or let `render.yaml` configure it).
4. Add an environment variable named `LEDGER_URL`. Its value must be the full `questionData` URL from your assignment, including your `email`, `quizSign`, and `questionId` query parameters. Use the initial URL that opens the API's root instructions (`path=/`). Keep this value out of your public GitHub repository.
5. Deploy and wait until `GET /` returns `"ready": true`.

## 2. Test

Replace `https://YOUR-SERVICE.onrender.com` with your Render URL:

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/ \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"What was the total revenue in USD from the North region in March 2026?\"}"
```

Expected response shape:
```json
{"answer": 1234.56}
```
The number above is only an example of the response format, not the actual answer.

## Notes

- The service tries `/export` first, including newline-delimited JSON, then falls back to paginating `/orders`.
- It deduplicates by order ID, keeping the row with the latest `updated_at`.
- Business dates are interpreted in Asia/Kolkata.
- Revenue counts paid orders; refund questions use refunded orders.
- This is a no-LLM rule-based parser. It covers common revenue/refund/count/ranking questions, but truly arbitrary natural language can require more advanced parsing.
