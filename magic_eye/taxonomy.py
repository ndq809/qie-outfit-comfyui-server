"""Label maps, hierarchy matrices and score look-ups for the wardrobe classifier.

Inference-only subset of MS_Model_Magic_Eye's magic_eye/taxonomy.py. TaxonomyInfo
below is a verbatim copy of the dataclass there - the model code vendored alongside
it (model.py, model_phase2.py, model_phase3.py) sizes every head off these maps, so
the fields and their order have to match the trained checkpoints exactly.

What is NOT copied is how upstream *builds* one: it streams a 394 MB anchor_items.json
(257k items, ~30s per startup) and reads auxiliary scores out of an Excel workbook,
neither of which is committable here, and neither of which inference needs - the
result is only these maps and matrices. scripts/export_magic_eye_bundle.py runs that
build once, against the upstream project, and writes the outcome to
models/magic_eye/taxonomy.json (~40 KB); load_taxonomy() below reads it back.

So: retraining upstream means re-running the export script, not editing this file.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import json
import torch


@dataclass
class TaxonomyInfo:
    """Holds every label map, hierarchy matrix, and score lookup."""

    # value → index
    gender_map: Dict[str, int] = field(default_factory=dict)
    category_map: Dict[str, int] = field(default_factory=dict)
    sub_category_map: Dict[str, int] = field(default_factory=dict)
    type_map: Dict[str, int] = field(default_factory=dict)
    color_map: Dict[str, int] = field(default_factory=dict)
    neck_map: Dict[str, int] = field(default_factory=dict)
    sleeve_map: Dict[str, int] = field(default_factory=dict)
    pattern_map: Dict[str, int] = field(default_factory=dict)

    # hierarchy
    cat_to_sub_matrix: Optional[torch.Tensor] = None   # [num_cat, num_sub]
    sub_to_type_matrix: Optional[torch.Tensor] = None  # [num_sub, num_type]

    # score look-ups (value → score(s))
    type_scores: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    neck_scores: Dict[str, float] = field(default_factory=dict)
    sleeve_scores: Dict[str, float] = field(default_factory=dict)
    color_scores: Dict[str, float] = field(default_factory=dict)

    # convenience counts
    @property
    def num_genders(self) -> int:
        return len(self.gender_map)

    @property
    def num_categories(self) -> int:
        return len(self.category_map)

    @property
    def num_sub_categories(self) -> int:
        return len(self.sub_category_map)

    @property
    def num_types(self) -> int:
        return len(self.type_map)

    @property
    def num_colors(self) -> int:
        return len(self.color_map)

    @property
    def num_necks(self) -> int:
        return len(self.neck_map)

    @property
    def num_sleeves(self) -> int:
        return len(self.sleeve_map)

    @property
    def num_patterns(self) -> int:
        return len(self.pattern_map)

    def summary(self) -> str:
        return (f"gender={self.num_genders}  category={self.num_categories}  "
                f"sub_category={self.num_sub_categories}  type={self.num_types}  "
                f"color={self.num_colors}  neck={self.num_necks}  "
                f"sleeve={self.num_sleeves}  pattern={self.num_patterns}")


def load_taxonomy(path) -> TaxonomyInfo:
    """Read the taxonomy exported by scripts/export_magic_eye_bundle.py."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return TaxonomyInfo(
        gender_map=data["gender_map"],
        category_map=data["category_map"],
        sub_category_map=data["sub_category_map"],
        type_map=data["type_map"],
        color_map=data["color_map"],
        neck_map=data["neck_map"],
        sleeve_map=data["sleeve_map"],
        pattern_map=data["pattern_map"],
        cat_to_sub_matrix=torch.tensor(data["cat_to_sub_matrix"], dtype=torch.float32),
        sub_to_type_matrix=torch.tensor(data["sub_to_type_matrix"], dtype=torch.float32),
        type_scores={k: tuple(v) for k, v in data["type_scores"].items()},
        neck_scores=data["neck_scores"],
        sleeve_scores=data["sleeve_scores"],
        color_scores=data["color_scores"],
    )
