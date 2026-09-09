"""Configuration for Magic Eye training phases."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Phase1Config:
    """Phase 1: Classification Heads Training on pre-computed embeddings."""

    # Paths
    workspace: Path = Path(".")
    anchor_json: str = "anchor_items.json"
    visual_embeddings_path: str = "embeddings_cache/visual_embeddings.pt"
    excel_path: str = "MS_Model.xlsx"
    checkpoint_dir: str = "checkpoints/phase1"
    log_path: str = "LOG.md"
    checklist_path: str = "CHECKLIST.md"

    # Training
    batch_size: int = 512
    num_epochs: int = 20
    learning_rate: float = 1e-3
    weight_decay: float = 1e-3
    val_split: float = 0.1
    seed: int = 42
    num_workers: int = 0  # 0 for Windows compatibility

    # Model
    embedding_dim: int = 768
    hidden_dim: int = 512
    hard_head_hidden_dim: int = 768  # larger hidden for sub_category & type heads
    dropout: float = 0.4

    # Regularization
    label_smoothing: float = 0.02
    early_stopping_patience: int = 8

    # Warmup
    warmup_epochs: int = 3

    # Resume
    resume_from: str = ""  # path to checkpoint file


@dataclass
class Phase2Config:
    """Phase 2: End-to-End Fine-tuning with SigLIP Vision Encoder."""

    # Paths
    workspace: Path = Path(".")
    anchor_json: str = "anchor_items.json"
    excel_path: str = "MS_Model.xlsx"
    phase1_checkpoint: str = "checkpoints/phase1/best.pt"
    checkpoint_dir: str = "checkpoints/phase2"
    log_path: str = "LOG.md"

    # Backbone
    backbone_name: str = "google/siglip-base-patch16-224"
    unfreeze_layers: int = 4  # last N of 12 encoder layers

    # Model heads (must match Phase 1)
    embedding_dim: int = 768
    hidden_dim: int = 512
    hard_head_hidden_dim: int = 768
    dropout: float = 0.3

    # Training
    batch_size: int = 128
    gradient_accumulation_steps: int = 1
    num_epochs: int = 30
    head_lr: float = 3e-5
    backbone_lr: float = 5e-6
    weight_decay: float = 0.01
    warmup_epochs: int = 5
    label_smoothing: float = 0.02
    early_stopping_patience: int = 8
    max_grad_norm: float = 1.0
    use_amp: bool = True

    # Data
    val_split: float = 0.1
    seed: int = 42
    num_workers: int = 2
    train_subset_per_epoch: int = 50000  # sample N train images per epoch (0 = all)
    val_subset: int = 5000  # fixed val subset size (0 = all)

    # Resume
    resume_from: str = ""


@dataclass
class Phase3Config:
    """Phase 3: Text Embedding Generator — maps visual features + attributes to text embedding."""

    # Paths
    workspace: Path = Path(".")
    anchor_json: str = "anchor_items.json"
    excel_path: str = "MS_Model.xlsx"
    visual_embeddings_path: str = "embeddings_cache/visual_embeddings.pt"
    text_embeddings_path: str = "embeddings_cache/text_embeddings.pt"
    text_embeddings_metadata: str = "embeddings_cache/text_embeddings_metadata.json"
    checkpoint_dir: str = "checkpoints/phase3"
    log_path: str = "LOG.md"

    # Model
    embedding_dim: int = 768       # SigLIP visual / text embedding dim
    attr_embedding_dim: int = 64   # per-attribute embedding dim
    hidden_dim: int = 1024         # MLP hidden dim
    num_hidden_layers: int = 3     # number of hidden layers in MLP
    dropout: float = 0.1

    # Training
    batch_size: int = 512          # fast on pre-computed embeddings
    num_epochs: int = 50
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    warmup_epochs: int = 3
    early_stopping_patience: int = 10

    # Loss weights
    cosine_loss_weight: float = 1.0
    contrastive_loss_weight: float = 0.5
    contrastive_temperature: float = 0.07

    # Data
    val_split: float = 0.1
    seed: int = 42
    num_workers: int = 0           # 0 for Windows compatibility

    # Resume
    resume_from: str = ""
