# Reading matching

Matching content has three distinct measured candidates: `card-grid`, `stacked`, and `dual-independent-flow`. Each candidate is measured with the resolved font and reports page count, breaks, hard violations, empty ratio, and isolated blocks. Selection is deterministic by hard violations, page count, empty ratio, isolated count, break count, and profile tie-break order. Prompts and options remain complete and ordered across page breaks.

The selected student-facing matching layout must have a visible boundary: use a real frame, bordered cards, or a bordered grid/table that clearly separates the prompt area from the option area. Semantic bounding boxes alone do not satisfy this requirement. The boundary must survive pagination and must not be replaced by whitespace or an explanatory label.

Each option is emitted exactly once in the matching item. Do not repeat the option list beside every prompt, add per-cell copies of the same option, or add labels such as `continued`, `see above`, or `attach to previous line` to compensate for a wrap or page break. Natural line wrapping is allowed; invented layout text is not.

Prompts and their response marks remain bound to the prompt they answer. A page break may occur only between complete rows/cards or complete prompt units; it must not detach a response mark from its prompt or create an unlabeled option cell.
