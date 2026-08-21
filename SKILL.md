---
name: middle-school-english
description: Query the deterministic 2024 People’s Education Press junior-high English knowledge base and build scope-controlled original practice assessments with canonical evidence, blueprints, validation, and student/teacher outputs. Use when Codex needs to query Grades 7–8 books, check unit or assessment boundaries, create a requested practice blueprint, generate registered item types, or validate an assessment package.
---

# Middle School English

Use only the canonical references and bundled scripts in this skill. Do not read legacy textbook JSON, local maintenance records, audit archives, source documents, sample papers, or external authoring skills at runtime.

## Codex installation

Install this directory at `~/.agents/skills/middle-school-english/` for a user installation or `.agents/skills/middle-school-english/` for a repository installation.

## Runtime routing

1. Read `references/catalog.json`.
2. Confirm that the requested `book_id` appears in `supported_books` with `status: released`.
3. If the book is absent from `supported_books`, state that it is not currently published. Do not infer Grade 9 content or substitute another book.
4. Load only the requested `references/<book_id>.json`. Read `source-manifest.json` only when the user asks about evidence identity or source version.
5. Use `scripts/query_knowledge.py` for deterministic filtering. Supported filters are `--book`, `--unit`, `--domain`, `--level`, `--tag`, and `--assessment-scope`.

## Knowledge boundaries

- Treat A as the core answer threshold.
- Treat B as an application, material, or task point.
- Use C only when the user explicitly sets `reinforcement=true` or names the content.
- Use D only as context; never let it determine an answer.
- Do not create or expose E, uncertain, unresolved, or conflict states in runtime data.
- Do not treat assessment profiles as global duration, score, section, question-order, or answer-card rules.
- Do not add a knowledge point when the canonical references do not provide evidence.

## Assessment workflow

Do not jump from a natural-language request to a finished paper.

1. Parse the request into `assessment-request.schema.json`.
2. Ask only for missing parameters that prevent a unique blueprint: book, units or assessment scope, purpose, total score or complete item plan, item types, and outputs.
3. Run `scripts/build_blueprint.py` and inspect its scope, score arithmetic, A/B coverage, and C/D permissions.
4. Author original items using the relevant rules in `references/authoring/`. Do not copy source questions, passages, answer keys, explanations, model essays, or long textbook text.
5. Write the machine source `assessment.json` first. Every formal item must cite at least one in-scope A/B canonical item ID.
6. Run `scripts/validate_assessment.py`. Rewrite failed items at the failure location and rerun the full validator.
7. Run `scripts/render_assessment.py` from the validated machine source to create the student, teacher, and optional answer-sheet outputs.
8. Check that the student output contains no answers, rationales, canonical IDs, or validation metadata, and that teacher and answer-sheet rows match the machine source.
9. For a printable paper, call `scripts/run_print.py` with the validated `render-request.json`. The wrapper bootstraps and checks the isolated print runtime before running the print and preflight stages. Never invoke the underlying render or preflight scripts directly. Keep the generated bundle and report in a temporary directory.

Listening requests may produce scripts and a blueprint only. Do not claim that an audio file was generated unless the user separately supplies an authorized audio workflow.

## Student output contract

- Treat the student view as a strict whitelist: render only fields required by the resolved blueprint and registered item type. Do not add commentary, layout notes, transition notes, or repeated instructions.
- Keep each item's stem, real blank or parenthetical area, and its options in one contiguous item block. Keep them on one line when they fit; page-width wrapping is allowed, but authors must not insert manual breaks or continuation labels.
- Parenthetical content is optional and must carry real meaning. A requested count is a maximum unless the blueprint explicitly requires an exact count. Omit missing content; `()` and whitespace-only parentheses are invalid, and filler parentheses are forbidden.
- Do not add text such as "continued from above", "see above", "attach to previous line", "fill in each box", or any other text that is not part of the registered item fields.
- For reading matching, render prompts and one shared option set as separate bounded regions. The option set must use a bordered table or grid, and each option label and content must appear once.

## Print runtime

Core queries and standard-library validation do not require the print runtime. Printing does: use `scripts/bootstrap_runtime.py` and `scripts/runtime_doctor.py --print` for explicit setup and diagnosis, or let the required `scripts/run_print.py` wrapper perform both before its print pipeline. Do not install the four print packages into the host environment as a prerequisite for core queries.

## Reference routing

- `references/catalog.json`: published-book index and unit IDs.
- `references/<book_id>.json`: one canonical book at a time.
- `references/authoring/registry.json`: registered item types and required fields.
- `references/authoring/common-rules.md`: shared originality, scope, scoring, and output rules.
- `references/authoring/<item-type>.md`: item-type-specific constraints.
- `schema/`: machine contracts for knowledge, requests, assessments, print IR/manifests, and preflight reports.
- `references/rendering/`: generic A4 print profiles and typography/layout/illustration rules.

Keep the response honest about status. The four cataloged Grade 7–8 books are released logical-content datasets; this does not assert an exact physical printing or relicense underlying textbook rights.
