"""Optional local managed-book library and external snapshot cache.

Importing this package does not initialize storage. Call ``initialize`` on a
library database or service instance explicitly.
"""

from cove_book_forge.library.database import LibraryDatabase
from cove_book_forge.library.repository import LibraryRepository
from cove_book_forge.library.service import BookLibrary, create_book_library

__all__ = ["BookLibrary", "LibraryDatabase", "LibraryRepository", "create_book_library"]
