"""Memory retrieval calibration and acceptance harness.

The plan requires the similarity floor to be set from *measured* behaviour on a
real embedding model, using two non-overlapping datasets:

* a **calibration set** that tunes the threshold;
* a **validation set** that measures the tuned configuration exactly once.

Tuning on the validation set is what the plan explicitly forbids, so this script
keeps the two apart: :func:`scan_thresholds` only ever receives calibration data,
and :func:`evaluate` only ever receives the locked threshold.

Two rules shape the implementation:

1. Facts are written **through the client-equivalent path** — real messages,
   real extraction adapter, real embedding. Nothing is inserted as a
   pre-canned "correct answer", because a memory the extractor cannot actually
   produce is not a memory the product has.
2. Retrieval is scored with :meth:`MemoryStore.recall`, the same call a
   conversation makes. Scoring the vectors directly would measure the
   arithmetic rather than the feature.

Usage:
    python scripts/calibrate_memory.py --dataset calibration
    python scripts/calibrate_memory.py --dataset validation --floor 0.42
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber" / "src"))
sys.path.insert(0, str(ROOT_DIR))

from aemeath.adapters import AdapterFactory  # noqa: E402
from aemeath.config import load_config, resolve_config_path  # noqa: E402
from aemeath.memory import MemoryService, MemoryStore  # noqa: E402

DATASETS_DIR = ROOT_DIR / "docs" / "acceptance-data"

# The plan's acceptance rule: a floor qualifies only when BOTH the synonym
# recall rate and the unrelated empty-result rate reach 9/10 at the same time.
SYNONYM_MIN_RATIO = 0.9
UNRELATED_MIN_RATIO = 0.9


@dataclass
class Dataset:
    """One fact set with its probe questions.

    Attributes:
        name: Dataset identifier.
        facts: Statements to send as user messages, in order.
        synonym_questions: Questions that should retrieve a specific fact.
        unrelated_questions: Questions that should retrieve nothing.
        corrections: ``(old_fact_index, new_statement)`` pairs.
        forgets: ``(fact_index, reason)`` pairs.
        multi_fact_message: A message carrying two independent facts, plus
            the index of the one to forget and the one that must survive.
    """

    name: str
    facts: List[str] = field(default_factory=list)
    synonym_questions: List[str] = field(default_factory=list)
    unrelated_questions: List[str] = field(default_factory=list)
    corrections: List[Dict[str, Any]] = field(default_factory=list)
    forgets: List[Dict[str, Any]] = field(default_factory=list)
    multi_fact_message: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Dataset":
        """Read a dataset from JSON."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            facts=data["facts"],
            synonym_questions=data["synonym_questions"],
            unrelated_questions=data["unrelated_questions"],
            corrections=data.get("corrections", []),
            forgets=data.get("forgets", []),
            multi_fact_message=data.get("multi_fact_message", {}),
        )


