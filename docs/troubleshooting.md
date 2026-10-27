# Troubleshooting

Use this guide to diagnose common WaveflowDB client errors. Error messages and retry behavior may vary by SDK version and API layer.

## Authentication or Authorization Errors

Check the following:

- Verify that the API key is correct, active, and has not been revoked.
- Confirm that the user identifier matches the intended account.
- Ensure that the configured host points to the correct environment.
- Verify that the account has permission to access the target namespace.

**Security tip:** Never include real API keys in issue reports, source code examples, or public logs.

## Namespace Errors

Check that:

- The namespace name is spelled correctly.
- The namespace exists, if your deployment requires manual creation.
- The API key belongs to an account authorized to access the namespace.
- The configured host points to the environment where the namespace is available.

Namespace creation and access requirements may differ between deployments. Follow the setup instructions for your WaveflowDB environment.

## File Not Found or Unsupported File Type

If a file upload fails:

- Confirm that `vector_lake_path` points to the expected directory.
- Verify that the files passed to `index.add(files=[...])` exist in the configured directory.
- Check whether the file extension is supported in [Document Processing and OCR](document-processing-and-ocr.md).
- Check whether filename sanitization or other preprocessing has changed the expected filename.

## Missing or Incomplete Extracted Text

For PDFs and image-heavy documents:

- Determine whether the document contains selectable text or scanned images.
- Check whether OCR is enabled and the required optional dependencies are installed.
- For local OCR, verify that the Tesseract executable is installed and accessible.
- For cloud OCR, verify the provider credentials, permissions, region, and API access.
- Inspect the logs for document extraction or OCR failures.

See [Document Processing and OCR](document-processing-and-ocr.md) for configuration details.

## Upload Interrupted or Partially Failed

If an upload fails partway through:

- Review the run logs, failed-file records, and per-run reports, if available.
- Identify which files or batches completed successfully.
- Confirm whether your SDK version supports resuming the operation and how it handles previously processed files.
- Avoid running overlapping upload jobs against the same source files and namespace unless your deployment supports concurrent operations.

Batch processing, checkpointing, and resume behavior depend on the SDK version and configuration.

See [Advanced Ingestion](advanced-ingestion.md) for details.

## HTTP Errors, Timeouts, and Rate Limits

When an API request fails:

1. Inspect the HTTP status code and response body.
2. Determine whether the error is related to authentication, validation, rate limiting, a timeout, or a server-side failure.
3. Check the configured timeout and retry settings, where applicable.
4. Retry only when appropriate for the error and operation.

HTTP 429 indicates rate limiting. Transient failures may be retried when retry handling is configured, but not every HTTP error is necessarily retried. Check the behavior of your installed SDK version before relying on automatic retries.

## Search Returns Unexpected Results

If search results are missing or irrelevant:

- Try a more specific natural-language query.
- Confirm that the documents were uploaded to the intended namespace.
- Use VQL when exact terms or structured filtering are required.
- Verify that metadata field names and values match those stored with the documents.
- Review tokenization, short-token handling, and search-mode behavior for your query.

See [Search and Retrieval](search-and-retrieval.md) and [VQL Documentation](vql.md).

## Reporting an Issue

If the problem persists, open a [GitHub issue](https://github.com/agentanalyticsai/waveflowdb-client/issues) with the following information:

- WaveflowDB client version and Python version.
- The operation that failed and a minimal, sanitized code sample.
- The HTTP status code and redacted error message, if available.
- Relevant logs with API keys, credentials, personal data, and other sensitive information removed.
- Minimal steps to reproduce the issue.

Include enough detail to reproduce the problem, but never share secrets or sensitive production data.