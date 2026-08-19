# Authoring contract

1. Parse the user request into `assessment-request.schema.json`; ask only for missing book, scope, purpose, item plan, score, or output parameters that prevent a unique blueprint. Do not infer a local sample-paper profile.
2. Resolve the blueprint before writing items. The registered item types in `registry.json` are a closed set; an unregistered type cannot enter an assessment.
3. Keep every item inside the requested published book and resolved units. Cite at least one in-scope A or B canonical item as a primary target. C is allowed only when `reinforcement=true` or the user names it; D may supply context but never determine an answer. Never introduce E or an unresolved knowledge point.
4. Write original stems, options, passages, prompts, explanations, rubrics, and model responses. Do not reproduce source questions, passages, answer keys, explanations, model essays, or long textbook wording. Canonical IDs are evidence pointers, not text to copy.
5. Make each objective answer uniquely supported by the item or passage. For open responses, define observable scoring points and accepted-answer rules. The rationale must prove the intended answer and reject alternatives without relying on unstated outside facts.
6. Keep `score` arithmetic exact: item scores must match the blueprint and total score. Avoid repeated micro-points, answer-position patterns, clue leakage, and options that are simultaneously correct.
7. Treat difficulty as an estimate of content and cognitive load unless a separate lawful student-data calibration exists. Do not present an estimate as psychometric measurement.
8. Generate the machine source first: `assessment.json` → full validation → targeted rewrites → student/teacher/answer-sheet views. The student view must omit answers, rationales, canonical IDs, and validation metadata; all views must derive from the same item IDs and scores.
9. A `listening_blueprint` produces an original script/structure and scoring plan only. It must not claim that audio was generated or available.
10. The package is standalone: use only these bundled references and schemas, with no external authoring-skill lookup, installation, or fallback.
