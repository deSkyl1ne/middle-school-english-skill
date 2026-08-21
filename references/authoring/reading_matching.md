# Reading matching

- Write an original `passage`, parallel `prompts`, and a clearly typed option set.
- Give every prompt exactly one `answer.matches` entry. Each match must be supported by a distinct passage cue; unused options should be plausible but rejectable.
- Keep prompt and option categories parallel, avoid duplicate cues, and explain every match in `rationale`.
- Render prompts and the one shared option set as separate bounded regions. Put the option set in a bordered table or grid; do not render it as an unbounded paragraph.
- Render each option label and its content exactly once. Do not repeat a selection instruction in every cell or prompt, and do not add continuation text such as "continued from above", "see above", or "attach to previous line".
