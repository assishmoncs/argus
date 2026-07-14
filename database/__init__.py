"""
Database module for Argus.
"""

from database.connection import db, Database
from database.models import (
    GroupRepository,
    UserRepository,
    GroupMemberRepository,
    WarningRepository,
    PunishmentRepository,
    CustomFilterRepository,
    NoteRepository,
    WhitelistedWordsRepository,
)
