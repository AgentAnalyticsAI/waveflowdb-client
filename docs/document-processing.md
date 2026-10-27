# Document Processing and OCR

WaveflowDB can process supported document formats into content suitable for indexing and retrieval. Processing may include text extraction, structured-data parsing, chunking, and optical character recognition (OCR) for scanned documents and images.

Available formats and processing capabilities depend on the installed SDK version and its optional dependencies.

## Supported File Types

The following table summarizes the supported formats and their intended processing approaches. Verify availability against your installed release.

| File types | Processing approach |
|---|---|
| `.txt`, `.py` | Extract text and split content into chunks. |
| `.ipynb` | Extract code cells and process their content. |
| `.pdf` | Extract text and optionally apply OCR to images. |
| `.docx` | Extract text and inline images, with optional OCR. |
| `.csv` | Split records while preserving column headers. |
| `.xlsx` | Convert workbook content into a structured, CSV-like representation. |
| `.jsonl`, `.ndjson` | Process individual records and validate JSON lines. |
| `.json` | Process structured JSON content; atomic handling may depend on the implementation. |
| `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`, `.webp` | Extract text from images using OCR. |

Unsupported formats may be skipped or rejected during ingestion, depending on the implementation.

## Install Document Dependencies

Some document formats require additional Python packages for extraction and processing.

```bash
pip install "waveflowdb-client[documents]"
```

## Local OCR

Local OCR extracts text from scanned pages and images using software installed on your machine.

**Important:** Tesseract must also be installed separately as a system application. Installing `pytesseract` alone does not install the Tesseract executable.

After installation, ensure that the executable is available on your system's `PATH` or configure its location as supported by your environment.

### Configure OCR

If your SDK version exposes the following configuration interface, you can enable OCR as shown:

```python
from waveflowdb_client import Config

config = Config(
    api_key="YOUR_API_KEY",
    enable_ocr=True,
    ocr_lang="eng",
)
```

- `enable_ocr` enables OCR where supported.
- `ocr_lang` specifies the OCR language, using a language code accepted by the configured OCR engine.

Confirm the correct import path, constructor arguments, and configuration options for your installed SDK version.

## Cloud OCR Providers

Cloud OCR services can extract text from scanned documents, images, and complex layouts. Provider selection depends on the document type, language requirements, credentials, and available integrations.

| Provider setting | Service | Typical use cases |
|---|---|---|
| `aws` | AWS Textract | Forms, tables, and handwriting recognition where supported. |
| `gcp` | Google Cloud Vision | Image text extraction and multilingual OCR. |
| `azure` | Azure AI Document Intelligence | Forms, invoices, and structured document extraction. |
| `mathpix` | Mathpix | Mathematical notation and scientific documents. |
| `llama_parse` | LlamaParse | Complex document layouts and structured extraction. |

### Provider dependencies

Depending on the integration, additional packages may be required:

| Provider | Python dependency |
|---|---|
| AWS Textract | `boto3` |
| Google Cloud Vision | `google-cloud-vision` |
| Azure AI Document Intelligence | `azure-ai-documentintelligence` |
| Mathpix | `requests` for REST-based integration |
| LlamaParse | `requests` for REST-based integration |

Install only the dependencies required by your chosen provider and integration.

Cloud OCR may require API keys, cloud credentials, IAM permissions, endpoint configuration, or region settings. Follow the provider's setup instructions and never commit credentials to source control.

### OCR strategies

Depending on the SDK version, OCR processing may support strategies such as:

- **Hybrid OCR:** Attempts local OCR first and uses a cloud provider as a fallback when configured.
- **Cloud OCR:** Uses a configured cloud provider for extraction.

The supported strategy names, fallback conditions, and configuration parameters depend on the installed implementation.

## OCR Provenance Markers

OCR processing may wrap extracted text in markers that identify its source and extraction method.

For example:

```text
<<<IMAGE_EXTRACT source="scan.pdf" page=2 image_index=1 method="aws_textract">>>
Extracted text from the image.
<<<END_IMAGE_EXTRACT>>>
```

These markers can help identify text extracted from an image, its source document, the page number, and the OCR provider.

If your SDK exposes the following helper functions, you can use them to work with OCR markers:

```python
from waveflowdb_client import strip_image_markers, list_image_regions
```

- `strip_image_markers` — Removes OCR provenance markers from text.
- `list_image_regions` — Retrieves information about marked image-extraction regions.

Verify that these helpers are exported by your installed package before importing them.

## Operational Considerations

- Test extraction using representative PDFs, scanned pages, spreadsheets, and image-heavy documents.
- Choose an OCR provider based on language, layout complexity, and accuracy requirements.
- Install the required system tools and Python dependencies before enabling local OCR.
- Configure cloud credentials securely and review provider costs and rate limits.
- Check extracted text for missing content, incorrect characters, and formatting issues.
- Review ingestion logs when files fail to process or expected text is missing.
- Confirm the supported file types and OCR configuration against the exact SDK version deployed in your environment.