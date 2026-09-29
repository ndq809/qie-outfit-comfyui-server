"""Text description of a garment from its D3 tags. Its own module, free of torch, so
data-server can rebuild the description when the user edits tags during review
without loading the classifier stack."""


def build_description(type_, gender, category, sub_category, color, neck, sleeve, pattern) -> str:
    """Concise natural-language description in the style of the wardrobe corpus,
    e.g. "short sleeve printed t-shirts in black and white. crewneck collar."."""
    parts = []
    if sleeve:
        parts.append(" ".join(sleeve))
    if pattern:
        parts.append(" ".join(pattern))
    parts.append(type_)
    color_names = [c["color"] if isinstance(c, dict) else c for c in color]
    if len(color_names) == 1:
        parts.append(f"in {color_names[0]}")
    elif len(color_names) == 2:
        parts.append(f"in {color_names[0]} and {color_names[1]}")
    elif color_names:
        parts.append("in " + ", ".join(color_names[:-1]) + f", and {color_names[-1]}")

    sentences = [" ".join(parts) + "."]
    if neck:
        sentences.append(" ".join(neck) + ".")
    sentences.append(f"{gender}'s {category}.")
    return " ".join(sentences)
