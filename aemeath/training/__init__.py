"""Training integration: technical verification of the local GPT-SoVITS path.

This package holds the *verified* adapter surface for driving a local
GPT-SoVITS checkout from Aemeath. It is deliberately separate from
``aemeath.management``: V2-T05 establishes what the upstream training path can
actually support, and V2-T06 (the wizard) builds on those findings.

The module is pure orchestration. It never imports Gradio and never starts the
upstream web UI; see :mod:`aemeath.training.gpt_sovits` for the invocation
contract that was measured against the real install.
"""

from __future__ import annotations

from .gpt_sovits import (
    PreprocessSpec,
    TrainingJob,
    TrainingSpec,
    TrainingStatus,
    build_gpt_train_spec,
    build_preprocess_spec,
    build_sovits_train_spec,
    check_training_inputs,
    find_artifacts,
    fingerprint_upstream,
    merge_all_part_files,
    merge_part_files,
    merge_phoneme_files,
    merge_semantic_files,
    validate_material,
)
from .jobs import (
    JobState,
    TrainingJobRecord,
    TrainingJobStore,
    can_transition,
    reconcile_startup,
)
from .wizard import (
    WIZARD_STEPS,
    TrainingWizardService,
    check_ddp_guard,
    check_ffmpeg,
)

__all__ = [
    "JobState",
    "PreprocessSpec",
    "TrainingJob",
    "TrainingJobRecord",
    "TrainingJobStore",
    "TrainingSpec",
    "TrainingStatus",
    "TrainingWizardService",
    "WIZARD_STEPS",
    "build_gpt_train_spec",
    "build_preprocess_spec",
    "build_sovits_train_spec",
    "can_transition",
    "check_ddp_guard",
    "check_ffmpeg",
    "check_training_inputs",
    "find_artifacts",
    "fingerprint_upstream",
    "merge_all_part_files",
    "merge_part_files",
    "merge_phoneme_files",
    "merge_semantic_files",
    "reconcile_startup",
    "validate_material",
]