@dataclass
class Score:
    """One retrieval outcome."""

    question: str
    expected: Optional[str]
    retrieved: List[str]
    top_score: Optional[float]
    correct: bool

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the report."""
        return {
            "question": self.question,
            "expected": self.expected,
            "retrieved": self.retrieved,
            "top_score": self.top_score,
            "correct": self.correct,
        }


class MemoryHarness:
    """Drives a temporary memory database through the real adapter stack."""

    def __init__(self, config, db_path: Path, similarity_floor: float) -> None:
        """Assemble the store and service for one run.

        Args:
            config: Resolved Aemeath configuration.
            db_path: Database file for this run (temporary by default).
            similarity_floor: Retrieval floor to evaluate.
        """
        factory = AdapterFactory.from_config(config)
        self.embedding, embedding_status = factory.build_embedding()
        self.extraction, extraction_status = factory.build_extraction()
        if self.embedding is None:
            raise SystemExit(f"embedding unavailable: {embedding_status.error or embedding_status.detail}")
        if self.extraction is None:
            raise SystemExit(
                f"extraction unavailable: {extraction_status.error or extraction_status.detail}"
            )

        self.store = MemoryStore(
            db_path, self.embedding, similarity_floor=similarity_floor
        )
        self.service = MemoryService(self.store, extraction=self.extraction)
        self.db_path = db_path

    def close(self) -> None:
        """Release the database handle.

        Every store call opens and closes its own connection, so nothing is held
        open by the store itself; this exists so the temporary directory can be
        removed on Windows, where an open handle blocks deletion.
        """
        self.store = None  # type: ignore[assignment]

    def __enter__(self) -> "MemoryHarness":
        """Support use as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Release the database handle on exit."""
        self.close()

    async def ingest(self, statements: Sequence[str]) -> List[str]:
        """Write statements as completed turns, then wait for extraction.

        Each statement becomes a user message followed by a short reply, which
        is how a real turn is shaped and what the extraction prompt expects.

        Args:
            statements: User statements to ingest.

        Returns:
            The message ids, in order.
        """
        message_ids: List[str] = []
        for statement in statements:
            user_id = self.service.record_user_message(statement, source="calibration")
            reply_id = self.service.record_assistant_message("好，我记住了。")
            await self.service.process_turn(user_id, reply_id)
            message_ids.append(user_id)

        # Drain until the queue is empty: extraction is asynchronous in the
        # product, and scoring before it finishes would measure an empty index.
        for _ in range(20):
            if not self.store.pending_tasks():
                break
            await self.service.run_pending_extraction()
        return message_ids

    async def recall(self, query: str, limit: int = 5):
        """Run one retrieval through the same call a conversation uses."""
        return await self.service.recall(query, limit=limit)

    def memories(self):
        """All valid memories, newest first."""
        return self.store.list_memories()

    def find_memory(self, needle: str):
        """Find a valid memory whose text contains ``needle``."""
        for item in self.memories():
            if needle in item.content:
                return item
        return None


async def _score_question(
    harness: MemoryHarness, question: str, expected: Optional[str]
) -> Score:
    """Retrieve for one question and judge the outcome.

    A synonym question is correct when the fact it paraphrases is retrieved. An
    unrelated question is correct when *nothing* comes back — the failure the
    plan calls out is injecting an unrelated personal memory, which then makes
    the model talk about something the user never raised.
    """
    results = await harness.recall(question)
    contents = [record.content for record in results]
    top = None

    # Recompute the raw top score regardless of the floor, so a threshold scan
    # can see what the floor is cutting off instead of only what survived it.
    ids, matrix = harness.store.load_vectors()
    if ids:
        import numpy as np

        query_vector = np.asarray(
            (await harness.embedding.embed([question]))[0], dtype=np.float32
        )
        if query_vector.shape[0] == matrix.shape[1]:
            norms = np.linalg.norm(matrix, axis=1)
            similarities = matrix @ query_vector / (
                norms * np.linalg.norm(query_vector) + 1e-9
            )
            finite = np.isfinite(similarities)
            if finite.any():
                top = float(similarities[finite].max())

    if expected is None:
        correct = not contents
    else:
        correct = any(expected in content for content in contents)

    return Score(
        question=question,
        expected=expected,
        retrieved=contents,
        top_score=top,
        correct=correct,
    )


def qualifies(floor_report: Dict[str, Any]) -> bool:
    """Whether one floor's measurements meet both halves of the rule.

    A floor qualifies only when the synonym recall **hits the annotated target
    fact** (not merely returns something) and unrelated questions return
    nothing, each at ≥ 9/10. Zone overlap is diagnostic only: an overlapping
    distribution can still produce a qualifying floor when the rule is applied
    per-question rather than per-zone.
    """
    synonym_total = floor_report["synonym_total"]
    unrelated_total = floor_report["unrelated_total"]
    if synonym_total == 0 or unrelated_total == 0:
        return False
    return (
        floor_report["synonym_hits"] / synonym_total >= SYNONYM_MIN_RATIO
        and floor_report["unrelated_clean"] / unrelated_total >= UNRELATED_MIN_RATIO
    )


def select_floor(floors: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick one floor among those meeting both thresholds.

    Selection order, most important first: higher synonym recall, higher
    unrelated empty rate, higher floor. When nothing qualifies, returns
    ``None`` — the caller must report failure, not lower the bar.
    """
    candidates = [entry for entry in floors if qualifies(entry)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda entry: (
            entry["synonym_hits"] / entry["synonym_total"],
            entry["unrelated_clean"] / entry["unrelated_total"],
            entry["floor"],
        ),
    )


