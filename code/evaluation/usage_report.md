# Usage report

Final full-dataset run that produced `output.csv` (518 requests).

Cached replays count the tokens of the original live call, so these figures are the real cost of producing the answers.

| Provider | Model | Purpose | Calls | Cache replays | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|
| Google | gemini-2.5-flash | media_extraction | 8 | 8 | 7090 | 27480 | 0.0708 |
| Google | gemini-3.5-flash | media_extraction | 6 | 6 | 10034 | 24279 | 0.0637 |
| Google | gemini-flash-latest | intent_classification | 10 | 9 | 17252 | 15735 | 0.0445 |
| Google | gemini-flash-latest | media_extraction | 1 | 1 | 1128 | 2722 | 0.0071 |
| Google | **gemini-2.5-flash total** | all | 8 | 8 | 7090 | 27480 | 0.0708 |
| Google | **gemini-3.5-flash total** | all | 6 | 6 | 10034 | 24279 | 0.0637 |
| Google | **gemini-flash-latest total** | all | 11 | 10 | 18380 | 18457 | 0.0517 |
| **Overall** | all models | all | 25 | 24 | 35504 | 70216 | 0.1862 |

- Total tokens: 105720
- Average tokens per request: 204.1
- Estimated total cost: $0.1862
- Estimated cost per request: $0.000359
- Pricing assumption: $0.30 / 1M input, $2.50 / 1M output (thinking tokens billed as output). Same flash-tier list price applied to every model used.
- Model fallback: calls start on gemini-2.5-flash; on a daily-quota error they move to the next model in ['gemini-2.5-flash', 'gemini-3.5-flash', 'gemini-flash-latest', 'gemini-2.5-pro'].
