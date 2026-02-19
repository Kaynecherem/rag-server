"""Document Processing Service - PDF text extraction and intelligent chunking."""

import re
import uuid
from dataclasses import dataclass

import fitz  # PyMuPDF
import tiktoken
import structlog

from app.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


@dataclass
class Chunk:
    """A chunk of text extracted from a document."""
    chunk_id: str
    chunk_index: int
    text: str
    page_number: int | None
    section_title: str | None
    token_count: int


@dataclass
class ProcessedDocument:
    """Result of processing a PDF document."""
    full_text: str
    chunks: list[Chunk]
    page_count: int
    metadata: dict


class DocumentProcessor:
    """Processes PDF documents: extracts text, chunks intelligently."""

    def __init__(self):
        self.tokenizer = tiktoken.get_encoding("cl100k_base")
        self.chunk_size = settings.chunk_size
        self.chunk_overlap = settings.chunk_overlap

    def process_pdf(self, pdf_bytes: bytes, filename: str = "") -> ProcessedDocument:
        """
        Extract text from PDF and chunk it intelligently.

        Pipeline:
        1. Extract text with page mapping (PyMuPDF)
        2. Detect section headers from layout
        3. Chunk by sections (primary) or sliding window (fallback)
        4. Return structured chunks with metadata
        """
        logger.info("Processing PDF", filename=filename, size_bytes=len(pdf_bytes))

        # Step 1: Extract text with page mapping
        pages = self._extract_pages(pdf_bytes)
        full_text = "\n\n".join(page["text"] for page in pages)
        page_count = len(pages)

        logger.info("Text extracted", pages=page_count, chars=len(full_text))

        # Step 2: Detect sections
        sections = self._detect_sections(pages)

        # Step 3: Chunk
        if sections and len(sections) > 1:
            chunks = self._chunk_by_sections(sections)
            logger.info("Chunked by sections", chunk_count=len(chunks), section_count=len(sections))
        else:
            chunks = self._chunk_sliding_window(pages)
            logger.info("Chunked by sliding window", chunk_count=len(chunks))

        return ProcessedDocument(
            full_text=full_text,
            chunks=chunks,
            page_count=page_count,
            metadata={
                "filename": filename,
                "page_count": page_count,
                "chunk_count": len(chunks),
                "chunking_method": "section" if (sections and len(sections) > 1) else "sliding_window",
            }
        )

    def _extract_pages(self, pdf_bytes: bytes) -> list[dict]:
        """Extract text from each page with layout preservation."""
        pages = []
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for page_num, page in enumerate(doc, 1):
                text = page.get_text("text")
                if not text.strip():
                    # Fallback: try OCR-friendly extraction
                    text = page.get_text("blocks")
                    text = "\n".join(block[4] for block in text if block[6] == 0)

                pages.append({
                    "page_number": page_num,
                    "text": text.strip(),
                })
            doc.close()
        except Exception as e:
            logger.error("PDF extraction failed", error=str(e))
            raise

        return pages

    def _detect_sections(self, pages: list[dict]) -> list[dict]:
        """
        Detect section headers using common patterns in insurance documents.
        Looks for: all-caps lines, numbered sections, bold-like patterns.
        """
        sections = []
        current_section = {"title": "Document Start", "text": "", "page_number": 1}

        # Patterns for section headers
        header_patterns = [
            r"^[A-Z][A-Z\s\-]{5,}$",              # ALL CAPS lines (5+ chars)
            r"^(?:SECTION|ARTICLE|PART)\s+\d+",     # SECTION 1, ARTICLE 2, etc.
            r"^\d+\.\s+[A-Z]",                       # 1. Title format
            r"^[IVXLC]+\.\s+",                       # Roman numeral sections
            r"^(?:COVERAGE|EXCLUSION|CONDITION|DEFINITION|ENDORSEMENT)", # Insurance-specific
        ]

        for page in pages:
            lines = page["text"].split("\n")
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue

                is_header = any(re.match(pattern, line_stripped) for pattern in header_patterns)

                if is_header and len(line_stripped) < 100:
                    # Save current section and start new one
                    if current_section["text"].strip():
                        sections.append(current_section)
                    current_section = {
                        "title": line_stripped,
                        "text": "",
                        "page_number": page["page_number"],
                    }
                else:
                    current_section["text"] += line + "\n"

        # Don't forget the last section
        if current_section["text"].strip():
            sections.append(current_section)

        return sections

    def _chunk_by_sections(self, sections: list[dict]) -> list[Chunk]:
        """Chunk by detected sections. Split large sections with sliding window."""
        chunks = []
        chunk_index = 0

        for section in sections:
            text = section["text"].strip()
            token_count = len(self.tokenizer.encode(text))

            if token_count <= self.chunk_size:
                # Section fits in one chunk
                chunks.append(Chunk(
                    chunk_id=str(uuid.uuid4()),
                    chunk_index=chunk_index,
                    text=text,
                    page_number=section["page_number"],
                    section_title=section["title"],
                    token_count=token_count,
                ))
                chunk_index += 1
            else:
                # Section too large, split with sliding window
                sub_chunks = self._split_text(
                    text,
                    page_number=section["page_number"],
                    section_title=section["title"],
                    start_index=chunk_index,
                )
                chunks.extend(sub_chunks)
                chunk_index += len(sub_chunks)

        return chunks

    def _chunk_sliding_window(self, pages: list[dict]) -> list[Chunk]:
        """Fallback: chunk using sliding window across all pages."""
        chunks = []
        chunk_index = 0

        for page in pages:
            text = page["text"].strip()
            if not text:
                continue

            sub_chunks = self._split_text(
                text,
                page_number=page["page_number"],
                section_title=None,
                start_index=chunk_index,
            )
            chunks.extend(sub_chunks)
            chunk_index += len(sub_chunks)

        return chunks

    def _split_text(
        self,
        text: str,
        page_number: int | None,
        section_title: str | None,
        start_index: int,
    ) -> list[Chunk]:
        """Split text into overlapping chunks by token count."""
        tokens = self.tokenizer.encode(text)
        chunks = []
        start = 0

        while start < len(tokens):
            end = min(start + self.chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens)

            chunks.append(Chunk(
                chunk_id=str(uuid.uuid4()),
                chunk_index=start_index + len(chunks),
                text=chunk_text.strip(),
                page_number=page_number,
                section_title=section_title,
                token_count=len(chunk_tokens),
            ))

            if end >= len(tokens):
                break
            start += self.chunk_size - self.chunk_overlap

        return chunks

    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string."""
        return len(self.tokenizer.encode(text))