async def scan_thresholds(
    dataset: Dataset, config, floors: Sequence[float], workdir: Path
) -> Dict[str, Any]:
    """Measure retrieval quality at each candidate floor.

    The facts are ingested once and the index is reused across floors: the floor
    only filters results, it does not change what is stored, so re-ingesting per
    floor would multiply API cost without changing the measurement.

    Args:
        dataset: The **calibration** dataset.
        config: Resolved Aemeath configuration.
        floors: Candidate similarity floors.
        workdir: Directory for the temporary database.

    Returns:
        Per-floor scores plus the raw question-by-question detail.
    """
    db_path = workdir / f"{dataset.name}.sqlite3"
    harness = MemoryHarness(config, db_path, similarity_floor=0.0)
    await harness.ingest(dataset.facts)

    stored = harness.memories()
    report: Dict[str, Any] = {
        "dataset": dataset.name,
        "facts_sent": len(dataset.facts),
        "memories_extracted": len(stored),
        "memories": [item.content for item in stored],
        "floors": {},
    }

    # Measure the raw similarity distribution first, with no floor applied.
    #
    # This is the diagnostic that matters. If unrelated questions score as high
    # as synonym questions, no threshold can separate them, and the honest
    # conclusion is "this embedding model cannot support retrieval" rather than
    # "the threshold needs lowering". Reporting only hit rates per floor hides
    # that distinction, because every floor then looks equally good (or bad).
    synonym_expected = _pair_expectations(harness, dataset)
    separation = await _measure_separation(harness, dataset, synonym_expected)
    report["separation"] = separation

    for floor in floors:
        harness.store.similarity_floor = floor

        synonym_scores = []
        for question, expected in synonym_expected:
            synonym_scores.append(
                await _score_question(harness, question, expected)
            )

        unrelated_scores = [
            await _score_question(harness, question, None)
            for question in dataset.unrelated_questions
        ]

        hits = sum(1 for score in synonym_scores if score.correct)
        clean = sum(1 for score in unrelated_scores if score.correct)
        floor_report = {
            "floor": floor,
            "synonym_hits": hits,
            "synonym_total": len(synonym_scores),
            "unrelated_clean": clean,
            "unrelated_total": len(unrelated_scores),
            "synonym_pass": len(synonym_scores) > 0
            and hits / len(synonym_scores) >= SYNONYM_MIN_RATIO,
            "unrelated_pass": len(unrelated_scores) > 0
            and clean / len(unrelated_scores) >= UNRELATED_MIN_RATIO,
            "qualifies": None,  # filled after all floors are measured
            "synonym_detail": [score.to_dict() for score in synonym_scores],
            "unrelated_detail": [score.to_dict() for score in unrelated_scores],
        }
        floor_report["qualifies"] = qualifies(floor_report)
        report["floors"][f"{floor:.2f}"] = floor_report

    # The selection is judged only on the two ratios; zone overlap stays in
    # report["separation"] as a diagnostic and does not gate this decision.
    ordered_floors = sorted(
        report["floors"].values(), key=lambda entry: entry["floor"]
    )
    selected = select_floor(ordered_floors)
    report["selected"] = (
        None if selected is None else {key: selected[key] for key in (
            "floor", "synonym_hits", "synonym_total",
            "unrelated_clean", "unrelated_total",
        )}
    )

    harness.close()
    return report


def _pair_expectations(
    harness: MemoryHarness, dataset: Dataset
) -> List[tuple[str, Optional[str]]]:
    """Pair each synonym question with the stored memory it paraphrases.

    The datasets are written so question *i* paraphrases fact *i*. The pairing
    is resolved against what the extractor actually stored, by matching a
    distinctive span of the original fact — extraction rewrites the first person
    ("我养了" -> "用户养了"), so the whole sentence would never match.

    Args:
        harness: The populated harness.
        dataset: The dataset being scored.

    Returns:
        ``(question, expected_memory_text)`` pairs; ``None`` when the fact was
        not extracted at all, which is reported as a miss with a clear reason.
    """
    pairs: List[tuple[str, Optional[str]]] = []
    for index, question in enumerate(dataset.synonym_questions):
        expected = None
        if index < len(dataset.facts):
            needle = _needle(dataset.facts[index])
            memory = harness.find_memory(needle)
            expected = memory.content if memory else None
        pairs.append((question, expected))
    return pairs


