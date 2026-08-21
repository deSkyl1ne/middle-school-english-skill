# Illustrations

Assessment content references only registered asset IDs. The asset manifest owns the bundle-relative file, rights, and linked item IDs. `original-grayscale` assets must be parseable, bound, embedded in the PDF, and placed at the configured DPI; missing, unknown-rights, low-DPI, or low-contrast assets fail preflight. `none` still requires a valid empty manifest.

Any item that requires visual identification, ordering, comparison, or description must bind a real, verifiable asset to the relevant item and placement. The asset must be present in the bundle and usable by the student; a text reference such as `see the picture` without an embedded asset is invalid. If the item does not actually need visual evidence, do not add an image merely to fill a layout slot.

The following are prohibited: stick figures, blank or placeholder images, unrelated decorative images, image literals or missing-image markers, and images with an obvious fabricated-AI look or materially incorrect objects, text, anatomy, perspective, or scene details. An image generated only to satisfy the presence of a picture is also invalid.

Prefer real images with appropriate rights, or clear, natural textbook-style instructional artwork with verifiable provenance. Asset quality is part of correctness, not decoration: the image must directly expose the information required by the item and remain legible at print size.

If no qualified, authorized, relevant asset is available, do not silently substitute a weak image. Change the item to a non-visual question type, or fail the item/render request explicitly with a missing-qualified-asset error. Do not use `see the picture`, a placeholder, or an explanatory note as a fallback.
