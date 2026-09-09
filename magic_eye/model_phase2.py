"""
MagicEyePhase2 — end-to-end model: SigLIP Vision Encoder + Classification Heads.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from transformers import SiglipModel

from .model import MagicEyeClassifier
from .taxonomy import TaxonomyInfo


class MagicEyePhase2(nn.Module):
    """
    End-to-end model for Phase 2 fine-tuning:
      • SigLIP Vision Encoder (partially unfrozen)
      • MagicEyeClassifier heads (loaded from Phase 1)
      • L2 normalization between backbone and heads
    """

    def __init__(
        self,
        taxonomy: TaxonomyInfo,
        backbone_name: str = "google/siglip-base-patch16-224",
        embedding_dim: int = 768,
        hidden_dim: int = 512,
        hard_head_hidden_dim: int = 768,
        dropout: float = 0.3,
        unfreeze_layers: int = 4,
    ):
        super().__init__()

        # Load full SigLIP then extract vision model
        full_model = SiglipModel.from_pretrained(backbone_name)
        self.backbone = full_model.vision_model
        del full_model
        self.classifier = MagicEyeClassifier(
            taxonomy,
            embedding_dim=embedding_dim,
            hidden_dim=hidden_dim,
            hard_head_hidden_dim=hard_head_hidden_dim,
            dropout=dropout,
        )

        self._freeze_backbone(unfreeze_layers)

    # ------------------------------------------------------------------
    def _freeze_backbone(self, unfreeze_last_n: int):
        """Freeze all backbone params, then unfreeze last N encoder layers."""
        for param in self.backbone.parameters():
            param.requires_grad = False

        # Unfreeze last N transformer layers
        for layer in self.backbone.encoder.layers[-unfreeze_last_n:]:
            for param in layer.parameters():
                param.requires_grad = True

        # Always unfreeze post-layernorm and pooling head
        if hasattr(self.backbone, "post_layernorm"):
            for param in self.backbone.post_layernorm.parameters():
                param.requires_grad = True
        if hasattr(self.backbone, "head"):
            for param in self.backbone.head.parameters():
                param.requires_grad = True

    # ------------------------------------------------------------------
    def load_phase1_heads(self, checkpoint_path: str):
        """Load pre-trained classification heads from Phase 1 checkpoint."""
        state = torch.load(checkpoint_path, map_location="cpu")
        self.classifier.load_state_dict(state["model_state_dict"])
        print(f"  Loaded Phase 1 heads from epoch {state['epoch']} "
              f"(best val raw_sum={state.get('best_val_raw', '?')})")

    # ------------------------------------------------------------------
    def forward(self, pixel_values: torch.Tensor) -> dict:
        """
        Args:
            pixel_values: [B, 3, 224, 224] preprocessed images
        Returns:
            dict of logits / predictions (same keys as MagicEyeClassifier)
        """
        vision_output = self.backbone(pixel_values=pixel_values)
        embedding = vision_output.pooler_output          # [B, 768]
        embedding = F.normalize(embedding, p=2, dim=-1)  # L2 norm (matches Phase 1)
        return self.classifier(embedding)

    # ------------------------------------------------------------------
    def backbone_parameters(self):
        """Yield trainable backbone parameters."""
        for param in self.backbone.parameters():
            if param.requires_grad:
                yield param

    def head_parameters(self):
        """Yield all classification head parameters."""
        return self.classifier.parameters()