async def _measure_separation(
    harness: MemoryHarness,
    dataset: Dataset,
    synonym_expected: Sequence[tuple[str, Optional[str]]],
) -> Dict[str, Any]:
    """Measure how far apart related and unrelated queries actually score.

    Args:
        harness: The populated harness.
        dataset: The dataset being scored.
        synonym_expected: Pairings from :func:`_pair_expectations`.

    Returns:
        Score distributions and a verdict on whether a floor can work at all.
    """
    import numpy as np

    def top_scores(questions: Sequence[str], vectors, matrix) -> List[float]:
        scores: List[float] = []
        for question in questions:
            vector = np.asarray(embeddings[question], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1)
            sims = matrix @ vector / (norms * np.linalg.norm(vector) + 1e-9)
            finite = np.isfinite(sims)
            scores.append(float(sims[finite].max()) if finite.any() else 0.0)
        return scores

    # Embed every probe question once; the scan reuses these vectors.
    questions = [q for q, _ in synonym_expected] + list(dataset.unrelated_questions)
    embeddings = {
        question: vector
        for question, vector in zip(
            questions, await harness.embedding.embed(questions)
        )
    }

    _ids, matrix = harness.store.load_vectors()
    if matrix.size == 0:
        return {"available": False, "detail": "index is empty"}

    related = top_scores([q for q, _ in synonym_expected], embeddings, matrix)
    unrelated = top_scores(list(dataset.unrelated_questions), embeddings, matrix)

    lowest_related = float(np.min(related))
    highest_unrelated = float(np.max(unrelated))
    separated = lowest_related > highest_unrelated

    return {
        "available": True,
        "related_min": lowest_related,
        "related_mean": float(np.mean(related)),
        "unrelated_max": highest_unrelated,
        "unrelated_mean": float(np.mean(unrelated)),
        # The widest gap: any floor in (unrelated_max, related_min] satisfies
        # both halves of the plan's requirement at once.
        "separated": separated,
        "suggested_floor": (
            round((lowest_related + highest_unrelated) / 2.0, 3)
            if separated
            else None
        ),
        "detail": (
            "related and unrelated queries overlap; no threshold can separate "
            "them, so the embedding model cannot support this retrieval task"
            if not separated
            else "a separating threshold exists"
        ),
    }


async def evaluate(dataset: Dataset, config, floor: float, workdir: Path) -> Dict[str, Any]:
    """Run the validation set once at a locked floor.

    Args:
        dataset: The **validation** dataset.
        config: Resolved Aemeath configuration.
        floor: The locked similarity floor from calibration.
        workdir: Directory for the temporary database.

    Returns:
        Full validation results, including corrections and forgets.
    """
    db_path = workdir / f"{dataset.name}.sqlite3"
    harness = MemoryHarness(config, db_path, similarity_floor=floor)
    await harness.ingest(dataset.facts)

    report: Dict[str, Any] = {
        "dataset": dataset.name,
        "floor": floor,
        "facts_sent": len(dataset.facts),
        "memories_after_ingest": [m.content for m in harness.memories()],
    }

    # -- recall --------------------------------------------------------
    synonym_scores = [
        await _score_question(harness, question, expected)
        for question, expected in _pair_expectations(harness, dataset)
    ]

    unrelated_scores = [
        await _score_question(harness, question, None)
        for question in dataset.unrelated_questions
    ]

    synonym_hits = sum(1 for score in synonym_scores if score.correct)
    unrelated_clean = sum(1 for score in unrelated_scores if score.correct)
    report["recall"] = {
        "synonym_hits": synonym_hits,
        "synonym_total": len(synonym_scores),
        "synonym_pass": synonym_hits >= 9,
        "unrelated_clean": unrelated_clean,
        "unrelated_total": len(unrelated_scores),
        "unrelated_pass": unrelated_clean >= 9,
        "synonym_detail": [score.to_dict() for score in synonym_scores],
        "unrelated_detail": [score.to_dict() for score in unrelated_scores],
    }

    # -- corrections ---------------------------------------------------
    correction_results = []
    for entry in dataset.corrections:
        old_needle = entry["old"]
        new_statement = entry["new"]
        old_memory = harness.find_memory(old_needle)
        if old_memory is None:
            correction_results.append(
                {"old": old_needle, "new": new_statement,
                 "ok": False, "detail": "original memory not found"}
            )
            continue

        harness.store.correct_memory(old_memory.memory_id, new_statement)
        # Re-embed the corrected text so retrieval sees the new fact.
        vector = (await harness.embedding.embed([new_statement]))[0]
        harness.store.store_embedding(old_memory.memory_id, vector)

        results = await harness.recall(entry["question"])
        contents = [record.content for record in results]
        uses_new = any(new_statement[:12] in content for content in contents)
        uses_old = any(old_needle in content for content in contents)
        correction_results.append(
            {
                "old": old_needle,
                "new": new_statement,
                "question": entry["question"],
                "retrieved": contents,
                "ok": uses_new and not uses_old,
            }
        )
    report["corrections"] = {
        "passed": sum(1 for item in correction_results if item["ok"]),
        "total": len(correction_results),
        "detail": correction_results,
    }

    # -- forgets -------------------------------------------------------
    forget_results = []
    for entry in dataset.forgets:
        needle = entry["fact"]
        memory = harness.find_memory(needle)
        if memory is None:
            forget_results.append(
                {"fact": needle, "ok": False, "detail": "memory not found"}
            )
            continue

        memory_id = memory.memory_id
        outcome = harness.store.forget(memory_id)
        if not outcome.get("ok", True):
            forget_results.append(
                {"fact": needle, "ok": False,
                 "detail": f"forget required selection: {outcome.get('reason')}"}
            )
            continue

        # The plan's real test of forgetting: it must not come back from
        # history, a summary, or a queued background task.
        await harness.service.run_pending_extraction()
        results = await harness.recall(entry["question"], limit=5)
        contents = [record.content for record in results]
        history = [
            message.content
            for message in harness.store.recent_messages(limit=100)
        ]
        still_retrievable = any(needle in content for content in contents)
        still_in_history = any(needle in content for content in history)
        resurrected = harness.find_memory(needle) is not None

        forget_results.append(
            {
                "fact": needle,
                "retrieved_after_forget": contents,
                "still_retrievable": still_retrievable,
                "still_in_history": still_in_history,
                "resurrected_by_extraction": resurrected,
                "ok": not (still_retrievable or still_in_history or resurrected),
            }
        )
    report["forgets"] = {
        "passed": sum(1 for item in forget_results if item["ok"]),
        "total": len(forget_results),
        "detail": forget_results,
    }

    # -- one message, two facts ---------------------------------------
    multi = dataset.multi_fact_message
    if multi:
        report["multi_fact"] = await _check_multi_fact(harness, multi)

    harness.close()
    return report


