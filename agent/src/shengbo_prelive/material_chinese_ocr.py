"""Offline RapidOCR CPU adapter using only explicitly selected bundled models.

The caller owns the disposable process and image byte/pixel budgets. No URL is
accepted here, model downloads are never enabled, and visualization is unused.
"""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, distribution, version
from io import BytesIO
import math
from pathlib import Path
import time

from shengbo_contracts.common import ContractError

MODEL_FILES = {"Det": "PP-OCRv6_det_small.onnx", "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
               "Rec": "PP-OCRv6_rec_small.onnx"}
# Published in rapidocr 3.10.0/default_models.yaml; bundled wheel hashes matched
# the official model manifest during the two-sample comparison.
MODEL_SHA256 = {"PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
                "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
                "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"}
LANGUAGE = "chi_sim+chi_tra+eng"


def rapidocr_capabilities():
    try:
        sdk_version, runtime_version = version("rapidocr"), version("onnxruntime")
        directory = Path(distribution("rapidocr").locate_file("rapidocr/models")).resolve()
    except PackageNotFoundError:
        return {"ready": False, "backend": "rapidocr_onnxruntime_cpu", "reason": "sdk_unavailable"}
    files = [{"name": name, "path": str(directory / name),
              "bytes": (directory / name).stat().st_size if (directory / name).is_file() else 0}
             for name in MODEL_FILES.values()]
    ready = all(item["bytes"] > 100_000 for item in files)
    return {"ready": ready, "backend": "rapidocr_onnxruntime_cpu", "rapidocr_version": sdk_version,
            "onnxruntime_version": runtime_version, "language": LANGUAGE, "models": files,
            "models_source": "official_rapidocr_wheel_bundled_models", "device": "cpu", "threads": 2,
            "network_downloads_enabled": False, "reason": None if ready else "bundled_models_missing"}


def _build_engine(capabilities):
    from rapidocr import RapidOCR
    paths = {item["name"]: item["path"] for item in capabilities["models"]}
    params = {"Global.log_level": "critical", "EngineConfig.onnxruntime.intra_op_num_threads": 2,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1, "EngineConfig.onnxruntime.use_cuda": False,
              "EngineConfig.onnxruntime.use_dml": False,
              **{task + ".model_path": paths[name] for task, name in MODEL_FILES.items()}}
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.is_file():
        params["Global.font_path"] = str(font)
    return RapidOCR(params=params)


def _adapt_result(output, png, info, stages, capabilities, elapsed_ms, *, image_source=False):
    """Convert official SDK line boxes/scores without changing recognized text."""
    from PIL import Image
    with Image.open(BytesIO(png)) as image:
        dimensions = {"width": image.width, "height": image.height}
    source_locator = "image:1" if image_source else f"page:{info['number']}"
    image_locator = "image:1" if image_source else f"image:{info['number']}"
    texts = list(output.txts or [])
    scores = list(output.scores or [])
    boxes = output.boxes.tolist() if output.boxes is not None else []
    if len(texts) != len(scores) or len(texts) != len(boxes):
        raise ContractError("MATERIAL_OCR_RESULT_INVALID", "本地OCR未返回一致的文字和位置结果", status=422)
    text = "\n".join(texts)
    if len(text) > 2_000_000:
        raise ContractError("MATERIAL_DOCUMENT_TEXT_TOO_LARGE", "OCR文本超出安全预算", status=413)
    locators, known_scores = [], []
    for index, (line, score, box) in enumerate(zip(texts, scores, boxes), 1):
        score = float(score)
        known = math.isfinite(score) and 0 <= score <= 1
        if known:
            known_scores.append(score)
        xs, ys = [float(point[0]) for point in box], [float(point[1]) for point in box]
        if not all(math.isfinite(value) for value in xs + ys):
            raise ContractError("MATERIAL_OCR_RESULT_INVALID", "本地OCR文字位置无效", status=422)
        locators.append({"id": f"{source_locator}:ocr:{index}", "kind": "ocr_text", "page": info["number"],
                         "source_locator": source_locator, "image_locator": image_locator, "text": line,
                         "bbox": {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)},
                         "polygon": box, "coordinate_space": "ocr_input_image_pixels", "image_dimensions": dimensions,
                         "confidence": score if known else "unknown",
                         "confidence_source": "rapidocr_line_score" if known else "unavailable"})
    stage = {"stage": "rapidocr_onnxruntime_cpu", "duration_ms": elapsed_ms,
             "inference_ms": round(float(output.elapse) * 1000, 3), "stage_seconds": output.elapse_list}
    stages.append(stage)
    models = [{"name": item["name"], "bytes": item["bytes"], "sha256": item["sha256"]}
              for item in capabilities["models"]]
    return {"text": text, "page": info["number"], "source_locator": source_locator,
            "image_locator": image_locator, "language": LANGUAGE, "locators": locators,
            "confidence": sum(known_scores) / len(known_scores) if known_scores else "unknown",
            "confidence_source": "mean_rapidocr_line_score" if known_scores else "unavailable",
            "image_sha256": "sha256:" + hashlib.sha256(png).hexdigest(), "image_dimensions": dimensions,
            "coverage": {"ocr_performed": True, "business_understanding_complete": False,
                         "visual_content_complete": False, "pending_visual": True, "empty_ocr_text": not bool(text.strip())},
            "parse_metrics": {"engine": "pdf-reader-sdk-v1", "ocr_engine": "rapidocr_onnxruntime_cpu",
                              "rapidocr_version": capabilities["rapidocr_version"],
                              "onnxruntime_version": capabilities["onnxruntime_version"], "model_files": models,
                              "ocr_performed": True, "ocr_language": LANGUAGE, "ocr_ms": elapsed_ms,
                              "stages": stages, "confidence_is_accuracy": False, "model_calls": 0,
                              "llm_calls": 0, "ocr_inference_passes": 1,
                              "local_only": True, "network_downloads_enabled": False,
                              "selected_page_count": 1, "device": "cpu", "cpu_threads": 2}}


def ocr_png_local(png, info, stages, *, image_source=False):
    """Run one local OCR inference; never retry the same material with a model."""
    capabilities = rapidocr_capabilities()
    if not capabilities["ready"]:
        raise ContractError("MATERIAL_OCR_SDK_NOT_READY", "本地RapidOCR CPU引擎或随包模型未就绪，请保留原图进行视觉读取", status=503,
                            details={"backend": "rapidocr_onnxruntime_cpu", "reason": capabilities.get("reason")})
    started = time.perf_counter()
    try:
        for item in capabilities["models"]:
            item["sha256"] = hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
            if item["sha256"] != MODEL_SHA256.get(item["name"]):
                raise ContractError("MATERIAL_OCR_MODEL_INTEGRITY_FAILED", "本地OCR模型与已验证官方版本不一致", status=503)
        engine = _build_engine(capabilities)
        output = engine(png)
    except ContractError:
        raise
    except Exception as error:
        raise ContractError("MATERIAL_OCR_FAILED", "本地RapidOCR未完成；页面原图仍可用于视觉读取", status=422,
                            details={"backend": "rapidocr_onnxruntime_cpu", "error_type": type(error).__name__}) from error
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return _adapt_result(output, png, info, stages, capabilities, elapsed, image_source=image_source)
