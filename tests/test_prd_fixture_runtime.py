"""Black-box forward tests for every §17 fixture.

Each case.json selects a real CLI operation and its expected structured gate
error.  The test constructs only small temporary source bundles, invokes the
production commands through subprocesses, and keeps every generated file under
the platform's temporary root.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_contract import ADVERSARIAL_FIXTURES, POSITIVE_FIXTURES, load_case
from test_assessment_contract import AssessmentContractTest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SCRIPTS = ROOT / "scripts"
POSITIVE = FIXTURES / "print-positive"
TEMP_ROOT = Path(tempfile.gettempdir())
PYTHON_CACHE = TEMP_ROOT / "mse-prd-pycache"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def command_env() -> dict[str, str]:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    PYTHON_CACHE.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["TMPDIR"] = str(TEMP_ROOT)
    environment["PYTHONPYCACHEPREFIX"] = str(PYTHON_CACHE)
    return environment


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=command_env(), capture_output=True, text=True, check=False)


def last_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout.strip())
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(result.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def append_text(value: Any, suffix: str) -> Any:
    if isinstance(value, str):
        return value + " " + suffix
    if isinstance(value, list):
        return [append_text(child, suffix) for child in value]
    if isinstance(value, dict):
        result = dict(value)
        for key in ("text", "prompt", "passage", "stem", "context", "script_outline", "rationale", "purpose", "descriptor"):
            if isinstance(result.get(key), str):
                result[key] = result[key] + " " + suffix
        return result
    return value


def update_plan(assessment: dict[str, Any], items: list[dict[str, Any]]) -> None:
    counts: dict[tuple[str, int], int] = {}
    order: list[tuple[str, int]] = []
    for item in items:
        key = (str(item["item_type"]), int(item["score"]))
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += 1
    plan = [
        {"item_type": item_type, "item_count": counts[(item_type, score)], "score_each": score}
        for item_type, score in order
    ]
    total = sum(line["item_count"] * line["score_each"] for line in plan)
    request = assessment["request"]
    request["item_type_plan"] = copy.deepcopy(plan)
    request["total_score"] = total
    blueprint = assessment["blueprint"]
    blueprint["request"] = copy.deepcopy(request)
    blueprint["sections"] = [
        {**line, "score_total": line["item_count"] * line["score_each"]}
        for line in plan
    ]
    blueprint["score_check"] = {"expected_total": total, "computed_total": total}
    blueprint["boundary_check"] = {
        "allowed_primary_levels": ["A", "B"],
        "reinforcement": False,
        "context_only_level": "D",
    }


def primary_ids() -> list[str]:
    source = read_json(ROOT / "references" / "grade-07-semester-2.json")
    return [
        str(item["id"])
        for item in source.get("items", [])
        if item.get("unit_id") == "unit-01" and item.get("level") in {"A", "B"}
    ]


def coverage_targets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    order: list[str] = []
    for item in items:
        canonical_id = str(item["canonical_item_ids"][0])
        if canonical_id not in counts:
            order.append(canonical_id)
            counts[canonical_id] = 0
        counts[canonical_id] += 1
    return [
        {"canonical_item_id": canonical_id, "target_role": "primary", "planned_item_count": counts[canonical_id]}
        for canonical_id in order
    ]


def paper_item_count() -> int:
    # The shared fixture begins with eleven all-type probes.  Forty-four
    # deterministic reinforcement items then make 55 total: the student
    # projection closes on an eight-item page and the teacher projection
    # closes on a five-item page.  This is a measured boundary for two
    # different projections, not a whitespace-threshold exemption.
    return int(os.environ.get("MSE_PRD_ITEM_COUNT", "55"))


def closing_repeats() -> int:
    return int(os.environ.get("MSE_PRD_CLOSING_REPEATS", "5"))


def unique_choice(template: dict[str, Any], item_id: str, canonical_id: str, index: int) -> dict[str, Any]:
    item = copy.deepcopy(template)
    suffix = f"Fixture choice {index:02d} provides a distinct print regression probe."
    item["item_id"] = item_id
    item["canonical_item_ids"] = [canonical_id]
    item["stem"] = f"{item.get('stem', 'Choose the correct answer.')} {suffix}"
    item["rationale"] = f"{item.get('rationale', 'The selected option is correct.')} {suffix}"
    item["options"] = [
        {**option, "text": f"{option.get('text', '')} ({index:02d})"}
        for option in item.get("options", [])
        if isinstance(option, dict)
    ]
    if item.get("item_type") in {"single_choice", "reading_multiple_choice"}:
        option_id = ["A", "B"][index % 2]
        item.setdefault("answer", {})["option_ids"] = [option_id]
        item["rationale"] = f"Option {option_id} is correct."
    item["validation"] = {"answer_unique": True}
    return item


def matching_item(case_id: str) -> dict[str, Any]:
    long_options = case_id == "print-matching-long-options"
    cross_page = case_id == "print-matching-cross-page-balanced"
    prompt_count = int(os.environ.get("MSE_CROSS_PROMPT_COUNT", "20")) if cross_page else 5
    option_count = int(os.environ.get("MSE_CROSS_OPTION_COUNT", "20")) if cross_page else 7
    cross_passage_units = int(os.environ.get("MSE_CROSS_PASSAGE_UNITS", "3"))
    passage_repeats = int(os.environ.get("MSE_MATCHING_PASSAGE_REPEATS", "1"))
    default_extra = 18 if not long_options and not cross_page else 4 if long_options else 0
    passage_extra = " The measured passage adds one balanced context sentence for pagination." * int(os.environ.get("MSE_MATCHING_EXTRA_SENTENCES", str(default_extra)))
    detail = (
        " The complete option includes a location, time, participants, purpose, and evidence for a real school activity."
        if long_options or cross_page
        else ""
    )
    detail += os.environ.get("MSE_CROSS_DETAIL_SUFFIX", "") if cross_page else ""
    prompt_detail = detail + (os.environ.get("MSE_CROSS_PROMPT_SUFFIX", "") if cross_page else "")
    cross_option_extra = " The timetable and participant list are part of the evidence." if cross_page else ""
    return {
        "item_id": "fixture-matching-001",
        "item_type": "reading_matching",
        "passage": (
            "Read the descriptions and match every prompt with the best complete option. "
            + (
                "The passage is balanced across two independent reading flows. " * cross_passage_units
                if cross_page
                else "The school club descriptions give a complete setting, time, purpose, participants, and evidence for each activity. " * 12
            )
        ) * passage_repeats + passage_extra,
        "prompts": [
            {"prompt_id": f"p{index}", "text": f"Prompt {index} asks for one complete school activity description.{prompt_detail}"}
            for index in range(1, prompt_count + 1)
        ],
        "options": [
            {"option_id": chr(65 + index), "text": f"Option {chr(65 + index)} is a complete school activity description.{detail}{cross_option_extra if cross_page and index < 2 else ''}"}
            for index in range(option_count)
        ],
        "answer": {
            "matches": [
                {"prompt_id": f"p{index}", "option_id": chr(64 + index)}
                for index in range(1, prompt_count + 1)
            ]
        },
        "rationale": "Each prompt is bound to one unique option in the measured matching layout.",
        "score": 10,
        "canonical_item_ids": [],
        "context_item_ids": [],
        "validation": {"answer_unique": True},
    }


def task_item() -> dict[str, Any]:
    passage_units = int(os.environ.get("MSE_TASK_PASSAGE_UNITS", "26"))
    return {
        "item_id": "fixture-task-001",
        "item_type": "task_based_reading",
        "passage": "Read the school club notice and answer each question in one sentence. "
        "The notice gives the place, time, purpose, and participants. " * passage_units,
        "tasks": [
            {"task_id": f"t{index}", "prompt": f"Question {index}: state one detail from the notice.", "response_format": "short answer", "score": 1}
            for index in range(1, 4)
        ],
        "answer": {"responses": [{"task_id": f"t{index}", "response": f"The notice detail {index}."} for index in range(1, 4)]},
        "rationale": "Each response addresses its bound task.",
        "score": 3,
        "canonical_item_ids": [],
        "context_item_ids": [],
        "validation": {"answer_unique": True},
    }


def word_bank_item(count: int) -> dict[str, Any]:
    words = [f"word-{index}" for index in range(1, count + 1)]
    extra_units = int(os.environ.get("MSE_WORD_BANK_EXTRA_SENTENCES", "35"))
    return {
        "item_id": "fixture-word-bank-001",
        "item_type": "word_bank_fill",
        "stem": "Complete the sentence with the correct word: The school club is [b1]."
        + " The measured sentence provides a clear school context and keeps the response line bound to the blank. " * extra_units,
        "blanks": [{"blank_id": "b1", "position": 1}],
        "word_bank": words,
        "answer": {"blank_answers": [{"blank_id": "b1", "value": "word-1"}]},
        "rationale": "The first word completes the sentence.",
        "score": 2,
        "canonical_item_ids": [],
        "context_item_ids": [],
        "validation": {"answer_unique": True},
    }


def custom_paper(case_id: str, custom: dict[str, Any]) -> dict[str, Any]:
    base = AssessmentContractTest().assessment()
    ids = primary_ids()
    # Layout-focused positives must exercise the named semantic construct,
    # not carry an unrelated tail of ordinary choice items.  The all-types
    # paper has its own measured multi-page boundary above.
    default_count = 1 if case_id.startswith(("print-matching-", "print-task-", "print-word-bank-")) else 40
    if case_id.startswith("print-matching-"):
        # The cross-page positive is a real paper boundary: the matching
        # item continues onto the next page and complete later items occupy
        # the same pages in both projections, so teacher answers cannot be
        # stranded on a sparse tail page.
        default_count = 26 if case_id == "print-matching-cross-page-balanced" else 1
    elif case_id.startswith(("print-task-", "print-word-bank-")):
        default_count = 1
    target_count = min(int(os.environ.get("MSE_PRD_CUSTOM_ITEM_COUNT", str(default_count))), len(ids) + 8)
    items = [copy.deepcopy(custom)]
    items[0]["canonical_item_ids"] = [ids[0]]
    for index in range(1, target_count):
        items.append(unique_choice(base["items"][1], f"{case_id}-choice-{index:02d}", ids[index % len(ids)], index))
    if "stem" in items[-1]:
        items[-1]["stem"] += " The closing item keeps enough measured text to avoid an artificial tail gap." * closing_repeats()
    if "rationale" in items[-1] and "stem" in items[-1]:
        items[-1]["rationale"] += " The closing item is intentionally verbose for deterministic page occupancy." * 2
    assessment = copy.deepcopy(base)
    assessment["assessment_id"] = case_id
    assessment["items"] = items
    update_plan(assessment, items)
    assessment["blueprint"]["coverage_targets"] = coverage_targets(items)
    return assessment


def assessment_for_case(case_id: str, variant: str) -> dict[str, Any]:
    base = AssessmentContractTest().assessment()
    ids = primary_ids()
    # The print positive is a physical multi-page projection, so it may use
    # deterministic reinforcement probes beyond the first canonical-ID pass.
    # Item IDs remain unique and coverage remains explicit; reusing a bounded
    # primary canonical ID is allowed by the assessment contract.
    target_count = min(paper_item_count(), len(ids) + 32)
    if variant == "all-types" or variant == "font-fallback":
        items: list[dict[str, Any]] = []
        body_repeats = int(os.environ.get("MSE_ALL_TYPES_BODY_REPEATS", "0"))
        for index, template in enumerate(base["items"]):
            probe_text = (
                f"All-type print probe {index + 1}."
                + " The measured source records a clear setting, purpose, participants, timing, and evidence for this registered item."
                * body_repeats
            )
            item = append_text(copy.deepcopy(template), probe_text)
            if item.get("item_type") == "reading_matching":
                item["passage"] = "Read the measured school activity descriptions and match each prompt with one option."
                item["prompts"] = [
                    {"prompt_id": f"p{prompt_index}", "text": f"Prompt {prompt_index} identifies one school activity." + (" It includes setting, timing, purpose, participants, and evidence." * (prompt_index % 3))}
                    for prompt_index in range(1, 6)
                ]
                item["options"] = [
                    {"option_id": chr(65 + option_index), "text": f"Option {chr(65 + option_index)} describes one complete school activity." + (" It includes setting, timing, purpose, participants, and evidence." * (option_index % 4))}
                    for option_index in range(7)
                ]
                item["answer"] = {"matches": [{"prompt_id": f"p{prompt_index}", "option_id": chr(64 + prompt_index)} for prompt_index in range(1, 6)]}
                item = append_text(item, probe_text)
            item["item_id"] = f"{case_id}-all-type-{index + 1:02d}"
            item["canonical_item_ids"] = [ids[index % len(ids)]]
            if item.get("item_type") in {"single_choice", "reading_multiple_choice"}:
                option_id = ["A", "B"][index % 2]
                item.setdefault("answer", {})["option_ids"] = [option_id]
                item["rationale"] = f"Option {option_id} is correct."
            elif item.get("item_type") == "cloze":
                blank_answers = item.get("answer", {}).get("blank_answers", [])
                if blank_answers:
                    option_id = ["A", "B"][index % 2]
                    blank_answers[0]["option_id"] = option_id
                    blank_answers[0]["value"] = ["Correct", "Other"][index % 2]
                    item["rationale"] = f"Option {option_id} completes the blank."
            items.append(item)
        for index in range(len(items), target_count):
            items.append(unique_choice(base["items"][1], f"{case_id}-closing-{index + 1:02d}", ids[index % len(ids)], index + 1))
        closing_probe_repeats = int(os.environ.get("MSE_ALL_TYPES_CLOSING_REPEATS", "0"))
        if closing_probe_repeats:
            for item in items[len(base["items"]):]:
                item["stem"] += " The closing choice keeps a complete measured context for stable pagination." * closing_probe_repeats
        # The listening probe is deliberately long enough to exercise a real
        # multi-page passage, but not so long that the teacher projection's
        # bound answer/rationale becomes an avoidable one-item page.  The
    # same source must satisfy both projections under the physical 15%
    # whitespace gate.
        items[0]["script_outline"] += " The opening listening script records the setting, speakers, purpose, schedule, and evidence for this measured school-club context." * int(os.environ.get("MSE_ALL_TYPES_OPENING_REPEATS", "12"))
        if isinstance(items[-1].get("stem"), str):
            items[-1]["stem"] += " The closing item keeps enough measured text to avoid an artificial tail gap." * closing_repeats()
        items[9]["rationale"] += " This teacher-side content keeps the early page occupied for the hard whitespace gate." * 4
        if variant == "all-types":
            task = items[5]
            task["tasks"] = [
                {"task_id": "t1", "prompt": "State the club place in one sentence.", "response_format": "short answer", "score": 1},
                {"task_id": "t2", "prompt": "State the club time in one sentence.", "response_format": "short answer", "score": 1},
                {"task_id": "t3", "prompt": "State the club purpose in one sentence.", "response_format": "short answer", "score": 1},
            ]
            task["answer"] = {
                "responses": [
                    {"task_id": "t1", "response": "The club meets in the library."},
                    {"task_id": "t2", "response": "The club meets on Friday afternoon."},
                    {"task_id": "t3", "response": "The club helps students read together."},
                ]
            }
            task["score"] = 3
            task["rationale"] = "Each response matches its task."
            asset_item = items[3]
            asset_item["stimulus_assets"] = [{
                "asset_id": "fixture-grayscale",
                "semantic_role": "required_context",
                "placement": "after_passage",
                "required_for_answer": True,
                "caption": "A simple grayscale activity map.",
            }]
    elif variant.startswith("matching-"):
        items = custom_paper(case_id, matching_item(case_id))["items"]
    elif variant == "task-short-answers":
        items = custom_paper(case_id, task_item())["items"]
    elif variant.startswith("word-bank-"):
        count = int(variant.rsplit("-", 1)[-1])
        items = custom_paper(case_id, word_bank_item(count))["items"]
    elif variant == "asset":
        first = copy.deepcopy(base["items"][1])
        first["item_id"] = f"{case_id}-asset-item"
        items = custom_paper(case_id, first)["items"]
    else:
        raise ValueError(f"unknown print fixture variant: {variant}")
    assessment = copy.deepcopy(base)
    assessment["assessment_id"] = case_id
    assessment["items"] = items
    update_plan(assessment, items)
    assessment["blueprint"]["coverage_targets"] = coverage_targets(items)
    if variant == "asset":
        assessment["items"][0]["stimulus_assets"] = [{
            "asset_id": "fixture-grayscale",
            "semantic_role": "required_context",
            "placement": "after_stem",
            "required_for_answer": True,
            "caption": "A simple grayscale activity map.",
        }]
    return assessment


def create_source(case_id: str, root: Path) -> Path:
    case = load_case(ROOT, case_id)
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    variant = str(case.get("variant", "all-types"))
    assessment = assessment_for_case(case_id, variant)
    write_json(source / "assessment.json", assessment)
    asset_enabled = variant == "asset" or case_id == "print-basic-all-types"
    if asset_enabled:
        asset_source = FIXTURES / "print-asset-valid-grayscale" / "asset.pbm"
        shutil.copyfile(asset_source, source / "asset.pbm")
        write_json(source / "asset-manifest.json", {
            "schema_version": "1.0.0",
            "assets": [{
                "asset_id": "fixture-grayscale",
                "file": "asset.pbm",
                "semantic_role": "required_context",
                "required_for_answer": True,
                "rights_status": "cc_public_domain",
                "linked_item_ids": [next(item["item_id"] for item in assessment["items"] if any(ref.get("asset_id") == "fixture-grayscale" for ref in item.get("stimulus_assets", [])))],
                "pixel_width": 64,
                "pixel_height": 64,
                "color_mode": "1",
                "measured_dpi": 300,
                "contrast_ratio": 21.0,
                "cropped": False,
            }],
        })
    else:
        write_json(source / "asset-manifest.json", {"schema_version": "1.0.0", "assets": []})
    legacy = root / "legacy"
    rendered = run([
        sys.executable,
        str(SCRIPTS / "render_assessment.py"),
        "--input", str(source / "assessment.json"),
        "--out-dir", str(legacy),
    ])
    if rendered.returncode:
        raise AssertionError(f"{case_id} markdown render failed:\n{rendered.stdout}\n{rendered.stderr}")
    validation = run([
        sys.executable,
        str(SCRIPTS / "validate_assessment.py"),
        "--assessment", str(source / "assessment.json"),
        "--student", str(legacy / "student.md"),
        "--teacher", str(legacy / "teacher.md"),
        "--answer-sheet", str(legacy / "answer-sheet.json"),
        "--include-candidates",
    ])
    if validation.returncode:
        raise AssertionError(f"{case_id} content validation failed:\n{validation.stdout}\n{validation.stderr}")
    (source / "content-validation-report.json").write_text(validation.stdout, encoding="utf-8")
    shutil.copyfile(POSITIVE / "generic-cn-junior-english-v1.json", source / "generic-cn-junior-english-v1.json")
    request = {
        "schema_version": "1.0.0",
        "assessment_path": "assessment.json",
        "validation_report_path": "content-validation-report.json",
        "base_profile_path": "generic-cn-junior-english-v1.json",
        "asset_manifest_path": "asset-manifest.json",
        "outputs": ["student_pdf", "teacher_pdf", "answer_sheet"],
        "page": {"size": "A4", "orientation": "portrait"},
        "locale": "zh-CN",
        "illustration_mode": "original-grayscale" if asset_enabled else "none",
    }
    write_json(source / "render-request.json", request)
    return source


def create_compact_source(root: Path) -> Path:
    """Build the same valid paper with the second generic profile.

    This is deliberately a runtime source copy under the platform's temporary
    root: the test must prove that profile resolution and the real PDF gate
    consume compact profile bytes, rather than merely schema-checking a
    profile file.
    """
    source = create_source("print-font-fallback-valid", root)
    profile = ROOT / "references" / "rendering" / "profiles" / "generic-cn-compact-v1.json"
    compact_name = source / profile.name
    shutil.copyfile(profile, compact_name)
    request_path = source / "render-request.json"
    request = read_json(request_path)
    request["base_profile_path"] = compact_name.name
    write_json(request_path, request)
    return source


def render_case(case_id: str, root: Path) -> tuple[Path, Path]:
    source = create_source(case_id, root)
    bundle = root / "bundle"
    rendered = run([
        sys.executable,
        str(SCRIPTS / "render_pdf.py"),
        "--request", str(source / "render-request.json"), "--bundle-out", str(bundle),
    ])
    if rendered.returncode or last_json(rendered).get("status") != "RENDERED":
        raise AssertionError(f"{case_id} render failed:\n{rendered.stdout}\n{rendered.stderr}")
    preflight = run([sys.executable, str(SCRIPTS / "preflight_pdf.py"), "--bundle", str(bundle)])
    payload = last_json(preflight)
    if preflight.returncode or payload.get("status") != load_case(ROOT, case_id).get("expected_status"):
        raise AssertionError(f"{case_id} preflight failed:\n{preflight.stdout}\n{preflight.stderr}")
    return source, bundle


def clone_bundle(source: Path, root: Path, name: str) -> Path:
    target = root / name
    shutil.copytree(source, target)
    return target


def mutate_pdf(path: Path, text: str, fontsize: float = 12) -> None:
    temporary = path.with_name(path.stem + ".mutated.pdf")
    document = fitz.open(path)
    document[0].insert_text((70, 80), text, fontsize=fontsize, fontname="helv")
    document.save(temporary)
    document.close()
    temporary.replace(path)


def mutate_manifest(bundle: Path, operation: str) -> None:
    manifest_path = bundle / "render-manifest.json"
    manifest = read_json(manifest_path)
    if operation == "matching-column-hole":
        manifest["matching"][0]["candidates"][0]["isolated_item_count"] = 1
        manifest["matching"][0]["selected_layout"] = "dual-independent-flow"
        matching_blocks = [block for block in manifest["blocks"] if block.get("document") == "student" and block.get("layout_region") == "matching" and block.get("layout_column") == "right"]
        for block in matching_blocks:
            block["bbox_pt"][1] += 240
            block["bbox_pt"][3] += 240
    elif operation == "matching-single-tail-item":
        manifest["matching"][0]["candidates"][1]["isolated_item_count"] = 1
    elif operation == "task-responses-detached":
        task_id = "t1"
        response = next(
            record for record in manifest["response_areas"]
            if record.get("document") == "student" and record.get("source_task_id") == task_id
        )
        task = next(
            block for block in manifest["blocks"]
            if block.get("document") == "student"
            and block.get("role") == "task"
            and block.get("source_task_id") == task_id
        )
        response["bbox_pt"] = [
            float(task["bbox_pt"][0]),
            max(0.0, float(task["bbox_pt"][1]) - 220.0),
            float(task["bbox_pt"][2]),
            max(1.0, float(task["bbox_pt"][3]) - 220.0),
        ]
    elif operation == "choice-extra-full-line":
        item = next(
            item for item in read_json(bundle / "assessment.json")["items"]
            if item.get("item_type") in {"single_choice", "reading_multiple_choice", "vocabulary_in_context"}
        )
        manifest["response_areas"].append({
            "document": "student",
            "response_id": f"{item['item_id']}/extra",
            "source_item_id": item["item_id"],
            "response_contract": {"response_kind": "sentence", "line_policy": "one-line", "line_count": 1, "score": 1},
            "actual_line_count": 1,
            "page": 1,
            "bbox_pt": [60, 700, 500, 716],
        })
    elif operation == "box-off-center" or operation == "box-10pt":
        box = next(block["box"] for block in manifest["blocks"] if block.get("box"))
        box["horizontal_center_delta_pt" if operation == "box-off-center" else "font_size_pt"] = 9 if operation == "box-off-center" else 10
    elif operation == "asset-72dpi":
        manifest["assets"][0]["measured_dpi"] = 72
    elif operation == "asset-low-contrast":
        manifest["assets"][0]["contrast_ratio"] = 1.1
    elif operation == "font-regular-black":
        manifest["fonts"][0]["weight"] = "black"
    elif operation == "heading-orphan":
        heading = next(block for block in manifest["blocks"] if block.get("role") == "heading")
        visible = next(block for block in manifest["blocks"] if block.get("source_item_id") == heading.get("source_item_id") and block.get("role") not in {"heading", "response_area"})
        heading["page"] = int(visible["page"]) + 1
    elif operation == "option-orphan":
        item_id = next(block["source_item_id"] for block in manifest["blocks"] if block.get("role") == "option")
        stem = next(block for block in manifest["blocks"] if block.get("source_item_id") == item_id and block.get("role") in {"stem", "content", "passage"})
        option = next(block for block in manifest["blocks"] if block.get("source_item_id") == item_id and block.get("role") == "option")
        stem["page"] = 2
        option["page"] = 1
    else:
        raise ValueError(f"unknown manifest operation: {operation}")
    write_json(manifest_path, manifest)


def error_codes(payload: dict[str, Any]) -> set[str]:
    codes = {str(error.get("code")) for error in payload.get("errors", []) if isinstance(error, dict)}
    if payload.get("error_code"):
        raw_code = str(payload["error_code"])
        codes.add(raw_code)
        codes.add(raw_code.split(":", 1)[0])
    return codes


class PRDFixtureRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="mse-prd-forward-", dir=str(TEMP_ROOT))
        cls.root = Path(cls.temp.name)
        cls.sources: dict[str, Path] = {}
        cls.bundles: dict[str, Path] = {}
        for index, case_id in enumerate(POSITIVE_FIXTURES):
            source, bundle = render_case(case_id, cls.root / f"positive-{index:02d}")
            cls.sources[case_id] = source
            cls.bundles[case_id] = bundle

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def assert_case_blocked(self, case_id: str, payload: dict[str, Any], result: subprocess.CompletedProcess[str]) -> None:
        case = load_case(ROOT, case_id)
        self.assertNotEqual(result.returncode, 0, case_id)
        self.assertEqual(payload.get("status"), case["expected_status"], f"{case_id}: {result.stdout}")
        self.assertTrue(
            error_codes(payload).intersection(set(case["expected_error_codes"])),
            f"{case_id}: expected {case['expected_error_codes']}, got {sorted(error_codes(payload))}; output={result.stdout}",
        )

    def test_all_positive_cases(self) -> None:
        self.assertEqual(set(self.bundles), set(POSITIVE_FIXTURES))
        for case_id, bundle in self.bundles.items():
            with self.subTest(case=case_id):
                self.assertEqual(load_case(ROOT, case_id)["expected_status"], "PRINT_PREFLIGHT_PASS")
                for filename in ("student.pdf", "teacher.pdf", "student-ir.json", "teacher-ir.json", "answer-sheet.json", "render-manifest.json", "print-validation-report.json"):
                    self.assertTrue((bundle / filename).is_file(), filename)

    def test_every_adversarial_case_is_forwarded_from_case_json(self) -> None:
        for index, case_id in enumerate(ADVERSARIAL_FIXTURES):
            case = load_case(ROOT, case_id)
            source_bundle = self.bundles[case["source_fixture"]]
            operation = str(case["operation"])
            runner = str(case["runner"])
            with self.subTest(case=case_id):
                if runner == "preflight_pdf":
                    bundle = clone_bundle(source_bundle, self.root / "negative", f"{index:02d}-{case_id}")
                    if operation == "asset-missing":
                        (bundle / "asset.pbm").unlink()
                    elif operation == "asset-markdown-literal":
                        mutate_pdf(bundle / "student.pdf", "![asset](asset.png)")
                    elif operation == "matching-10pt":
                        mutate_pdf(bundle / "student.pdf", "ten point probe", fontsize=10)
                    else:
                        mutate_manifest(bundle, operation)
                    result = run([sys.executable, str(SCRIPTS / "preflight_pdf.py"), "--bundle", str(bundle)])
                    self.assert_case_blocked(case_id, last_json(result), result)
                elif runner == "render_pdf":
                    request_root = self.root / "negative-requests" / f"{index:02d}-{case_id}"
                    request_root.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source_bundle, request_root, dirs_exist_ok=True)
                    request = read_json(request_root / "render-request.json")
                    if operation == "schema-extra-property":
                        request["unexpected"] = True
                    else:
                        raise ValueError(operation)
                    request_path = request_root / "mutated-request.json"
                    write_json(request_path, request)
                    output = request_root / "output"
                    result = run([sys.executable, str(SCRIPTS / "render_pdf.py"), "--request", str(request_path), "--bundle-out", str(output)])
                    self.assert_case_blocked(case_id, last_json(result), result)
                else:
                    raise AssertionError(f"unsupported runner {runner}")


if __name__ == "__main__":
    unittest.main()