def _needle(fact: str) -> str:
    """Extract a short distinctive span to match against retrieved memory text.

    Extraction rewrites the third person ("我养了" -> "用户养了"), so matching the
    whole sentence would fail even when the fact was stored correctly. A short
    content-bearing span survives that rewrite.
    """
    cleaned = fact.replace("，", " ").replace("。", " ").replace(",", " ").strip()
    parts = [part for part in cleaned.split() if len(part) >= 3]
    return parts[0] if parts else fact[:6]


async def _check_multi_fact(harness: MemoryHarness, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Verify forgetting one fact from a message leaves the other usable."""
    message = spec["message"]
    user_id = harness.service.record_user_message(message, source="calibration")
    reply_id = harness.service.record_assistant_message("好。")
    await harness.service.process_turn(user_id, reply_id)
    for _ in range(10):
        if not harness.store.pending_tasks():
            break
        await harness.service.run_pending_extraction()

    forget_memory = harness.find_memory(spec["forget"])
    keep_needle = spec["keep"]
    if forget_memory is None:
        return {"ok": False, "detail": f"memory for {spec['forget']!r} not extracted"}

    harness.store.forget(forget_memory.memory_id)
    await harness.service.run_pending_extraction()

    remains = harness.find_memory(keep_needle)
    gone = harness.find_memory(spec["forget"])
    return {
        "message": message,
        "forgot": spec["forget"],
        "kept": keep_needle,
        "kept_memory_present": remains is not None,
        "forgotten_memory_present": gone is not None,
        "ok": remains is not None and gone is None,
    }


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the harness arguments."""
    parser = argparse.ArgumentParser(description="Aemeath 记忆检索校准与验收")
    parser.add_argument(
        "--dataset",
        choices=["calibration", "validation"],
        required=True,
        help="要运行的数据集",
    )
    parser.add_argument(
        "--config", metavar="PATH", help="使用的配置文件"
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=None,
        help="锁定的相似度下限（validation 必填；calibration 改用 --scan）",
    )
    parser.add_argument(
        "--scan",
        metavar="VALUES",
        default="0.20,0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60",
        help="校准扫描的下限候选值，逗号分隔",
    )
    parser.add_argument(
        "--output", metavar="PATH", help="把 JSON 结果写到文件"
    )
    parser.add_argument(
        "--keep-db", action="store_true", help="保留临时数据库以便复现"
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    """Execute the requested mode."""
    config = load_config(resolve_config_path(args.config))
    dataset_path = DATASETS_DIR / f"{args.dataset}.json"
    if not dataset_path.is_file():
        raise SystemExit(f"dataset not found: {dataset_path}")
    dataset = Dataset.load(dataset_path)

    if args.dataset == "calibration":
        floors = [float(value) for value in args.scan.split(",") if value.strip()]
        if args.keep_db:
            workdir = ROOT_DIR / "data" / "acceptance" / "calibration"
            workdir.mkdir(parents=True, exist_ok=True)
            return await scan_thresholds(dataset, config, floors, workdir)
        with tempfile.TemporaryDirectory(prefix="aemeath-calib-") as tmp:
            return await scan_thresholds(dataset, config, floors, Path(tmp))

    if args.floor is None:
        raise SystemExit("--floor is required for the validation dataset")
    if args.keep_db:
        workdir = ROOT_DIR / "data" / "acceptance" / "validation"
        workdir.mkdir(parents=True, exist_ok=True)
        return await evaluate(dataset, config, args.floor, workdir)
    with tempfile.TemporaryDirectory(prefix="aemeath-val-") as tmp:
        return await evaluate(dataset, config, args.floor, Path(tmp))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the harness and print a report."""
    args = _parse_args(argv)
    report = asyncio.run(_run(args))

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入 {output}")

    if args.dataset == "calibration":
        print(f"数据集 {report['dataset']}: 发送 {report['facts_sent']} 条，"
              f"提取到 {report['memories_extracted']} 条记忆")

        separation = report.get("separation", {})
        if separation.get("available"):
            print("\n相似度分布（未加下限）")
            print(f"  同义问题 最低/均值: {separation['related_min']:.3f} / "
                  f"{separation['related_mean']:.3f}")
            print(f"  无关问题 最高/均值: {separation['unrelated_max']:.3f} / "
                  f"{separation['unrelated_mean']:.3f}")
            if separation["separated"]:
                print(f"  可分离：建议下限 {separation['suggested_floor']}")
            else:
                # This is the outcome the plan anticipates: report it as a
                # retrieval-strategy problem, not as "lower the threshold".
                print("  不可分离：同义与无关分数重叠，任何阈值都无法同时满足召回与过滤。")
                print("  结论：当前嵌入模型不支持该检索任务，需要更换模型或改进检索策略。")
        else:
            print(f"  无法测量相似度分布：{separation.get('detail')}")

        print(f"\n{'下限':>6} {'同义命中':>10} {'无关干净':>10} {'达标':>4}")
        for key, data in report["floors"].items():
            print(
                f"{data['floor']:>6.2f} "
                f"{data['synonym_hits']:>4}/{data['synonym_total']:<5} "
                f"{data['unrelated_clean']:>4}/{data['unrelated_total']:<5} "
                f"{'是' if data['qualifies'] else '否':>4}"
            )

        selected = report.get("selected")
        if selected is not None:
            print(f"\n选定下限 {selected['floor']:.2f}："
                  f"同义命中 {selected['synonym_hits']}/{selected['synonym_total']}，"
                  f"无关空结果 {selected['unrelated_clean']}/{selected['unrelated_total']}，"
                  f"双项 ≥ 9/10 达标。")
        else:
            print("\n失败：没有任何候选下限同时达到 同义 ≥9/10 与 无关 ≥9/10，"
                  "不自动降低标准。")
            print("先核对模型要求的 query/document 前缀、输入格式与截断情况；"
                  "格式正确仍失败时更换模型或调整检索策略。")
    else:
        recall = report["recall"]
        print(f"数据集 {report['dataset']}（下限 {report['floor']}）")
        print(f"  同义召回: {recall['synonym_hits']}/{recall['synonym_total']} "
              f"{'PASS' if recall['synonym_pass'] else 'FAIL'}")
        print(f"  无关过滤: {recall['unrelated_clean']}/{recall['unrelated_total']} "
              f"{'PASS' if recall['unrelated_pass'] else 'FAIL'}")
        corrections = report["corrections"]
        print(f"  纠正生效: {corrections['passed']}/{corrections['total']}")
        forgets = report["forgets"]
        print(f"  遗忘彻底: {forgets['passed']}/{forgets['total']}")
        if "multi_fact" in report:
            print(f"  一句话两事实: {'PASS' if report['multi_fact']['ok'] else 'FAIL'}")

    print(json.dumps(report, ensure_ascii=False, indent=2) if not args.output else "")
    if args.dataset == "calibration" and report.get("selected") is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
