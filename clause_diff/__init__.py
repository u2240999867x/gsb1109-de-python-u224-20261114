"""条款对照工具：比较两份 DOCX 的条款并迁移工单引用。"""

from .docx_extract import Clause, extract_clauses
from .align import Mapping, align_documents, migrate_references, migrate_text

__all__ = [
    "Clause",
    "Mapping",
    "extract_clauses",
    "align_documents",
    "migrate_references",
    "migrate_text",
]
