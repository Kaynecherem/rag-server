"""Tests for document processing service."""

import pytest

from app.services.document_processor import DocumentProcessor


@pytest.fixture
def processor():
    return DocumentProcessor()


class TestDocumentProcessor:
    """Test PDF text extraction and chunking."""

    def test_count_tokens(self, processor):
        text = "This is a simple test sentence."
        count = processor.count_tokens(text)
        assert count > 0
        assert count < 20  # Should be around 7 tokens

    def test_split_text_small(self, processor):
        """Text smaller than chunk_size should produce one chunk."""
        text = "This is a short piece of text."
        chunks = processor._split_text(text, page_number=1, section_title="Test", start_index=0)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].page_number == 1
        assert chunks[0].section_title == "Test"

    def test_split_text_large(self, processor):
        """Text larger than chunk_size should produce multiple overlapping chunks."""
        # Create text that's definitely larger than 512 tokens
        text = " ".join(["insurance policy coverage"] * 200)
        chunks = processor._split_text(text, page_number=1, section_title=None, start_index=0)
        assert len(chunks) > 1
        # Verify chunk indices are sequential
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_detect_sections(self, processor):
        """Section headers should be detected from common patterns."""
        pages = [
            {
                "page_number": 1,
                "text": "COVERAGE LIMITS\nThe maximum coverage is $500,000.\n\nEXCLUSIONS\nFlood damage is excluded."
            }
        ]
        sections = processor._detect_sections(pages)
        assert len(sections) >= 2
        titles = [s["title"] for s in sections]
        assert "COVERAGE LIMITS" in titles
        assert "EXCLUSIONS" in titles

    def test_chunk_ids_unique(self, processor):
        """All chunk IDs should be unique."""
        pages = [
            {"page_number": 1, "text": "Some text on page one."},
            {"page_number": 2, "text": "More text on page two."},
        ]
        chunks = processor._chunk_sliding_window(pages)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))
