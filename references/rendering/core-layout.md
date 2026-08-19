# Core layout

The print pipeline consumes semantic Render IR blocks and flows them with final-font measurements. Blocks are the smallest keep-together units; page breaks may occur only at declared paragraph, task, prompt, or option boundaries. Non-response whitespace above 15% of a usable region, orphaned semantic blocks, overflow, and overlap are hard errors.

Student and teacher documents are projections of one canonical item order. The renderer records block role, source item, page, and bbox in the render manifest; preflight verifies those records against the parsed PDF.
