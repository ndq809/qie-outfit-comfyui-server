"""
MagicEyeClassifier — multi-task classification model with hierarchy masking.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from .taxonomy import TaxonomyInfo


class ClassificationHead(nn.Module):
    """Two-layer MLP head: Linear → BN → ReLU → Dropout → Linear."""

    def __init__(self, in_dim: int, hidden: int, n_cls: int, dropout: float = 0.3):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.BatchNorm1d(hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, n_cls),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class AuxRegressionHead(nn.Module):
    """Small regression head for auxiliary score prediction."""

    def __init__(self, in_dim: int, hidden: int, out_dim: int = 1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class MagicEyeClassifier(nn.Module):
    """
    Multi-task classifier with:
      • 4 single-label heads (gender, category, sub_category, type)
      • 4 multi-label heads  (color, neck, sleeve, pattern)
      • Hierarchy soft-masking (category → sub_category → type)
      • 4 auxiliary regression heads (type scores, coverage, dirt)
    """

    def __init__(
        self,
        taxonomy: TaxonomyInfo,
        embedding_dim: int = 768,
        hidden_dim: int = 512,
        hard_head_hidden_dim: int = 768,
        dropout: float = 0.3,
    ):
        super().__init__()
        d = embedding_dim
        h = hidden_dim
        h_hard = hard_head_hidden_dim
        p = dropout
        h_aux = hidden_dim // 2

        # Single-label heads
        self.gender_head = ClassificationHead(d, h, taxonomy.num_genders, p)
        self.category_head = ClassificationHead(d, h, taxonomy.num_categories, p)
        self.sub_category_head = ClassificationHead(d, h_hard, taxonomy.num_sub_categories, p)
        self.type_head = ClassificationHead(d, h_hard, taxonomy.num_types, p)

        # Multi-label heads
        self.color_head = ClassificationHead(d, h, taxonomy.num_colors, p)
        self.neck_head = ClassificationHead(d, h, taxonomy.num_necks, p)
        self.sleeve_head = ClassificationHead(d, h, taxonomy.num_sleeves, p)
        self.pattern_head = ClassificationHead(d, h, taxonomy.num_patterns, p)

        # Hierarchy buffers (not learnable parameters)
        self.register_buffer("cat_to_sub", taxonomy.cat_to_sub_matrix)
        self.register_buffer("sub_to_type", taxonomy.sub_to_type_matrix)

        # Auxiliary regression heads
        self.type_score_head = AuxRegressionHead(d, h_aux, 2)
        self.neck_score_head = AuxRegressionHead(d, h_aux, 1)
        self.sleeve_score_head = AuxRegressionHead(d, h_aux, 1)
        self.color_score_head = AuxRegressionHead(d, h_aux, 1)

    # ------------------------------------------------------------------
    def forward(self, x: torch.Tensor) -> dict:
        """
        Args:
            x: [B, 768] visual embedding
        Returns:
            dict of logits / predictions
        """
        # --- classification ---
        gender_logits = self.gender_head(x)
        category_logits = self.category_head(x)

        # Sub-category with hierarchy soft mask
        sub_logits_raw = self.sub_category_head(x)
        cat_probs = F.softmax(category_logits, dim=-1)          # [B, n_cat]
        sub_mask = cat_probs @ self.cat_to_sub + 1e-6            # [B, n_sub]
        sub_category_logits = sub_logits_raw * sub_mask

        # Type with hierarchy soft mask
        type_logits_raw = self.type_head(x)
        sub_probs = F.softmax(sub_category_logits, dim=-1)       # [B, n_sub]
        type_mask = sub_probs @ self.sub_to_type + 1e-6          # [B, n_type]
        type_logits = type_logits_raw * type_mask

        # Multi-label
        color_logits = self.color_head(x)
        neck_logits = self.neck_head(x)
        sleeve_logits = self.sleeve_head(x)
        pattern_logits = self.pattern_head(x)

        # --- auxiliary regression ---
        type_scores = self.type_score_head(x)                    # [B, 2]
        neck_score = self.neck_score_head(x).squeeze(-1)         # [B]
        sleeve_score = self.sleeve_score_head(x).squeeze(-1)     # [B]
        color_score = self.color_score_head(x).squeeze(-1)       # [B]

        return {
            # classification logits
            "gender": gender_logits,
            "category": category_logits,
            "sub_category": sub_category_logits,
            "type": type_logits,
            "color": color_logits,
            "neck": neck_logits,
            "sleeve": sleeve_logits,
            "pattern": pattern_logits,
            # auxiliary
            "type_scores": type_scores,
            "neck_score": neck_score,
            "sleeve_score": sleeve_score,
            "color_score": color_score,
        }
