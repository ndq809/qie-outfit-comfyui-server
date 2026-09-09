"""
TextEmbeddingGenerator — maps visual embeddings + attribute labels to
text embeddings (768-dim) in SigLIP's shared embedding space.

Architecture:
  • Learnable embedding tables for each of 8 attribute heads
  • Single-label: direct lookup
  • Multi-label: mean-pooling of active label embeddings
  • MLP: concat(visual_emb, attr_embs) → hidden layers → 768-dim output → L2 norm
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from .taxonomy import TaxonomyInfo


class TextEmbeddingGenerator(nn.Module):
    """
    Generates text embeddings from visual features + classification attributes.

    During training: uses ground-truth labels as attribute input.
    During inference: uses predicted labels from Phase 2 classifier.
    """

    def __init__(
        self,
        taxonomy: TaxonomyInfo,
        embedding_dim: int = 768,
        attr_dim: int = 64,
        hidden_dim: int = 1024,
        num_hidden_layers: int = 3,
        dropout: float = 0.1,
    ):
        super().__init__()
        self.attr_dim = attr_dim

        # --- Attribute embedding tables ---
        # Single-label tasks
        self.gender_emb = nn.Embedding(taxonomy.num_genders, attr_dim)
        self.category_emb = nn.Embedding(taxonomy.num_categories, attr_dim)
        self.sub_category_emb = nn.Embedding(taxonomy.num_sub_categories, attr_dim)
        self.type_emb = nn.Embedding(taxonomy.num_types, attr_dim)

        # Multi-label tasks (one embedding per class, mean-pooled at runtime)
        self.color_emb = nn.Embedding(taxonomy.num_colors, attr_dim)
        self.neck_emb = nn.Embedding(taxonomy.num_necks, attr_dim)
        self.sleeve_emb = nn.Embedding(taxonomy.num_sleeves, attr_dim)
        self.pattern_emb = nn.Embedding(taxonomy.num_patterns, attr_dim)

        # --- MLP ---
        # Input: visual_embedding (768) + 8 attribute embeddings (8 * attr_dim)
        input_dim = embedding_dim + 8 * attr_dim

        layers = []
        dim_in = input_dim
        for i in range(num_hidden_layers):
            dim_out = hidden_dim if i < num_hidden_layers - 1 else embedding_dim
            layers.extend([
                nn.Linear(dim_in, dim_out),
                nn.LayerNorm(dim_out),
                nn.GELU(),
                nn.Dropout(dropout),
            ])
            dim_in = dim_out

        # Final projection to embedding_dim
        layers.append(nn.Linear(dim_in, embedding_dim))
        self.mlp = nn.Sequential(*layers)

        self._init_weights()

    def _init_weights(self):
        """Initialize embedding tables and MLP weights."""
        for emb in [self.gender_emb, self.category_emb, self.sub_category_emb,
                     self.type_emb, self.color_emb, self.neck_emb,
                     self.sleeve_emb, self.pattern_emb]:
            nn.init.normal_(emb.weight, std=0.02)

        for module in self.mlp:
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                nn.init.zeros_(module.bias)

    def _multi_label_embed(
        self, table: nn.Embedding, labels: torch.Tensor
    ) -> torch.Tensor:
        """
        Mean-pool embeddings for active multi-label classes.

        Args:
            table: nn.Embedding [num_classes, attr_dim]
            labels: [B, num_classes] binary tensor
        Returns:
            [B, attr_dim] — mean of active embeddings (zeros if no label active)
        """
        # labels @ weight = weighted sum, then divide by count
        weighted = labels @ table.weight          # [B, attr_dim]
        count = labels.sum(dim=-1, keepdim=True).clamp(min=1.0)  # [B, 1]
        return weighted / count

    def forward(
        self,
        visual_emb: torch.Tensor,
        gender: torch.Tensor,
        category: torch.Tensor,
        sub_category: torch.Tensor,
        type_idx: torch.Tensor,
        color: torch.Tensor,
        neck: torch.Tensor,
        sleeve: torch.Tensor,
        pattern: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            visual_emb:   [B, 768] visual embedding
            gender:       [B]      single-label index
            category:     [B]      single-label index
            sub_category: [B]      single-label index
            type_idx:     [B]      single-label index
            color:        [B, C_color]   multi-label binary
            neck:         [B, C_neck]    multi-label binary
            sleeve:       [B, C_sleeve]  multi-label binary
            pattern:      [B, C_pattern] multi-label binary
        Returns:
            [B, 768] L2-normalized text embedding
        """
        # Single-label lookups
        g = self.gender_emb(gender)               # [B, attr_dim]
        c = self.category_emb(category)
        s = self.sub_category_emb(sub_category)
        t = self.type_emb(type_idx)

        # Multi-label mean-pooling
        col = self._multi_label_embed(self.color_emb, color)
        n = self._multi_label_embed(self.neck_emb, neck)
        sl = self._multi_label_embed(self.sleeve_emb, sleeve)
        p = self._multi_label_embed(self.pattern_emb, pattern)

        # Concatenate all features
        x = torch.cat([visual_emb, g, c, s, t, col, n, sl, p], dim=-1)

        # MLP → L2 normalize
        out = self.mlp(x)
        return F.normalize(out, p=2, dim=-1)
