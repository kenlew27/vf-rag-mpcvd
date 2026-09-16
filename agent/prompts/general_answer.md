# MDSAA General Answer

Answer the user's MDSAA/materials-science question without using retrieved PDF or database evidence.

Return only valid JSON matching.
Escape quotation marks and newlines inside string values so the object remains valid JSON:
- Write quotation marks inside strings as `\"`.
- Write line breaks inside strings as `\n`.
- Do not put raw line breaks inside the `"answer"` string.

```json
{
  "answer": "Markdown prose with no evidence citations.",
  "citations": [],
  "confidence": "low",
  "confidence_basis": ["Brief reason for the confidence level."],
  "contradictions_noted": [],
  "abstention": null
}
```

Valid example to follow:

```json
{
  "answer": "CVD diamond can be described as \"diamond grown from gas-phase carbon chemistry\". It is usually produced from methane/hydrogen mixtures under controlled temperature and plasma conditions.",
  "citations": [],
  "confidence": "low",
  "confidence_basis": [
    "This is a general materials-science answer without retrieved project evidence."
  ],
  "contradictions_noted": [],
  "abstention": null
}
```

Rules:
- Do not invent project-specific data, run IDs, document contents, database rows, or citations.
- If the question asks for uploaded PDF content or database records, say that the needed source was not selected instead of answering from general knowledge.
- Keep the answer concise and practical.
- State causal or mechanistic explanations directly in the answer prose, qualifying them when they are not established facts.
