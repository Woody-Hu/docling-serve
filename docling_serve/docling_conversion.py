import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import HTTPException

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.docling_parse_v2_backend import DoclingParseV2DocumentBackend
from docling.backend.docling_parse_v4_backend import DoclingParseV4DocumentBackend
from docling.backend.pdf_backend import PdfDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    OcrOptions,
    PdfBackend,
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, FormatOption, PdfFormatOption
from docling_core.types.doc import ImageRefMode

from docling_serve.datamodel.convert import ConvertDocumentsOptions, ocr_factory
from docling_serve.helper_functions import _to_list_of_strings
from docling_serve.settings import docling_serve_settings

_log = logging.getLogger(__name__)


# Custom serializer for PdfFormatOption
# (model_dump_json does not work with some classes)
def _hash_pdf_format_option(pdf_format_option: PdfFormatOption) -> bytes:
    data = pdf_format_option.model_dump()

    # pipeline_options are not fully serialized by model_dump, dedicated pass
    if pdf_format_option.pipeline_options:
        data["pipeline_options"] = pdf_format_option.pipeline_options.model_dump()

        # Replace `artifacts_path` with a string representation
        data["pipeline_options"]["artifacts_path"] = repr(
            data["pipeline_options"]["artifacts_path"]
        )

    # Replace `pipeline_cls` with a string representation
    data["pipeline_cls"] = repr(data["pipeline_cls"])

    # Replace `backend` with a string representation
    data["backend"] = repr(data["backend"])

    # Handle `device` in `accelerator_options`
    if "accelerator_options" in data and "device" in data["accelerator_options"]:
        data["accelerator_options"]["device"] = repr(
            data["accelerator_options"]["device"]
        )

    # Serialize the dictionary to JSON with sorted keys to have consistent hashes
    serialized_data = json.dumps(data, sort_keys=True)
    options_hash = hashlib.sha1(serialized_data.encode()).digest()
    return options_hash


# Cache of DocumentConverter objects
_options_map: dict[bytes, PdfFormatOption] = {}


@lru_cache(maxsize=docling_serve_settings.options_cache_size)
def _get_converter_from_hash(options_hash: bytes) -> DocumentConverter:
    pdf_format_option = _options_map[options_hash]
    format_options: dict[InputFormat, FormatOption] = {
        InputFormat.PDF: pdf_format_option,
        InputFormat.IMAGE: pdf_format_option,
    }

    return DocumentConverter(format_options=format_options)


def get_converter(pdf_format_option: PdfFormatOption) -> DocumentConverter:
    options_hash = _hash_pdf_format_option(pdf_format_option)
    _options_map[options_hash] = pdf_format_option
    return _get_converter_from_hash(options_hash)


# Computes the PDF pipeline options and returns the PdfFormatOption and its hash
def get_pdf_pipeline_opts(
    request: ConvertDocumentsOptions,
) -> PdfFormatOption:
    try:
        ocr_options: OcrOptions = ocr_factory.create_options(
            kind=request.ocr_engine.value,  # type: ignore
            force_full_page_ocr=request.force_ocr,
        )
    except ImportError as err:
        raise HTTPException(
            status_code=400,
            detail="The requested OCR engine"
            f" (ocr_engine={request.ocr_engine.value})"  # type: ignore
            " is not available on this system. Please choose another OCR engine "
            "or contact your system administrator.\n"
            f"{err}",
        )

    if request.ocr_lang is not None:
        if isinstance(request.ocr_lang, str):
            ocr_options.lang = _to_list_of_strings(request.ocr_lang)
        else:
            ocr_options.lang = request.ocr_lang

    pipeline_options = PdfPipelineOptions(
        do_ocr=request.do_ocr,
        ocr_options=ocr_options,
        do_table_structure=request.do_table_structure,
        do_code_enrichment=request.do_code_enrichment,
        do_formula_enrichment=request.do_formula_enrichment,
        do_picture_classification=request.do_picture_classification,
        do_picture_description=request.do_picture_description,
    )
    pipeline_options.table_structure_options.do_cell_matching = True  # do_cell_matching
    pipeline_options.table_structure_options.mode = TableFormerMode(request.table_mode)

    if request.image_export_mode != ImageRefMode.PLACEHOLDER:
        pipeline_options.generate_page_images = True
        if request.images_scale:
            pipeline_options.images_scale = request.images_scale

    if request.pdf_backend == PdfBackend.DLPARSE_V1:
        backend: type[PdfDocumentBackend] = DoclingParseDocumentBackend
    elif request.pdf_backend == PdfBackend.DLPARSE_V2:
        backend = DoclingParseV2DocumentBackend
    elif request.pdf_backend == PdfBackend.DLPARSE_V4:
        backend = DoclingParseV4DocumentBackend
    elif request.pdf_backend == PdfBackend.PYPDFIUM2:
        backend = PyPdfiumDocumentBackend
    else:
        raise RuntimeError(f"Unexpected PDF backend type {request.pdf_backend}")

    if docling_serve_settings.artifacts_path is not None:
        if str(docling_serve_settings.artifacts_path.absolute()) == "":
            _log.info(
                "artifacts_path is an empty path, model weights will be dowloaded "
                "at runtime."
            )
            pipeline_options.artifacts_path = None
        elif docling_serve_settings.artifacts_path.is_dir():
            _log.info(
                "artifacts_path is set to a valid directory. "
                "No model weights will be downloaded at runtime."
            )
            pipeline_options.artifacts_path = docling_serve_settings.artifacts_path
        else:
            _log.warning(
                "artifacts_path is set to an invalid directory. "
                "The system will download the model weights at runtime."
            )
            pipeline_options.artifacts_path = None
    else:
        _log.info(
            "artifacts_path is unset. "
            "The system will download the model weights at runtime."
        )

    pdf_format_option = PdfFormatOption(
        pipeline_options=pipeline_options,
        backend=backend,
    )

    return pdf_format_option


def convert_documents(
    sources: Iterable[Union[Path, str, DocumentStream]],
    options: ConvertDocumentsOptions,
    headers: Optional[dict[str, Any]] = None,
):
    pdf_format_option = get_pdf_pipeline_opts(options)
    converter = get_converter(pdf_format_option)
    results: Iterator[ConversionResult] = converter.convert_all(
        sources,
        headers=headers,
    )

    return results
